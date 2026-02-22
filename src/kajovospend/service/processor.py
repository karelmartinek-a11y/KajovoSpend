from __future__ import annotations

import datetime as dt
import os
from dateutil import parser as dtparser
import shutil
import re
import hashlib
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

from PIL import Image, ImageFilter, ImageOps
from pypdf import PdfReader
from sqlalchemy import text

from kajovospend.db.models import ImportJob
from kajovospend.db.queries import (
    add_document,
    create_file_record,
    rebuild_fts_for_document,
    upsert_supplier,
)
from kajovospend.extract.parser import extract_from_text, postprocess_items_for_db
from kajovospend.extract.structured_pdf import extract_structured_from_pdf
from kajovospend.integrations.ares import fetch_by_ico, normalize_ico
try:
    # Volitelný (placený) fallback – v našem nastavení typicky vypnutý.
    from kajovospend.integrations.openai_fallback import (
        OpenAIConfig,
        extract_with_openai,
        extract_with_openai_fallback,
    )
except Exception:  # pragma: no cover
    OpenAIConfig = None  # type: ignore
    extract_with_openai = None  # type: ignore
    extract_with_openai_fallback = None  # type: ignore

from kajovospend.ocr.pdf_render import render_pdf_to_images
from kajovospend.ocr.rapidocr_engine import RapidOcrEngine
from kajovospend.utils.hashing import sha256_file
from kajovospend.utils.text_quality import compute_text_quality, summarize_text_quality, text_quality_score
from kajovospend.utils.qr_spayd import decode_qr_from_pil, parse_spayd
from kajovospend.utils.iban import normalize_iban, is_valid_iban



def safe_move(src: Path, dst_dir: Path, target_name: str) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / target_name
    if dst.exists():
        stem = dst.stem
        suffix = dst.suffix
        i = 1
        while True:
            cand = dst_dir / f"{stem}_{i}{suffix}"
            if not cand.exists():
                dst = cand
                break
            i += 1
    for attempt in range(3):
        try:
            shutil.move(str(src), str(dst))
            return dst
        except PermissionError:
            if attempt < 2:
                time.sleep(0.05)
                continue
            # fallback: copy + best-effort delete
            shutil.copy2(str(src), str(dst))
            for _ in range(20):
                try:
                    Path(src).unlink()
                    break
                except PermissionError:
                    time.sleep(0.1)
                except Exception:
                    break
            return dst
    return dst


