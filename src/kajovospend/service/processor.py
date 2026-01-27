from __future__ import annotations

import datetime as dt
import shutil
import re
import hashlib
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

from PIL import Image
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
from kajovospend.integrations.ares import fetch_by_ico, normalize_ico
from kajovospend.ocr.pdf_render import render_pdf_to_images
from kajovospend.ocr.rapidocr_engine import RapidOcrEngine
from kajovospend.utils.hashing import sha256_file
from kajovospend.utils.text_quality import compute_text_quality, summarize_text_quality


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
    shutil.move(str(src), str(dst))
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
        quality_threshold = float(ocr_cfg.get("page_text_quality_threshold", 0.45))
        min_non_ws = int(ocr_cfg.get("page_text_min_non_ws", 25))
        score_margin = float(ocr_cfg.get("page_text_score_margin", 0.02))

        def _metrics(txt: str) -> Dict[str, Any]:
            t = txt or ""
            total = len(t)
            non_ws = len(re.sub(r"\s+", "", t))
            letters = len(re.findall(r"[A-Za-zÁČĎÉĚÍŇÓŘŠŤÚŮÝŽáčďéěíňóřšťúůýž]", t))
            digits = len(re.findall(r"\d", t))
            bad = t.count("\ufffd") + len(re.findall(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", t))
            lines_nonempty = len([ln for ln in t.splitlines() if ln.strip()])
            return {
                "total": total,
                "non_ws": non_ws,
                "letters": letters,
                "digits": digits,
                "bad": bad,
                "lines_nonempty": lines_nonempty,
            }

        def _score(m: Dict[str, Any]) -> float:
            total = int(m.get("total") or 0)
            non_ws = int(m.get("non_ws") or 0)
            if non_ws <= 0 or total <= 0:
                return 0.0
            letters = int(m.get("letters") or 0)
            digits = int(m.get("digits") or 0)
            bad = int(m.get("bad") or 0)
            density = min(1.0, non_ws / 400.0)
            alnum_ratio = (letters + digits) / max(1, non_ws)
            bad_ratio = bad / max(1, total)
            s = 0.55 * density + 0.55 * alnum_ratio - 0.80 * bad_ratio
            if s < 0.0:
                return 0.0
            if s > 1.0:
                return 1.0
            return float(s)

        def _conf_from_embedded(m: Dict[str, Any], s: float) -> float:
            non_ws = int(m.get("non_ws") or 0)
            if non_ws <= 0:
                return 0.0
            conf = 0.50 + 0.45 * float(s)
            if conf < 0.0:
                conf = 0.0
            if conf > 0.95:
                conf = 0.95
            return float(conf)

        if status_cb:
            status_cb("PDF: čtu text (hybrid embedded/OCR)…")

        try:
            reader = PdfReader(str(pdf_path))
            embedded_texts: List[str] = []
            embedded_metrics: List[Dict[str, Any]] = []
            embedded_scores: List[float] = []
            for page in reader.pages:
                t = page.extract_text() or ""
                embedded_texts.append(t)
                m = _metrics(t)
                embedded_metrics.append(m)
                embedded_scores.append(_score(m))
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
            embedded_metrics = []
            embedded_scores = []
            n_pages = 0
            text_debug["embedded_error"] = str(e)

        if n_pages <= 0 and self.ocr_engine is None:
            text_debug["reason"] = "no_embedded_no_ocr"
            text_debug["method"] = "none"
            return [], [], 0, "none", text_debug

        needs_ocr: List[bool] = []
        if n_pages > 0:
            for i in range(n_pages):
                m = embedded_metrics[i]
                s = embedded_scores[i]
                weak = (int(m.get("non_ws") or 0) < min_non_ws) or (float(s) < quality_threshold)
                needs_ocr.append(bool(weak))

        out_texts: List[str] = list(embedded_texts) if embedded_texts else []
        out_confs: List[float] = []
        if embedded_texts:
            for i in range(n_pages):
                out_confs.append(_conf_from_embedded(embedded_metrics[i], embedded_scores[i]))

        if embedded_texts:
            weak_cnt = sum(1 for x in needs_ocr if x)
            text_debug["weak_pages"] = weak_cnt
            self.log.info(
                f"pdf_text_source: pages={n_pages} weak_pages={weak_cnt} "
                f"threshold={quality_threshold:.2f} min_non_ws={min_non_ws}"
            )

        if self.ocr_engine is None:
            if embedded_texts:
                text_debug["method"] = "embedded"
                for i in range(n_pages):
                    m = embedded_metrics[i]
                    s = embedded_scores[i]
                    chosen = "embedded"
                    why = "weak_embedded_but_no_ocr" if needs_ocr[i] else "embedded_ok"
                    self.log.info(
                        f"pdf_text_source page={i+1}/{n_pages} chosen={chosen} why={why} "
                        f"emb_score={s:.3f} emb_nonws={int(m.get('non_ws') or 0)} emb_bad={int(m.get('bad') or 0)}"
                    )
                text_method = "embedded"
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
                m = _metrics(t or "")
                s = _score(m)
                self.log.info(
                    f"pdf_text_source page={idx_page}/{len(images)} chosen=ocr why=no_embedded "
                    f"ocr_score={s:.3f} ocr_nonws={int(m.get('non_ws') or 0)} ocr_bad={int(m.get('bad') or 0)} "
                    f"ocr_conf={float(c or 0.0):.3f}"
                )
            text_debug["method"] = text_method
            text_debug["ocr_conf_avg"] = float(sum(confs2) / len(confs2)) if confs2 else 0.0
            text_debug["ocr_conf_min"] = float(min(confs2)) if confs2 else 0.0
            text_debug["ocr_conf_max"] = float(max(confs2)) if confs2 else 0.0
            ocr_summary = summarize_text_quality([compute_text_quality(t) for t in texts2])
            text_debug["ocr"] = ocr_summary
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
                m = embedded_metrics[i]
                s = embedded_scores[i]
                self.log.info(
                    f"pdf_text_source page={i+1}/{n_pages} chosen=embedded why=embedded_ok "
                    f"emb_score={s:.3f} emb_nonws={int(m.get('non_ws') or 0)} emb_bad={int(m.get('bad') or 0)}"
                )
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

                emb_m = embedded_metrics[page_idx]
                emb_s = embedded_scores[page_idx]
                ocr_m = _metrics(ocr_text)
                ocr_s = _score(ocr_m)

                choose_ocr = bool(ocr_s > (emb_s + score_margin))
                if choose_ocr:
                    out_texts[page_idx] = ocr_text
                    out_confs[page_idx] = ocr_conf_f
                    chosen = "ocr"
                    why = "embedded_weak_or_worse"
                else:
                    chosen = "embedded"
                    why = "ocr_not_better"

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
                self.log.info(
                    f"pdf_text_source page={page_idx+1}/{n_pages} chosen={chosen} why={why} "
                    f"emb_score={emb_s:.3f} emb_nonws={int(emb_m.get('non_ws') or 0)} emb_bad={int(emb_m.get('bad') or 0)} "
                    f"ocr_score={ocr_s:.3f} ocr_nonws={int(ocr_m.get('non_ws') or 0)} ocr_bad={int(ocr_m.get('bad') or 0)} "
                    f"ocr_conf={ocr_conf_f:.3f}"
                )

        text_debug["method"] = text_method
        text_debug["selections"] = selections
        return out_texts, out_confs, n_pages, text_method, text_debug

    def _merge_extracted_by_key(self, per_page: List[Tuple[int, Any, str, float]]) -> List[Dict[str, Any]]:
        """
        per_page: [(page_no, Extracted, full_text, ocr_conf), ...]
        Sloučí sousední stránky do 1 dokladu, pokud sedí klíč:
          (supplier_ico, doc_number, issue_date)
        Když klíč není kompletní, neslučuje (bezpečnější deterministické chování).
        """
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
            # merge only if both have complete key and keys match and pages are consecutive
            if key_ok and cur.get("key") is not None and cur["key"] == key and page_no == int(cur["page_to"]) + 1:
                cur["page_to"] = page_no
                # merge items + text
                try:
                    cur_ex = cur["extracted"]
                    cur_ex.items = list(cur_ex.items or []) + list(ex.items or [])
                    # keep "best" confidence; requires_review if any says review
                    cur_ex.confidence = float(max(cur_ex.confidence or 0.0, ex.confidence or 0.0))
                    cur_ex.requires_review = bool(cur_ex.requires_review or ex.requires_review)
                    cur_ex.review_reasons = list(dict.fromkeys((cur_ex.review_reasons or []) + (ex.review_reasons or [])))
                    # total_with_vat: keep first non-null (multi-page invoice often repeats totals only at end)
                    if cur_ex.total_with_vat is None and ex.total_with_vat is not None:
                        cur_ex.total_with_vat = ex.total_with_vat
                    cur["extracted"] = cur_ex
                except Exception:
                    pass
                cur["full_text"] = (cur["full_text"] + "\n\n" + (full_text or "")).strip()
                cur["ocr_conf"] = float(sum([cur["ocr_conf"], float(ocr_conf or 0.0)]) / 2.0)
            else:
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
        """U účtenek tolerujeme chybějící položky / sum_ok a syntetické číslo -> ořez zbytečných důvodů."""
        drop = {
            "nekompletní vytěžení",
            "chybí číslo dokladu",
            "chybí položky",
        }
        out: List[str] = []
        for r in reasons or []:
            if r in drop:
                continue
            # toleruj i typické hlášky o součtu (necháváme v logu parseru, ale nemá to zvedat review)
            if "nesedí součet" in str(r).lower():
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

        if path.suffix.lower() == ".pdf":
            page_texts, page_confs, pages, text_method, text_debug = self._ocr_pdf_pages(path, status_cb=status_cb)
            if not page_texts:
                # no text => hard quarantine later
                page_texts = []
                page_confs = []
                pages = 0
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
                "page_to": 1,
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

            # Pro účtenky: pokud nemáme položky, vytvoř syntetickou z total.
            if (doc_type == "receipt") and (len(list(extracted.items or [])) == 0) and (extracted.total_with_vat is not None):
                try:
                    total_f = float(extracted.total_with_vat)
                    extracted.items = [
                        {
                            "name": "Nákup",
                            "quantity": 1.0,
                            "unit_price": total_f,
                            "vat_rate": 0.0,
                            "line_total": total_f,
                        }
                    ]
                    sum_ok = True
                    reasons.append("chybí položky -> vytvořena syntetická položka z total")
                except Exception:
                    pass

            # Povinné minimum pro přesun do OUT:
            # - IČO, číslo dokladu, datum, total (vč. DPH)
            # - položky
            # - součet položek (včetně zaokrouhlení) sedí na total v toleranci
            if doc_type == "receipt":
                # Benevolentnější režim: sum_ok nevyžadujeme; položky mohou být syntetické.
                complete = bool(
                    extracted.supplier_ico
                    and extracted.doc_number
                    and extracted.issue_date
                    and (extracted.total_with_vat is not None)
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
                        requires_review = True
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