class Processor:
    def __init__(self, cfg: Dict[str, Any], paths, logger):
        self.cfg = cfg
        self.paths = paths
        self.log = logger
        # OCR engine is optional; if unavailable we quarantine rather than crash service.
        try:
            self.ocr_engine = RapidOcrEngine(paths.models_dir)
        except Exception as e:
            self.log.warning(f"OCR init failed; will quarantine documents. Error: {e}")
            self.ocr_engine = None


    def _try_qr_spayd_from_image(self, img: Image.Image) -> dict[str, Any]:
        """Zkusí dekódovat QR (SPAYD / QR Platba). Vrací dict s klíči account/amount/currency/vs/date..."""
        features = (self.cfg or {}).get("features") or {}
        if not (features.get("qr_spayd", {}) or {}).get("enabled", True):
            return {}
        payload = decode_qr_from_pil(img)
        if not payload:
            return {}
        sp = parse_spayd(payload)
        if not sp:
            return {}
        return {
            "account": sp.account,
            "amount": sp.amount,
            "currency": sp.currency,
            "vs": sp.vs,
            "ss": sp.ss,
            "ks": sp.ks,
            "message": sp.message,
            "date": sp.date,
            "raw": payload,
        }
    
    def _augment_extracted_with_qr(self, extracted, qr: dict[str, Any]) -> None:
        """Doplní vytažená pole z QR, pouze pokud chybí nebo zvyšuje jistotu."""
        if not extracted or not qr:
            return
        changed = False
        try:
            if not extracted.bank_account and qr.get("account"):
                extracted.bank_account = qr["account"]
                changed = True
            if extracted.total_with_vat is None and qr.get("amount") is not None:
                extracted.total_with_vat = float(qr["amount"])
                changed = True
            if (not extracted.currency or extracted.currency == "CZK") and qr.get("currency"):
                extracted.currency = str(qr["currency"])
                changed = True
            if changed:
                rr = list(extracted.review_reasons or [])
                rr.append("QR: doplněno z QR Platba (SPAYD)")
                extracted.review_reasons = rr
                extracted.confidence = min(1.0, float(extracted.confidence or 0.0) + 0.05)
        except Exception:
            return
    
    def _validate_extracted(self, extracted) -> None:
        """Dodatečné validace (např. IBAN checksum). Nic nehazí."""
        if not extracted:
            return
        features = (self.cfg or {}).get("features") or {}
        if (features.get("iban_validation", {}) or {}).get("enabled", True):
            try:
                if extracted.bank_account:
                    iban = normalize_iban(extracted.bank_account)
                    # pokud to vypadá jako IBAN, ověř checksum
                    if len(iban) >= 15 and iban[:2].isalpha() and iban[2:4].isdigit():
                        if not is_valid_iban(iban):
                            rr = list(extracted.review_reasons or [])
                            rr.append("IBAN: neprošel kontrolním součtem")
                            extracted.review_reasons = rr
                            extracted.requires_review = True
                            extracted.confidence = min(float(extracted.confidence or 0.0), 0.6)
            except Exception:
                pass
    
    def _ocr_pdf_pages(self, pdf_path: Path, status_cb=None) -> Tuple[List[str], List[float], int, str, Dict[str, Any]]:
        """
        Hybrid per-page OCR:
        - pro každou stránku nejdřív zkus embedded text (extract_text), ohodnoť kvalitu
        - OCRuje se jen tam, kde embedded nedává smysl
        - výsledek = lepší z embedded/OCR na každé stránce (řeší mixované PDF)
        """
        text_method = "pdf_hybrid"
        text_debug: Dict[str, Any] = {"path": str(pdf_path)}

        ocr_cfg = (self.cfg.get("ocr") or {}) if isinstance(self.cfg, dict) else {}
        # Spec defaults: trigger OCR if score < 0.35 or len(text) < min_len (default 0 => len check disabled)
        quality_threshold = float(ocr_cfg.get("page_text_quality_threshold", 0.35))
        min_len = int(ocr_cfg.get("page_text_min_len", 0))
        score_margin = float(ocr_cfg.get("page_text_score_margin", 0.02))

        def _conf_from_score(s: float) -> float:
            # keep deterministic bounded mapping; never claim 0.95 for garbage text
            conf = 0.50 + 0.45 * float(s)
            if conf < 0.0:
                conf = 0.0
            if conf > 0.95:
                conf = 0.95
            return float(conf)

        if status_cb:
            status_cb("PDF: čtu text (hybrid embedded/OCR)…")

        reader = None
        reader_stream = None
        try:
            reader_stream = BytesIO(pdf_path.read_bytes())
            reader = PdfReader(reader_stream)
            embedded_texts: List[str] = []
            embedded_scores: List[float] = []
            embedded_token_groups: List[int] = []
            for page in reader.pages:
                t = page.extract_text() or ""
                embedded_texts.append(t)
                s, met = text_quality_score(t)
                embedded_scores.append(float(s))
                embedded_token_groups.append(int(met.get("token_groups") or 0))
            n_pages = len(embedded_texts)
            text_debug["embedded_scores"] = embedded_scores
            if n_pages > 0:
                pages_with_text = sum(1 for t in embedded_texts if (t or "").strip())
                emb_summary = summarize_text_quality([compute_text_quality(t) for t in embedded_texts])
                text_debug["embedded"] = emb_summary
                text_debug["embedded_pages_with_text"] = pages_with_text
                self.log.info(
                    "PDF text source: embedded pages_with_text=%s/%s quality=%s",
                    pages_with_text,
                    n_pages,
                    emb_summary,
                )
        except Exception as e:
            self.log.warning(f"PDF embedded extract_text() failed; fallback to OCR. Error: {e}")
            embedded_texts = []
            embedded_scores = []
            embedded_token_groups = []
            n_pages = 0
            text_debug["embedded_error"] = str(e)
        finally:
            try:
                if reader is not None and hasattr(reader, "close"):
                    reader.close()
            except Exception:
                pass
            try:
                if reader_stream is not None:
                    reader_stream.close()
            except Exception:
                pass

        if n_pages <= 0 and self.ocr_engine is None:
            text_debug["reason"] = "no_embedded_no_ocr"
            text_debug["method"] = "none"
            return [], [], 0, "none", text_debug

        needs_ocr: List[bool] = []
        if n_pages > 0:
            for i in range(n_pages):
                s = embedded_scores[i]
                t = embedded_texts[i] or ""
                weak = (len(t) < min_len) or (float(s) < quality_threshold)
                needs_ocr.append(bool(weak))

        out_texts: List[str] = list(embedded_texts) if embedded_texts else []
        out_confs: List[float] = []
        page_audit: List[Dict[str, Any]] = []
        if embedded_texts:
            for i in range(n_pages):
                out_confs.append(_conf_from_score(embedded_scores[i]))

        if embedded_texts:
            weak_cnt = sum(1 for x in needs_ocr if x)
            text_debug["weak_pages"] = weak_cnt
            self.log.info(
                f"pdf_text_source: pages={n_pages} weak_pages={weak_cnt} "
                f"threshold={quality_threshold:.2f} min_len={min_len}"
            )

        if self.ocr_engine is None:
            if embedded_texts:
                text_debug["method"] = "embedded"
                for i in range(n_pages):
                    s = embedded_scores[i]
                    chosen = "embedded"
                    why = "weak_embedded_but_no_ocr" if needs_ocr[i] else "embedded_ok"
                    page_audit.append({
                        "page_no": i + 1,
                        "chosen_mode": "embedded",
                        "chosen_score": float(s),
                        "embedded_score": float(s),
                        "ocr_score": 0.0,
                        "embedded_len": len(embedded_texts[i] or ""),
                        "ocr_len": 0,
                        "ocr_conf": 0.0,
                        "token_groups": int(embedded_token_groups[i] or 0),
                    })
                    self.log.info(
                        f"pdf_text_source page={i+1}/{n_pages} chosen={chosen} why={why} "
                        f"emb_score={s:.3f} emb_len={len(embedded_texts[i] or '')} token_groups={int(embedded_token_groups[i] or 0)}"
                    )
                text_method = "embedded"
                text_debug["page_audit"] = page_audit
                return out_texts, out_confs, n_pages, text_method, text_debug
            text_debug["reason"] = "no_ocr_engine"
            text_debug["method"] = "none"
            return [], [], 0, "none", text_debug

        dpi_cfg = int(self.cfg["ocr"].get("pdf_dpi", 200))
        dpi = max(300, dpi_cfg)
        text_debug["dpi"] = dpi

        if not embedded_texts:
            text_method = "ocr"
            if status_cb:
                status_cb(f"PDF: render na obrázky ({dpi} DPI)…")
            images = render_pdf_to_images(pdf_path, dpi=dpi)
            texts2: List[str] = []
            confs2: List[float] = []
            for idx_page, img in enumerate(images, start=1):
                if status_cb:
                    status_cb(f"OCR: strana {idx_page}/{len(images)}…")
                t, c = self.ocr_engine.image_to_text(img)
                texts2.append(t or "")
                confs2.append(float(c or 0.0))
                s, met = text_quality_score(t or "")
                tg = int(met.get("token_groups") or 0)
                page_audit.append({
                    "page_no": idx_page,
                    "chosen_mode": "ocr",
                    "chosen_score": float(s),
                    "embedded_score": 0.0,
                    "ocr_score": float(s),
                    "embedded_len": 0,
                    "ocr_len": len(t or ""),
                    "ocr_conf": float(c or 0.0),
                    "token_groups": tg,
                })
                self.log.info(
                    f"pdf_text_source page={idx_page}/{len(images)} chosen=ocr why=no_embedded "
                    f"ocr_score={float(s):.3f} ocr_len={len(t or '')} token_groups={tg} ocr_conf={float(c or 0.0):.3f}"
                )
            text_debug["method"] = text_method
            text_debug["ocr_conf_avg"] = float(sum(confs2) / len(confs2)) if confs2 else 0.0
            text_debug["ocr_conf_min"] = float(min(confs2)) if confs2 else 0.0
            text_debug["ocr_conf_max"] = float(max(confs2)) if confs2 else 0.0
            ocr_summary = summarize_text_quality([compute_text_quality(t) for t in texts2])
            text_debug["ocr"] = ocr_summary
            text_debug["page_audit"] = page_audit
            self.log.info(
                "PDF text source: ocr reason=embedded_empty dpi=%s pages=%s conf_avg=%.3f conf_min=%.3f conf_max=%.3f quality=%s",
                dpi,
                len(images),
                text_debug["ocr_conf_avg"],
                text_debug["ocr_conf_min"],
                text_debug["ocr_conf_max"],
                ocr_summary,
            )
            return texts2, confs2, len(images), text_method, text_debug

        need_idxs = [i for i, need in enumerate(needs_ocr) if need]
        if not need_idxs:
            text_method = "embedded"
            text_debug["method"] = text_method
            for i in range(n_pages):
                s = embedded_scores[i]
                page_audit.append({
                    "page_no": i + 1,
                    "chosen_mode": "embedded",
                    "chosen_score": float(s),
                    "embedded_score": float(s),
                    "ocr_score": 0.0,
                    "embedded_len": len(embedded_texts[i] or ""),
                    "ocr_len": 0,
                    "ocr_conf": 0.0,
                    "token_groups": int(embedded_token_groups[i] or 0),
                })
                self.log.info(
                    f"pdf_text_source page={i+1}/{n_pages} chosen=embedded why=embedded_ok "
                    f"emb_score={s:.3f} emb_len={len(embedded_texts[i] or '')} token_groups={int(embedded_token_groups[i] or 0)}"
                )
            text_debug["page_audit"] = page_audit
            return out_texts, out_confs, n_pages, text_method, text_debug

        segments: List[Tuple[int, int]] = []
        seg_start = need_idxs[0]
        seg_prev = need_idxs[0]
        for idx in need_idxs[1:]:
            if idx == seg_prev + 1:
                seg_prev = idx
                continue
            segments.append((seg_start, seg_prev))
            seg_start = idx
            seg_prev = idx
        segments.append((seg_start, seg_prev))
        text_debug["ocr_segments"] = segments

        selections: List[Dict[str, Any]] = []
        for s0, s1 in segments:
            count = (s1 - s0 + 1)
            if status_cb:
                status_cb(f"PDF: OCR strany {s0+1}-{s1+1}/{n_pages}…")
            images = render_pdf_to_images(pdf_path, dpi=dpi, start_page=s0, max_pages=count)
            for off, img in enumerate(images):
                page_idx = s0 + off
                if status_cb:
                    status_cb(f"OCR: strana {page_idx+1}/{n_pages}…")
                ocr_text, ocr_conf = self.ocr_engine.image_to_text(img)
                ocr_text = ocr_text or ""
                ocr_conf_f = float(ocr_conf or 0.0)

                emb_s = embedded_scores[page_idx]
                ocr_s, ocr_met = text_quality_score(ocr_text)
                ocr_s = float(ocr_s)
                emb_tg = int(embedded_token_groups[page_idx] or 0)
                ocr_tg = int(ocr_met.get("token_groups") or 0)

                choose_ocr = bool(ocr_s > (emb_s + score_margin))
                if choose_ocr:
                    out_texts[page_idx] = ocr_text
                    out_confs[page_idx] = ocr_conf_f
                    chosen = "ocr"
                    why = "embedded_weak_or_worse"
                    chosen_score = ocr_s
                    chosen_tg = ocr_tg
                else:
                    chosen = "embedded"
                    why = "ocr_not_better"
                    chosen_score = float(emb_s)
                    chosen_tg = emb_tg

                selections.append(
                    {
                        "page": page_idx + 1,
                        "chosen": chosen,
                        "why": why,
                        "emb_score": emb_s,
                        "ocr_score": ocr_s,
                        "ocr_conf": ocr_conf_f,
                    }
                )
                page_audit.append({
                    "page_no": page_idx + 1,
                    "chosen_mode": chosen,
                    "chosen_score": float(chosen_score),
                    "embedded_score": float(emb_s),
                    "ocr_score": float(ocr_s),
                    "embedded_len": len(embedded_texts[page_idx] or ""),
                    "ocr_len": len(ocr_text or ""),
                    "ocr_conf": float(ocr_conf_f),
                    "token_groups": int(chosen_tg),
                })
                self.log.info(
                    f"pdf_text_source page={page_idx+1}/{n_pages} chosen={chosen} why={why} "
                    f"emb_score={float(emb_s):.3f} emb_len={len(embedded_texts[page_idx] or '')} emb_tg={emb_tg} "
                    f"ocr_score={float(ocr_s):.3f} ocr_len={len(ocr_text or '')} ocr_tg={ocr_tg} "
                    f"ocr_conf={ocr_conf_f:.3f}"
                )

        text_debug["method"] = text_method
        text_debug["selections"] = selections
        text_debug["page_audit"] = page_audit
        # pokud jsme nakonec vybrali pouze embedded text, označ metodu jako embedded (žádné OCR použité)
        if page_audit and not any(str(pa.get("chosen_mode")) == "ocr" for pa in page_audit):
            text_method = "embedded"
            text_debug["method"] = text_method
        # deterministic aggregation: weighted by chosen text length
        denom = 0.0
        num = 0.0
        for pa in page_audit:
            clen = max(1, int(pa.get("embedded_len") if pa.get("chosen_mode") == "embedded" else pa.get("ocr_len") or 0))
            denom += float(clen)
            num += float(pa.get("chosen_score") or 0.0) * float(clen)
        text_debug["document_text_quality"] = float(num / denom) if denom > 0 else 0.0
        return out_texts, out_confs, n_pages, text_method, text_debug

    def _merge_extracted_by_key(self, per_page: List[Tuple[int, Any, str, float]]) -> List[Dict[str, Any]]:
        """
        per_page: [(page_no, Extracted, full_text, ocr_conf), ...]

        Bezpečné, deterministické slučování sousedních stránek:
        - pokud mají obě stránky kompletní klíč (IČO+číslo+datum) a klíč je shodný => merge
        - jinak používá konzervativní score na shodu atributů (IČO/číslo/datum/banka/měna),
          bez konfliktů na nenulových hodnotách
        - speciální případ: pokud první stránka má kompletní klíč a následující stránka má „prázdný“ header
          (typicky jen tabulka položek), merge povolíme, pokud text druhé stránky nevypadá jako nový doklad
        """
        def _norm(s: Any) -> str:
            return (str(s).strip() if s is not None else "")

        def _is_real_ico(v: Any) -> bool:
            vv = _norm(v)
            return bool(vv) and vv.isdigit() and len(vv) == 8

        def _conflict(a: Any, b: Any) -> bool:
            aa, bb = _norm(a), _norm(b)
            return bool(aa and bb and aa != bb)

        def _looks_new_document(text: str) -> bool:
            t = (text or "").upper()
            # typické začátky nového dokladu / opakované hlavičky
            if "DAŇOVÝ DOKLAD" in t or "DANOVY DOKLAD" in t:
                return True
            if re.search(r"\bFAKTURA\b", t) and ("FAKTURA #" in t or "FAKTURA Č" in t or "FAKTURA C" in t):
                return True
            if "INVOICE" in t and ("INVOICE #" in t or "INVOICE NO" in t):
                return True
            return False

        def _merge_score(a: Any, b: Any) -> int:
            score = 0
            # doc_number strongest
            if _norm(a.doc_number) and _norm(b.doc_number) and _norm(a.doc_number) == _norm(b.doc_number):
                score += 3
            # supplier_ico
            if _norm(a.supplier_ico) and _norm(b.supplier_ico) and _norm(a.supplier_ico) == _norm(b.supplier_ico):
                score += 2
            # issue_date
            if a.issue_date and b.issue_date and a.issue_date == b.issue_date:
                score += 2
            # bank_account
            if _norm(a.bank_account) and _norm(b.bank_account) and _norm(a.bank_account) == _norm(b.bank_account):
                score += 1
            # currency
            if _norm(a.currency) and _norm(b.currency) and _norm(a.currency) == _norm(b.currency):
                score += 1
            return int(score)

        merged: List[Dict[str, Any]] = []
        cur: Dict[str, Any] | None = None

        for page_no, ex, full_text, ocr_conf in per_page:
            key = (ex.supplier_ico, ex.doc_number, ex.issue_date)
            key_ok = bool(ex.supplier_ico and ex.doc_number and ex.issue_date)

            if cur is None:
                cur = {
                    "page_from": page_no,
                    "page_to": page_no,
                    "extracted": ex,
                    "full_text": full_text or "",
                    "ocr_conf": float(ocr_conf or 0.0),
                    "key": key if key_ok else None,
                }
                continue

            # only consecutive pages may merge
            if page_no != int(cur["page_to"]) + 1:
                merged.append(cur)
                cur = {
                    "page_from": page_no,
                    "page_to": page_no,
                    "extracted": ex,
                    "full_text": full_text or "",
                    "ocr_conf": float(ocr_conf or 0.0),
                    "key": key if key_ok else None,
                }
                continue

            cur_ex = cur["extracted"]
            cur_key_ok = bool(cur_ex.supplier_ico and cur_ex.doc_number and cur_ex.issue_date)
            cur_key = (cur_ex.supplier_ico, cur_ex.doc_number, cur_ex.issue_date)

            # hard conflicts on explicit fields
            if _conflict(cur_ex.doc_number, ex.doc_number) or _conflict(cur_ex.issue_date, ex.issue_date):
                merged.append(cur)
                cur = {
                    "page_from": page_no,
                    "page_to": page_no,
                    "extracted": ex,
                    "full_text": full_text or "",
                    "ocr_conf": float(ocr_conf or 0.0),
                    "key": key if key_ok else None,
                }
                continue

            # IČO conflict only if both look like real IČO
            if _is_real_ico(cur_ex.supplier_ico) and _is_real_ico(ex.supplier_ico) and _conflict(cur_ex.supplier_ico, ex.supplier_ico):
                merged.append(cur)
                cur = {
                    "page_from": page_no,
                    "page_to": page_no,
                    "extracted": ex,
                    "full_text": full_text or "",
                    "ocr_conf": float(ocr_conf or 0.0),
                    "key": key if key_ok else None,
                }
                continue

            should_merge = False

            # case 1: exact complete key match
            if cur_key_ok and key_ok and cur_key == key:
                should_merge = True
            else:
                # case 2: score-based merge
                score = _merge_score(cur_ex, ex)
                # allow merge if strong enough
                if score >= 4:
                    should_merge = True
                # case 3: continuation page with missing header (items only)
                elif cur_key_ok and (not ex.supplier_ico and not ex.doc_number and not ex.issue_date) and (len(list(ex.items or [])) >= 2):
                    # don't merge if it looks like new document header
                    if not _looks_new_document(full_text or ""):
                        should_merge = True

            if should_merge:
                cur["page_to"] = page_no
                try:
                    # merge items + text
                    cur_ex.items = list(cur_ex.items or []) + list(ex.items or [])
                    # propagate missing structured fields
                    if not cur_ex.supplier_ico and ex.supplier_ico:
                        cur_ex.supplier_ico = ex.supplier_ico
                    if not cur_ex.doc_number and ex.doc_number:
                        cur_ex.doc_number = ex.doc_number
                    if not cur_ex.issue_date and ex.issue_date:
                        cur_ex.issue_date = ex.issue_date
                    if cur_ex.total_with_vat is None and ex.total_with_vat is not None:
                        cur_ex.total_with_vat = ex.total_with_vat
                    if (not cur_ex.bank_account) and ex.bank_account:
                        cur_ex.bank_account = ex.bank_account
                    if (not cur_ex.currency) and ex.currency:
                        cur_ex.currency = ex.currency

                    cur_ex.confidence = float(max(cur_ex.confidence or 0.0, ex.confidence or 0.0))
                    cur_ex.requires_review = bool(cur_ex.requires_review or ex.requires_review)
                    cur_ex.review_reasons = list(dict.fromkeys((cur_ex.review_reasons or []) + (ex.review_reasons or [])))
                    cur["extracted"] = cur_ex
                except Exception:
                    pass
                cur["full_text"] = (cur["full_text"] + "\n\n" + (full_text or "")).strip()
                cur["ocr_conf"] = float(sum([cur["ocr_conf"], float(ocr_conf or 0.0)]) / 2.0)
                # update key if now complete
                if cur_ex.supplier_ico and cur_ex.doc_number and cur_ex.issue_date:
                    cur["key"] = (cur_ex.supplier_ico, cur_ex.doc_number, cur_ex.issue_date)
                continue

            # no merge
            merged.append(cur)
            cur = {
                "page_from": page_no,
                "page_to": page_no,
                "extracted": ex,
                "full_text": full_text or "",
                "ocr_conf": float(ocr_conf or 0.0),
                "key": key if key_ok else None,
            }

        if cur is not None:
            merged.append(cur)
        return merged

    

    def _force_ocr_pdf_range(self, pdf_path: Path, page_from: int, page_to: int, status_cb=None) -> Tuple[str, float]:
        """Vynucené OCR pro zadaný rozsah stránek (ignoruje embedded text).
        Používá RapidOCR nad rendrovanými stránkami PDF.
        """
        if self.ocr_engine is None or (not getattr(self.ocr_engine, "is_available", lambda: False)()):
            return "", 0.0
        try:
            dpi = int((self.cfg.get("ocr") or {}).get("pdf_dpi", 200))
        except Exception:
            dpi = 200
        try:
            max_pages = int(page_to - page_from + 1)
        except Exception:
            max_pages = 1
        if status_cb:
            status_cb("OCR retry: rendruji stránky…")
        imgs = render_pdf_to_images(pdf_path, dpi=dpi, max_pages=max_pages, start_page=max(0, int(page_from) - 1))
        texts: List[str] = []
        confs: List[float] = []
        if status_cb:
            status_cb("OCR retry: čtu text…")
        for im in imgs:
            try:
                t, c = self.ocr_engine.image_to_text(im)
                if t:
                    texts.append(t)
                    confs.append(float(c or 0.0))
            except Exception:
                continue
        if not texts:
            return "", 0.0
        avg_conf = sum(confs) / max(1, len(confs))
        return "\n\n".join(texts), float(avg_conf)

    

    def _score_extracted_candidate(self, ex) -> Tuple[int, int, int, int, int]:
        """Skóre pro výběr nejlepšího offline výstupu. Vyšší je lepší (lexikograficky)."""
        try:
            items_n = len(list(ex.items or []))
        except Exception:
            items_n = 0
        core = 0
        if getattr(ex, "supplier_ico", None):
            core += 1
        if getattr(ex, "doc_number", None):
            core += 1
        if getattr(ex, "issue_date", None):
            core += 1
        if getattr(ex, "total_with_vat", None) is not None:
            core += 1

        reasons = [str(r or "") for r in (getattr(ex, "review_reasons", None) or [])]
        sum_bad = any(("nesedí součet" in r) or ("nelze ověřit součet" in r) for r in reasons)
        sum_ok = 0 if sum_bad else 1
        no_review = 1 if (not bool(getattr(ex, "requires_review", False))) else 0
        conf = int(round(1000.0 * float(getattr(ex, "confidence", 0.0) or 0.0)))
        return (items_n, core, sum_ok, no_review, conf)

    def _ocr_pdf_range_candidates(
        self,
        pdf_path: Path,
        page_from: int,
        page_to: int,
        *,
        dpis: List[int],
        rotations: List[int],
        include_reconstructed: bool,
        max_candidates_per_page: int = 4,
        status_cb=None,
    ) -> List[Tuple[str, float, Dict[str, Any]]]:
        """Vygeneruje více OCR kandidátů pro rozsah stránek (různá DPI + rotace + rekonstrukce řádků)."""
        if self.ocr_engine is None or (not getattr(self.ocr_engine, "is_available", lambda: False)()):
            return []
        try:
            max_pages = int(page_to - page_from + 1)
        except Exception:
            max_pages = 1

        out: List[Tuple[str, float, Dict[str, Any]]] = []
        for dpi in dpis:
            if status_cb:
                status_cb(f"OCR ensemble: rendruji stránky (dpi={dpi})…")
            imgs = render_pdf_to_images(pdf_path, dpi=int(dpi), max_pages=max_pages, start_page=max(0, int(page_from) - 1))
            if not imgs:
                continue

            texts: List[str] = []
            confs: List[float] = []
            # per-page choose best candidate by OCR-internal ranking
            for im in imgs:
                try:
                    cands = self.ocr_engine.image_to_text_candidates(
                        im,
                        rotations=tuple(int(r) for r in rotations),
                        include_reconstructed=bool(include_reconstructed),
                        max_candidates=int(max_candidates_per_page),
                    )
                    if not cands:
                        continue
                    # choose best by engine scoring order (already sorted)
                    t, c, meta = cands[0]
                    if t:
                        texts.append(t)
                        confs.append(float(c or 0.0))
                except Exception:
                    continue

            if not texts:
                continue
            avg_conf = float(sum(confs) / max(1, len(confs)))
            out.append(("\n\n".join(texts), avg_conf, {"dpi": int(dpi), "rotations": list(rotations), "include_reconstructed": bool(include_reconstructed)}))
        return out

    def _ocr_image_candidates(
        self,
        image: Image.Image,
        *,
        rotations: List[int],
        include_reconstructed: bool,
        max_candidates: int,
    ) -> List[Tuple[str, float, Dict[str, Any]]]:
        if self.ocr_engine is None or (not getattr(self.ocr_engine, "is_available", lambda: False)()):
            return []
        cands = self.ocr_engine.image_to_text_candidates(
            image,
            rotations=tuple(int(r) for r in rotations),
            include_reconstructed=bool(include_reconstructed),
            max_candidates=int(max_candidates),
        )
        return [(t, float(c or 0.0), dict(m)) for t, c, m in cands]

    def _offline_ensemble_best(
        self,
        *,
        path: Path,
        page_from: int,
        page_to: int,
        baseline_text: str,
        baseline_extracted,
        status_cb=None,
    ) -> Tuple[Optional[str], Optional[Any], Dict[str, Any]]:
        """Zkusí víc offline metod a vrátí nejlepší (text, extracted, debug)."""
        ocr_cfg = (self.cfg.get("ocr") or {}) if isinstance(self.cfg, dict) else {}
        ens = (ocr_cfg.get("ensemble") or {}) if isinstance(ocr_cfg, dict) else {}
        enabled = bool(ens.get("enabled", True))
        if not enabled or self.ocr_engine is None or (not getattr(self.ocr_engine, "is_available", lambda: False)()):
            return None, None, {}

        dpis = ens.get("dpis", [200, 300, 450, 600])
        if not isinstance(dpis, list) or not dpis:
            dpis = [200, 300, 450]
        dpis = [int(x) for x in dpis if isinstance(x, (int, float, str)) and str(x).isdigit()]
        if not dpis:
            dpis = [200, 300, 450]

        rotations = ens.get("rotations", [0, 90, 180, 270])
        if not isinstance(rotations, list) or not rotations:
            rotations = [0, 90, 180, 270]
        rotations = [int(r) for r in rotations if isinstance(r, (int, float, str))]
        include_reconstructed = bool(ens.get("include_reconstructed", True))

        # candidate pool: baseline + OCR variants
        best_text = baseline_text
        best_ex = baseline_extracted
        best_score = self._score_extracted_candidate(best_ex)
        debug: Dict[str, Any] = {"baseline_score": list(best_score)}

        candidates: List[Tuple[str, float, Dict[str, Any]]] = []
        if path.suffix.lower() == ".pdf":
            candidates.extend(
                self._ocr_pdf_range_candidates(
                    path,
                    page_from,
                    page_to,
                    dpis=list(dpis),
                    rotations=list(rotations),
                    include_reconstructed=include_reconstructed,
                    status_cb=status_cb,
                )
            )
        else:
            try:
                with Image.open(path) as im:
                    im = im.convert("RGB")
                    candidates.extend(
                        self._ocr_image_candidates(
                            im,
                            rotations=list(rotations),
                            include_reconstructed=include_reconstructed,
                            max_candidates=int(ens.get("max_candidates", 8)),
                        )
                    )
            except Exception:
                candidates = []

        debug["candidates"] = []
        for cand_text, cand_conf, meta in candidates:
            try:
                ex2 = extract_from_text(cand_text)
            except Exception:
                continue
            sc = self._score_extracted_candidate(ex2)
            debug["candidates"].append({"score": list(sc), "meta": meta, "conf": float(cand_conf), "len": len(cand_text or "")})
            if sc > best_score:
                best_score = sc
                best_text = cand_text
                best_ex = ex2

        if best_ex is not baseline_extracted:
            debug["selected_score"] = list(best_score)
        return best_text, best_ex, debug

    def _enhance_for_openai(self, image: Image.Image) -> Image.Image:
        """Lehke zvyseni kontrastu a ostrosti pro lepsi citelnost."""
        img = image.convert("RGB")
        img = ImageOps.autocontrast(img)
        img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
        return img

    def _prepare_openai_images(
        self,
        path: Path,
        *,
        page_from: int,
        page_to: int,
    ) -> List[Tuple[str, bytes]]:
        """Pripravi obrazove vstupy pro OpenAI (origin + volitelne zlepsene varianty)."""
        cfg = self.cfg.get("openai") if isinstance(self.cfg, dict) else {}
        if not isinstance(cfg, dict):
            cfg = {}
        dpi = int(cfg.get("image_dpi", 300) or 300)
        max_pages = int(cfg.get("image_max_pages", 3) or 3)
        enhance = bool(cfg.get("image_enhance", True))
        variants = int(cfg.get("image_variants", 2) or 2)
        if variants < 1:
            variants = 1

        images_payload: List[Tuple[str, bytes]] = []
        try:
            if path.suffix.lower() == ".pdf":
                pages_cnt = max(1, int(page_to - page_from + 1))
                imgs = render_pdf_to_images(
                    path,
                    dpi=dpi,
                    max_pages=min(max_pages, pages_cnt),
                    start_page=max(0, page_from - 1),
                )
            else:
                with Image.open(path) as im:
                    imgs = [im.convert("RGB")]
        except Exception:
            return images_payload

        for im in imgs[:max_pages]:
            try:
                bio = BytesIO()
                im.convert("RGB").save(bio, format="PNG")
                images_payload.append(("image/png", bio.getvalue()))
                if enhance and variants > 1:
                    enh = self._enhance_for_openai(im)
                    bio2 = BytesIO()
                    enh.save(bio2, format="PNG")
                    images_payload.append(("image/png", bio2.getvalue()))
            except Exception:
                continue
        return images_payload

    def _merge_openai_result(self, extracted, obj: Dict[str, Any], *, prefer_items: bool = True) -> bool:
        """Slouci vysledek z OpenAI do Extracted (konzervativne)."""
        if not isinstance(obj, dict):
            return False
        changed = False

        def _is_pseudo_ico(v: Any) -> bool:
            vv = str(v or "")
            return vv.startswith("PSEUDO") or vv.startswith("pseudo") or vv.startswith("OMV_") or vv.startswith("NEZNAMY")

        items = obj.get("items")
        if isinstance(items, list) and len(items) > 0:
            cur_items = list(extracted.items or [])
            if prefer_items or (len(cur_items) == 0) or (len(items) > len(cur_items)):
                extracted.items = [it for it in items if isinstance(it, dict)]
                changed = True

        if obj.get("supplier_ico") and (not extracted.supplier_ico or _is_pseudo_ico(extracted.supplier_ico)):
            extracted.supplier_ico = str(obj.get("supplier_ico"))
            changed = True
        if obj.get("doc_number") and (not extracted.doc_number):
            extracted.doc_number = str(obj.get("doc_number"))
            changed = True
        if obj.get("bank_account") and (not extracted.bank_account):
            extracted.bank_account = str(obj.get("bank_account"))
            changed = True
        if obj.get("currency"):
            extracted.currency = str(obj.get("currency"))
            changed = True

        if (extracted.issue_date is None) and obj.get("issue_date"):
            try:
                extracted.issue_date = dtparser.parse(str(obj.get("issue_date"))).date()
                changed = True
            except Exception:
                pass
        if (extracted.total_with_vat is None) and (obj.get("total_with_vat") is not None):
            try:
                extracted.total_with_vat = float(obj.get("total_with_vat"))
                changed = True
            except Exception:
                pass

        if changed:
            extracted.confidence = float(max(extracted.confidence or 0.0, 0.90))
        return changed
        return None, None, debug
    def _ocr_image(self, path: Path, status_cb=None) -> Tuple[str, float, int]:
        if self.ocr_engine is None:
            return "", 0.0, 1
        if status_cb:
            status_cb("OCR: zpracovávám obrázek…")
        with Image.open(path) as img:
            t, c = self.ocr_engine.image_to_text(img)
        try:
            q = summarize_text_quality([compute_text_quality(t or "")])
            self.log.info("Image text source: ocr conf=%.3f quality=%s", float(c or 0.0), q)
        except Exception:
            pass
        return t, c, 1

    def _guess_supplier_ico_from_text(self, text: str) -> Optional[str]:
        """
        Best-effort self-healing IČO:
        - primárně zkusí explicitní výskyty "IČO/ICO" + 8 číslic
        - jinak sbírá 8místné kandidáty (\\b\\d{8}\\b), filtruje typické falešné vzory (PSČ, tel, EAN)
        - validuje přes ARES (rychlý timeout)
        - pokud vyjde přesně 1 validní kandidát, vrátí jej
        """
        if not text:
            return None

        t = str(text)

        def _is_false_pattern_line(line: str) -> bool:
            ln = (line or "").strip().lower()
            if not ln:
                return False
            if re.search(r"\bps[čc]\b", ln) or re.search(r"\bzip\b", ln):
                return True
            if re.search(r"\b(tel|telefon|mobil|phone)\b", ln):
                return True
            if re.search(r"\b(ean|barcode|čárov|carov)\b", ln):
                return True
            return False

        def _line_for_pos(pos: int) -> str:
            start = t.rfind("\n", 0, pos)
            start = 0 if start < 0 else start + 1
            end = t.find("\n", pos)
            end = len(t) if end < 0 else end
            return t[start:end]

        def _validate_candidate(raw_ico: str) -> Optional[str]:
            try:
                ico_n = normalize_ico(raw_ico)
            except Exception:
                return None
            try:
                rec = fetch_by_ico(ico_n, timeout=4)
                return getattr(rec, "ico", ico_n)
            except Exception:
                return None

        # 1) explicitní IČO/ICO patterny (vyšší priorita)
        #    - podporuje "IČO: 12345678", "ICO 12345678", "IČO#12345678" apod.
        label_re = re.compile(r"(?i)\b(ič[o0]|ico)\b\s*[:#]?\s*(\d{8})\b")
        for m in label_re.finditer(t):
            cand = m.group(2)
            ico = _validate_candidate(cand)
            if ico:
                return ico

        # 2) obecné 8místné kandidáty + filtrování falešných vzorů
        cands: List[str] = []
        for m in re.finditer(r"\b\d{8}\b", t):
            cand = m.group(0)
            line = _line_for_pos(m.start())
            if _is_false_pattern_line(line):
                continue
            if cand not in cands:
                cands.append(cand)
            if len(cands) >= 20:
                break

        if not cands:
            return None

        valid: List[str] = []
        for cand in cands:
            ico = _validate_candidate(cand)
            if ico:
                valid.append(ico)
            if len(valid) > 1:
                break

        # bezpečné chování: vrátíme jen když je jednoznačný výsledek
        if len(valid) == 1:
            return valid[0]
        return None

    def _looks_like_ico(self, ico: str | None) -> bool:
        if not ico:
            return False
        s = str(ico).strip()
        return bool(re.fullmatch(r"\d{6,10}", s))

    def _extract_supplier_name_guess(self, text: str) -> str | None:
        """Heuristika pro účtenky bez IČO: vezme první 'rozumný' řádek v horní části."""
        if not text:
            return None
        for ln in (text.splitlines()[:30]):
            ln = str(ln).strip()
            if not ln:
                continue
            # ignoruj generické a šum
            if re.search(r"(daňov|doklad|účtenk|uctenk|datum|celkem|prodej|platba|dph|iban|swift)", ln, re.IGNORECASE):
                continue
            # musí obsahovat aspoň 3 písmena
            if len(re.findall(r"[A-Za-zÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]", ln)) < 3:
                continue
            return ln[:80].strip()
        return None

    def _pseudo_ico(self, supplier_name: str) -> str:
        """Deterministické pseudo-IČO pro retail účtenky bez uvedeného IČO."""
        base = (supplier_name or "UNKNOWN").strip().upper().encode("utf-8", errors="ignore")
        h = hashlib.sha256(base).hexdigest()[:10]
        return f"NOICO-{h}"

    def _classify_doc_type(self, text: str) -> str:
        """
        Jednoduchá klasifikace typu dokladu podle tokenů.
        - invoice: "FAKTURA", "DAŇOVÝ DOKLAD", "INVOICE"
        - receipt: "ÚČTENKA", "POKLADNA", "KASA", "DĚKUJEME"
        Default: invoice (bezpečnější).
        """
        t = (text or "").upper()
        if any(k in t for k in ["FAKTURA", "DAŇOVÝ DOKLAD", "DANOVY DOKLAD", "INVOICE"]):
            return "invoice"
        if any(k in t for k in ["ÚČTENKA", "UCTENKA", "POKLADNA", "KASA", "DĚKUJEME", "DEKUJEME"]):
            return "receipt"
        return "invoice"

    def _synthetic_doc_number(self, sha256: str, page_from: int, page_to: int, issue_date, total_with_vat) -> str:
        """
        Stabilní syntetické číslo dokladu pro účtenky bez doc_number:
        prefix SHA256 + rozsah stránek + (volitelně) datum + (volitelně) total v centech.
        """
        parts: List[str] = []
        parts.append((sha256 or "")[:12])
        parts.append(f"P{int(page_from):02d}-{int(page_to):02d}")
        if issue_date:
            try:
                parts.append(issue_date.strftime("%Y%m%d"))
            except Exception:
                pass
        if total_with_vat is not None:
            try:
                cents = int(round(float(total_with_vat) * 100.0))
                parts.append(str(cents))
            except Exception:
                pass
        return "R-" + "-".join(parts)

    def _prune_receipt_reasons(self, reasons: List[str]) -> List[str]:
        """U uctenek nechame duvody zobrazeny, skryjeme jen obecne duplicitni hlasky."""
        drop = {
            "nekompletni vytezeni",
        }
        out: List[str] = []
        for r in reasons or []:
            rr = str(r or "")
            if rr in drop:
                continue
            out.append(r)
        return out

    def process_path(self, session, path: Path, status_cb=None) -> Dict[str, Any]:
        # returns dict with outcome
        sha = sha256_file(path)
        # dedupe check
        existing = session.execute(
            text("SELECT id, status FROM files WHERE sha256 = :sha"),
            {"sha": sha},
        ).fetchone()
        if existing:
            # duplicate file, move to DUPLICITY
            out_base = Path(self.cfg["paths"]["output_dir"])
            dup_dir = out_base / self.cfg["paths"].get("duplicate_dir_name", "DUPLICITY")
            moved = safe_move(path, dup_dir, path.name)
            return {"status": "DUPLICATE", "sha256": sha, "moved_to": str(moved), "text_method": None, "text_debug": {}}

        if status_cb:
            status_cb("Začínám vytěžování…")

        # OCR
        min_conf = float(self.cfg["ocr"].get("min_confidence", 0.65))
        pages = 1
        per_doc_chunks: List[Dict[str, Any]] = []
        text_method: Optional[str] = None
        text_debug: Dict[str, Any] = {}
        page_audit_map: Dict[int, Dict[str, Any]] = {}



        # Structured-first: pokus o vytěžení z PDF attachmentů (ISDOC / Factur-X / ZUGFeRD / apod.).
        # Pokud existuje strojově čitelné XML v PDF, je to řádově spolehlivější než OCR.
        structured_cfg = (self.cfg.get("ocr", {}) or {}).get("structured_first", {}) if isinstance(self.cfg, dict) else {}
        structured_enabled = bool(structured_cfg.get("enabled", True))
        structured_pdf_enabled = bool(structured_cfg.get("enable_pdf_attachments", True))
        if structured_enabled and structured_pdf_enabled and path.suffix.lower() == ".pdf":
            try:
                ex_struct, meta = extract_structured_from_pdf(path)
                if ex_struct and (ex_struct.items or ex_struct.total_with_vat or ex_struct.doc_number):
                    try:
                        pages_count = len(PdfReader(str(path)).pages)
                    except Exception:
                        pages_count = 1
                    # vytvoř jeden chunk přes celý dokument
                    per_doc_chunks = [{
                        "page_from": 1,
                        "page_to": int(pages_count),
                        "text": "",
                        "conf": float(ex_struct.confidence or 0.99),
                        "extracted": ex_struct,
                    }]
                    text_method = "structured_pdf_attachment"
                    text_debug = {"structured_first": meta}
            except Exception:
                # když structured-first selže, pokračuj standardní OCR cestou
                pass

        if path.suffix.lower() == ".pdf" and not per_doc_chunks:
            page_texts, page_confs, pages, text_method, text_debug = self._ocr_pdf_pages(path, status_cb=status_cb)
            if not page_texts:
                # no text => hard quarantine later
                page_texts = []
                page_confs = []
                pages = 0
            # per-page audit returned by _ocr_pdf_pages
            for rec in (text_debug.get("page_audit") or []):
                page_audit_map[int(rec.get("page_no") or 0)] = dict(rec)
            per_page: List[Tuple[int, Any, str, float]] = []
            for i, t in enumerate(page_texts, start=1):
                ex = extract_from_text(t or "")
                per_page.append((i, ex, t or "", float(page_confs[i - 1] if i - 1 < len(page_confs) else 0.0)))
            # Merge multi-page invoices deterministically by key
            per_doc_chunks = self._merge_extracted_by_key(per_page)
        else:
            ocr_text, ocr_conf, pages = self._ocr_image(path, status_cb=status_cb)
            text_method = "image_ocr"
            try:
                text_debug = {"ocr": summarize_text_quality([compute_text_quality(ocr_text or "")]), "ocr_conf": float(ocr_conf or 0.0)}
            except Exception:
                text_debug = {"ocr_conf": float(ocr_conf or 0.0)}
            ex = extract_from_text(ocr_text or "")
            per_doc_chunks = [{
                "page_from": 1,
                "page_to": int(pages_count),
                "extracted": ex,
                "full_text": ocr_text or "",
                "ocr_conf": float(ocr_conf or 0.0),
                "key": None,
            }]

        out_base = Path(self.cfg["paths"]["output_dir"])
        quarantine_dir = out_base / self.cfg["paths"].get("quarantine_dir_name", "KARANTENA")

        # create file record once (1 file can contain multiple documents)
        file_record = create_file_record(
            session,
            sha256=sha,
            original_name=path.name,
            path=str(path),
            pages=int(pages or 1),
            status="NEW",
            mime_type="application/pdf" if path.suffix.lower() == ".pdf" else "image",
        )

        created_doc_ids: List[int] = []
        any_requires_review = False
        any_processed = False
        method_global = "offline"

        # per-document processing within the file
        for idx_doc, chunk in enumerate(per_doc_chunks, start=1):
            if status_cb:
                status_cb(f"Parsování dokladu {idx_doc}/{max(1, len(per_doc_chunks))}…")
            extracted = chunk["extracted"]
            ocr_conf = float(chunk.get("ocr_conf") or 0.0)
            ocr_text = chunk.get("full_text") or ""
            page_from = int(chunk.get("page_from") or 1)
            page_to = int(chunk.get("page_to") or page_from)

            method = "offline"
            reasons = list(extracted.review_reasons or [])
            doc_type = self._classify_doc_type(ocr_text)

            # Offline OCR retry: u některých skenovaných PDF je v embedded vrstvě dlouhý, ale rozbitý text,
            # který projde quality score, ale parser z něj nevytěží položky. Pokud se to stane a OCR runtime je dostupný,
            # vynuť OCR nad stránkami a zkus znovu extrakci.
            force_cfg = (self.cfg.get("ocr") or {}) if isinstance(self.cfg, dict) else {}
            force_enabled = bool(force_cfg.get("force_ocr_on_parse_failure", True))
            if force_enabled and path.suffix.lower() == ".pdf" and self.ocr_engine is not None:
                try:
                    used_embedded = True
                    for pno in range(page_from, page_to + 1):
                        rec = page_audit_map.get(int(pno))
                        if rec and str(rec.get("chosen_mode") or "") != "embedded":
                            used_embedded = False
                            break
                    parse_missing_items = (len(list(extracted.items or [])) == 0)
                    parse_missing_core = (not extracted.issue_date) or (extracted.total_with_vat is None)
                    should_retry = parse_missing_items or (doc_type != "receipt" and parse_missing_core)
                    if used_embedded and should_retry and getattr(self.ocr_engine, "is_available", lambda: False)():
                        ocr_retry_text, ocr_retry_conf = self._force_ocr_pdf_range(path, page_from, page_to, status_cb=status_cb)
                        if ocr_retry_text:
                            ex2 = extract_from_text(ocr_retry_text)
                            # vyber "lepší" výsledek: více položek + více základních polí
                            def _score_ex(ex):
                                return (
                                    len(list(ex.items or [])),
                                    1 if ex.doc_number else 0,
                                    1 if (ex.issue_date and (ex.total_with_vat is not None)) else 0,
                                )
                            if _score_ex(ex2) > _score_ex(extracted):
                                extracted = ex2
                                ocr_text = ocr_retry_text
                                ocr_conf = float(ocr_retry_conf or 0.0)
                                doc_type = self._classify_doc_type(ocr_text)
                                reasons = list(extracted.review_reasons or [])
                                reasons.append("offline OCR retry: nahrazen embedded text (parser měl málo dat)")
                except Exception:
                    pass

            # Offline ensemble: zkus více OCR metod (různá DPI, rotace, rekonstrukce řádků) a vyber nejlepší výsledek.
            # Záměrně běží před heuristikami (pseudo-IČO, syntetické číslo), aby se vybíral "skutečně nejlepší" text.
            try:
                best_text, best_ex, ens_debug = self._offline_ensemble_best(
                    path=path,
                    page_from=page_from,
                    page_to=page_to,
                    baseline_text=ocr_text,
                    baseline_extracted=extracted,
                    status_cb=status_cb,
                )
                if best_ex is not None and best_text:
                    extracted = best_ex
                    ocr_text = best_text
                    # ensemble text se typicky opírá o OCR, takže konfidence jen orientačně
                    extracted.full_text = ocr_text
                    doc_type = self._classify_doc_type(ocr_text)
                    reasons = list(extracted.review_reasons or [])
                    reasons.append("offline ensemble: vybrán lepší OCR kandidát")
                    if isinstance(ens_debug, dict):
                        text_debug.setdefault("ensemble", ens_debug)
                    method = "offline_ensemble"
                    method_global = "offline_ensemble"
            except Exception:
                pass

            # OpenAI primary: pokud je API klic a povoleni, zkus online extrakci
            
            # QR Platba (SPAYD): pokud doklad obsahuje QR, umí doplnit účet/částku/měnu a zvýšit jistotu.
            try:
                need_qr = (extracted.bank_account is None) or (extracted.total_with_vat is None)
                if need_qr:
                    qr_payload: dict[str, Any] = {}
                    if path.suffix.lower() == ".pdf":
                        dpi = int(max(300, (ocr_cfg.get("pdf_dpi") or 200)))
                        imgs = render_pdf_to_images(path, dpi=dpi, max_pages=(page_to - page_from + 1), start_page=max(0, page_from - 1))
                    else:
                        imgs = [Image.open(path)]
                    for img in imgs:
                        qr_payload = self._try_qr_spayd_from_image(img)
                        if qr_payload:
                            break
                    if qr_payload:
                        self._augment_extracted_with_qr(extracted, qr_payload)
            except Exception:
                pass

            # Dodatečné validace (např. IBAN checksum)
            self._validate_extracted(extracted)

            openai_cfg = self.cfg.get("openai") if isinstance(self.cfg, dict) else {}
            if not isinstance(openai_cfg, dict):
                openai_cfg = {}
            api_key = str(openai_cfg.get("api_key") or os.getenv("KAJOVOSPEND_OPENAI_API_KEY", "")).strip()
            auto_enable = bool(openai_cfg.get("auto_enable", True))
            features = (self.cfg.get("features") or {}) if isinstance(self.cfg, dict) else {}
            openai_feature_enabled = bool((features.get("openai_fallback", {}) or {}).get("enabled", False))
            openai_enabled = bool(openai_feature_enabled and (OpenAIConfig is not None) and api_key and (openai_cfg.get("enabled") or auto_enable))
            primary_enabled = bool(openai_cfg.get("primary_enabled", True))
            openai_model = str(openai_cfg.get("model") or "auto").strip() or "auto"
            fallback_model = str(openai_cfg.get("fallback_model") or "").strip() or None
            use_json_schema = bool(openai_cfg.get("use_json_schema", True))
            temperature = float(openai_cfg.get("temperature", 0.0) or 0.0)
            max_output_tokens = int(openai_cfg.get("max_output_tokens", 2000) or 2000)
            openai_timeout = int(openai_cfg.get("timeout_sec", 60) or 60)
            openai_used_model: str | None = None
            openai_raw_response: str | None = None
            openai_method: str | None = None

            if openai_enabled and primary_enabled:
                need_primary = (
                    (doc_type == "receipt")
                    or (len(list(extracted.items or [])) == 0)
                    or (extracted.total_with_vat is None)
                    or (extracted.issue_date is None)
                )
                if need_primary:
                    try:
                        if status_cb:
                            status_cb("OpenAI: extrahuji data...")
                        images_payload = self._prepare_openai_images(
                            path,
                            page_from=page_from,
                            page_to=page_to,
                        )
                        cfg = OpenAIConfig(
                            api_key=api_key,
                            model=openai_model,
                            fallback_model=fallback_model,
                            use_json_schema=use_json_schema,
                            temperature=temperature,
                            max_output_tokens=max_output_tokens,
                        )
                        obj, raw, used_model = extract_with_openai(
                            cfg,
                            ocr_text=ocr_text,
                            images=images_payload,
                            timeout=openai_timeout,
                        )
                        if isinstance(obj, dict):
                            merged = self._merge_openai_result(extracted, obj, prefer_items=True)
                            if merged:
                                reasons.append("doplneno pres OpenAI (primary)")
                            method = "openai_primary"
                            method_global = "openai_primary"
                            openai_used_model = used_model
                            openai_raw_response = raw
                            openai_method = "openai_primary"
                        else:
                            reasons.append("OpenAI primary nevratil validni JSON")
                    except Exception as e:
                        reasons.append(f"OpenAI primary selhal: {e}")

            # Pokud chybí IČO, zkus heuristiku: najít 8-místné číslo a ověřit v ARES.
            if not extracted.supplier_ico:
                guessed_ico = self._guess_supplier_ico_from_text(ocr_text)
                if guessed_ico:
                    extracted.supplier_ico = guessed_ico
                    extracted.confidence = float(max(extracted.confidence or 0.0, 0.80))
                    reasons.append("IČO doplněno heuristikou (ARES validace)")


            # Pseudo-IČO jen pro účtenky (u faktur by to zbytečně pouštělo falešné "complete")
            if (doc_type == "receipt") and (not extracted.supplier_ico):
                supplier_name_guess = self._extract_supplier_name_guess(ocr_text) or "NEZNAMY_DODAVATEL"
                extracted.supplier_ico = self._pseudo_ico(supplier_name_guess)
                extracted.confidence = float(max(extracted.confidence or 0.0, 0.75))
                reasons.append("IČO není na dokladu: použito pseudo-ID dodavatele")

            # Pro účtenky: syntetické číslo dokladu, pokud chybí.
            if (doc_type == "receipt") and (not extracted.doc_number):
                extracted.doc_number = self._synthetic_doc_number(
                    sha256=sha,
                    page_from=page_from,
                    page_to=page_to,
                    issue_date=extracted.issue_date,
                    total_with_vat=extracted.total_with_vat,
                )
                reasons.append("chybí číslo dokladu: použito syntetické číslo (receipts)")

            # Po offline i OpenAI: kanonizace položek (unit_price bez DPH, line_total s DPH) + kontrola součtu.
            items_ref = list(extracted.items or [])
            sum_ok, reasons = postprocess_items_for_db(
                items=items_ref,
                total_with_vat=extracted.total_with_vat,
                reasons=reasons,
            )
            extracted.items = items_ref
            extracted.review_reasons = reasons

            # OpenAI fallback: pokud stale chybi klicova data, zkus druhe kolo s prisnejsim promptem
            need_openai = False
            if doc_type == "receipt":
                need_openai = (len(list(extracted.items or [])) == 0) or (extracted.total_with_vat is None)
            else:
                need_openai = not bool(
                    extracted.supplier_ico
                    and extracted.doc_number
                    and extracted.issue_date
                    and (extracted.total_with_vat is not None)
                    and (len(list(extracted.items or [])) > 0)
                    and sum_ok
                )

            if openai_enabled and need_openai:
                try:
                    if status_cb:
                        status_cb("OpenAI fallback: doplnuji polozky...")
                    images_payload = self._prepare_openai_images(
                        path,
                        page_from=page_from,
                        page_to=page_to,
                    )
                    cfg = OpenAIConfig(
                        api_key=api_key,
                        model=openai_model,
                        fallback_model=fallback_model,
                        use_json_schema=use_json_schema,
                        temperature=temperature,
                        max_output_tokens=max_output_tokens,
                    )
                    obj, raw, used_model = extract_with_openai_fallback(
                        cfg,
                        ocr_text=ocr_text,
                        images=images_payload,
                        timeout=openai_timeout,
                    )
                    if isinstance(obj, dict):
                        merged = self._merge_openai_result(extracted, obj, prefer_items=True)
                        if merged:
                            reasons.append("doplněno pres OpenAI fallback")
                        method = "openai_fallback"
                        method_global = "openai_fallback"
                        openai_used_model = used_model
                        openai_raw_response = raw
                        openai_method = "openai_fallback"
                    else:
                        reasons.append("OpenAI fallback nevratil validni JSON")
                except Exception as e:
                    reasons.append(f"OpenAI fallback selhal: {e}")

                # Po OpenAI znovu normalizace polozek a kontrola souctu
                items_ref = list(extracted.items or [])
                sum_ok, reasons = postprocess_items_for_db(
                    items=items_ref,
                    total_with_vat=extracted.total_with_vat,
                    reasons=reasons,
                )
                extracted.items = items_ref
                extracted.review_reasons = reasons

# Pro účtenky: pokud nemáme položky, vytvoř syntetickou z total.
            allow_synthetic = bool(openai_cfg.get("allow_synthetic_items", False))
            if allow_synthetic and (doc_type == "receipt") and (len(list(extracted.items or [])) == 0) and (extracted.total_with_vat is not None):
                try:
                    total_f = float(extracted.total_with_vat)
                    extracted.items = [
                        {
                            "name": "Nakup",
                            "quantity": 1.0,
                            "unit_price": total_f,
                            "vat_rate": 0.0,
                            "line_total": total_f,
                        }
                    ]
                    sum_ok = True
                    reasons.append("chybi polozky -> vytvorena synteticka polozka z total")
                except Exception:
                    pass

            # Povinné minimum pro přesun do OUT:
            # - IČO, číslo dokladu, datum, total (vč. DPH)
            # - položky
            # - součet položek (včetně zaokrouhlení) sedí na total v toleranci
            if doc_type == "receipt":
                # Prisnejsi rezim: uctenka musi mit polozky i soucet.
                complete = bool(
                    extracted.supplier_ico
                    and extracted.doc_number
                    and extracted.issue_date
                    and (extracted.total_with_vat is not None)
                    and (len(list(extracted.items or [])) > 0)
                    and sum_ok
                )
                reasons = self._prune_receipt_reasons(reasons)
            else:
                # Faktura: původní striktní pravidla.
                complete = bool(
                    extracted.supplier_ico
                    and extracted.doc_number
                    and extracted.issue_date
                    and (extracted.total_with_vat is not None)
                    and (len(list(extracted.items or [])) > 0)
                    and sum_ok
                )

            requires_review = bool((not complete) or (ocr_conf < min_conf))
            if ocr_conf < min_conf:
                reasons.append("nízká jistota OCR")
            if not complete:
                reasons.append("nekompletní vytěžení")

            # zapiš zpět pro UI/DB konzistenci
            extracted.requires_review = requires_review
            # de-dup důvodů, stabilní pořadí
            extracted.review_reasons = list(dict.fromkeys(reasons))

            # Pouze plně kompletní doklady smí vstoupit do DB (dodavatelé/doklady/položky).
            is_complete = bool(not requires_review)
            if not is_complete:
                any_requires_review = True
                try:
                    file_record.last_error = "; ".join(dict.fromkeys(reasons)) if reasons else "nekompletní vytěžení"
                    session.add(file_record)
                except Exception:
                    pass
                continue

            # Business duplicita per-doc
            if extracted.supplier_ico and extracted.doc_number and extracted.issue_date:
                try:
                    dup = session.execute(
                        text(
                            "SELECT id FROM documents "
                            "WHERE supplier_ico = :ico AND doc_number = :dn AND issue_date = :d "
                            "LIMIT 1"
                        ),
                        {"ico": extracted.supplier_ico, "dn": extracted.doc_number, "d": extracted.issue_date},
                    ).fetchone()
                    if dup:
                        dup_dir = out_base / self.cfg["paths"].get("duplicate_dir_name", "DUPLICITY")
                        moved = safe_move(path, dup_dir, path.name)
                        file_record.current_path = str(moved)
                        file_record.status = "DUPLICATE"
                        file_record.processed_at = dt.datetime.utcnow()
                        session.add(file_record)
                        session.flush()
                        return {
                            "status": "DUPLICATE",
                            "sha256": sha,
                            "file_id": file_record.id,
                            "moved_to": str(moved),
                            "duplicate_of_document_id": int(dup[0]),
                            "text_method": text_method,
                            "text_debug": text_debug,
                        }
                except Exception as e:
                    reasons.append(f"dup-check selhal: {e}")
                    requires_review = True

            last_error_msg = "; ".join(dict.fromkeys(reasons)) if reasons else "nekompletní vytěžení"
            if requires_review:
                any_requires_review = True
                try:
                    file_record.last_error = last_error_msg
                    session.add(file_record)
                except Exception:
                    pass
                continue

            supplier_id = None
            supplier_name_guess: str | None = None
            if extracted.supplier_ico:
                # ARES voláme jen pro "skutečné" IČO (číslice). Pro pseudo-IČO nic přes síť neřešíme.
                if self._looks_like_ico(extracted.supplier_ico):
                    try:
                        extracted.supplier_ico = normalize_ico(extracted.supplier_ico)
                    except Exception:
                        pass
                    try:
                        if status_cb:
                            status_cb("ARES: doplňuji dodavatele…")
                        ares = fetch_by_ico(extracted.supplier_ico)
                        s = upsert_supplier(
                            session,
                            ares.ico,
                            name=ares.name,
                            dic=ares.dic,
                            address=ares.address,
                            is_vat_payer=ares.is_vat_payer,
                            ares_last_sync=ares.fetched_at,
                            legal_form=ares.legal_form,
                            street=ares.street,
                            street_number=ares.street_number,
                            orientation_number=ares.orientation_number,
                            city=ares.city,
                            zip_code=ares.zip_code,
                            overwrite=True,
                        )
                        supplier_id = s.id
                        extracted.supplier_ico = ares.ico
                    except Exception as e:
                        reasons.append(f"ARES selhal: {e}")
                        # ARES je pouze enrich krok; při výpadku sítě neblokujeme uložení dokladu.
                        # Zachováme IČO z extrakce a vytvoříme/aktualizujeme lokálního dodavatele best-effort.
                        try:
                            s = upsert_supplier(
                                session,
                                str(extracted.supplier_ico),
                                name=self._extract_supplier_name_guess(ocr_text),
                                overwrite=False,
                            )
                            supplier_id = s.id
                        except Exception:
                            pass
                else:
                    # Pseudo-IČO: uložíme dodavatele lokálně jen se jménem (pokud ho umíme odhadnout).
                    supplier_name_guess = self._extract_supplier_name_guess(ocr_text)
                    s = upsert_supplier(
                        session,
                        str(extracted.supplier_ico),
                        name=supplier_name_guess,
                        overwrite=False,
                    )
                    supplier_id = s.id

            last_error_msg = "; ".join(dict.fromkeys(reasons)) if reasons else "nekompletní vytěžení"
            if requires_review:
                any_requires_review = True
                try:
                    file_record.last_error = last_error_msg
                    session.add(file_record)
                except Exception:
                    pass
                continue

            doc = add_document(
                session,
                file_id=file_record.id,
                supplier_id=supplier_id,
                supplier_ico=extracted.supplier_ico,
                doc_number=extracted.doc_number,
                bank_account=extracted.bank_account,
                issue_date=extracted.issue_date,
                total_with_vat=extracted.total_with_vat,
                currency=extracted.currency,
                confidence=float(extracted.confidence),
                method=method,
                requires_review=requires_review,
                review_reasons="; ".join(reasons) if reasons else None,
                items=extracted.items,
                page_from=page_from,
                page_to=page_to,
            )
            if openai_used_model:
                doc.openai_model = str(openai_used_model)
                doc.openai_raw_response = openai_raw_response
                session.add(doc)
            # Persist aggregated document text quality (if available)
            try:
                # default to file-level aggregation if present; still useful
                dtq = float(text_debug.get("document_text_quality") or 0.0)
                setattr(doc, "document_text_quality", dtq)
                session.add(doc)
            except Exception:
                pass

            # Persist per-page audit rows for the pages belonging to this document chunk
            try:
                from kajovospend.db.models import DocumentPageAudit
                for pno in range(int(page_from), int(page_to) + 1):
                    rec = page_audit_map.get(int(pno))
                    if not rec:
                        continue
                    session.add(DocumentPageAudit(
                        document_id=int(doc.id),
                        file_id=int(file_record.id),
                        page_no=int(pno),
                        chosen_mode=str(rec.get("chosen_mode") or "embedded"),
                        chosen_score=float(rec.get("chosen_score") or 0.0),
                        embedded_score=float(rec.get("embedded_score") or 0.0),
                        ocr_score=float(rec.get("ocr_score") or 0.0),
                        embedded_len=int(rec.get("embedded_len") or 0),
                        ocr_len=int(rec.get("ocr_len") or 0),
                        ocr_conf=float(rec.get("ocr_conf") or 0.0),
                        token_groups=int(rec.get("token_groups") or 0),
                    ))
                session.flush()
            except Exception as e:
                # never fail extraction due to audit insert
                self.log.warning(f"page_audit insert failed: {e}")
            rebuild_fts_for_document(session, doc.id, extracted.full_text if hasattr(extracted, "full_text") else ocr_text)
            created_doc_ids.append(int(doc.id))
            any_requires_review = bool(any_requires_review or requires_review)
            any_processed = True

        # Move file once (based on any_requires_review)
        out_base = Path(self.cfg["paths"]["output_dir"])
        quarantine_dir = out_base / self.cfg["paths"].get("quarantine_dir_name", "KARANTENA")
        if any_requires_review or (not any_processed):
            moved = safe_move(path, quarantine_dir, path.name)
            status = "QUARANTINE"
        else:
            moved = safe_move(path, out_base, path.name)
            status = "PROCESSED"

        file_record.current_path = str(moved)
        file_record.status = status
        file_record.processed_at = dt.datetime.utcnow()
        session.add(file_record)
        session.flush()

        return {
            "status": status,
            "sha256": sha,
            "file_id": file_record.id,
            "document_ids": created_doc_ids,
            "moved_to": str(moved),
            "text_method": text_method,
            "text_debug": text_debug,
        }
