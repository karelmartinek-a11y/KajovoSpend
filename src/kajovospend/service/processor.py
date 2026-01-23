from __future__ import annotations

import datetime as dt
import shutil
import io
import re
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
from kajovospend.extract.parser import extract_from_text
from kajovospend.integrations.ares import fetch_by_ico, normalize_ico
from kajovospend.integrations.openai_fallback import OpenAIConfig, extract_with_openai
from kajovospend.ocr.pdf_render import render_pdf_to_images
from kajovospend.ocr.rapidocr_engine import RapidOcrEngine
from kajovospend.utils.hashing import sha256_file


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

    def _openai_images_for_path(self, path: Path) -> List[Tuple[str, bytes]]:
        """
        Připraví obrazové vstupy pro OpenAI fallback.
        - PDF: render první 1-2 strany do PNG (rozumný kompromis cena/výkon).
        - image: vezme bytes přímo (png/jpg/webp).
        """
        images: List[Tuple[str, bytes]] = []
        try:
            suf = path.suffix.lower()
            if suf == ".pdf":
                dpi = int(self.cfg["ocr"].get("pdf_dpi", 200))
                pages = render_pdf_to_images(path, dpi=dpi)
                for img in pages[:2]:
                    buf = io.BytesIO()
                    # PNG je robustní pro text/čáry; optimalize kvůli velikosti
                    img.save(buf, format="PNG", optimize=True)
                    images.append(("image/png", buf.getvalue()))
            else:
                mime = "image/png"
                if suf in (".jpg", ".jpeg"):
                    mime = "image/jpeg"
                elif suf == ".webp":
                    mime = "image/webp"
                data = path.read_bytes()
                if data:
                    images.append((mime, data))
        except Exception as e:
            self.log.debug(f"OpenAI image prep failed ({path.name}): {e}")
        return images

    def _ocr_pdf_pages(self, pdf_path: Path) -> Tuple[List[str], List[float], int]:
        """
        Vrátí OCR text po jednotlivých stránkách:
        - embedded text: per page extract_text()
        - jinak: render->OCR per page
        """
        # Try embedded text first (page-by-page)
        try:
            reader = PdfReader(str(pdf_path))
            texts: List[str] = []
            for page in reader.pages:
                t = page.extract_text() or ""
                texts.append(t)
            # if at least one page has real text, treat as embedded text mode
            if any((t or "").strip() for t in texts):
                confs = [0.95 if (t or "").strip() else 0.0 for t in texts]
                return texts, confs, len(texts)
        except Exception:
            pass

        # fallback to image OCR
        if self.ocr_engine is None:
            return [], [], 0
        images = render_pdf_to_images(pdf_path, dpi=int(self.cfg["ocr"].get("pdf_dpi", 200)))
        texts2: List[str] = []
        confs2: List[float] = []
        for img in images:
            t, c = self.ocr_engine.image_to_text(img)
            texts2.append(t or "")
            confs2.append(float(c or 0.0))
        return texts2, confs2, len(images)

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

    def _ocr_image(self, path: Path) -> Tuple[str, float, int]:
        if self.ocr_engine is None:
            return "", 0.0, 1
        with Image.open(path) as img:
            t, c = self.ocr_engine.image_to_text(img)
        return t, c, 1

    def process_path(self, session, path: Path) -> Dict[str, Any]:
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
            return {"status": "DUPLICATE", "sha256": sha, "moved_to": str(moved)}

        # OCR
        min_conf = float(self.cfg["ocr"].get("min_confidence", 0.65))
        pages = 1
        per_doc_chunks: List[Dict[str, Any]] = []

        if path.suffix.lower() == ".pdf":
            page_texts, page_confs, pages = self._ocr_pdf_pages(path)
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
            ocr_text, ocr_conf, pages = self._ocr_image(path)
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
        for chunk in per_doc_chunks:
            extracted = chunk["extracted"]
            ocr_conf = float(chunk.get("ocr_conf") or 0.0)
            ocr_text = chunk.get("full_text") or ""
            page_from = int(chunk.get("page_from") or 1)
            page_to = int(chunk.get("page_to") or page_from)

            method = "offline"
            reasons = list(extracted.review_reasons or [])

            # OpenAI fallback: for PDF, it’s file-level images; OK as a first deterministic step
            if (extracted.requires_review or extracted.confidence < 0.75) and self.cfg.get("openai", {}).get("enabled"):
                api_key = str(self.cfg["openai"].get("api_key") or "").strip()
                model = str(self.cfg["openai"].get("model") or "").strip()
                if api_key and model:
                    try:
                        imgs = self._openai_images_for_path(path)
                        obj, raw = extract_with_openai(
                            OpenAIConfig(api_key=api_key, model=model),
                            ocr_text,
                            images=imgs if imgs else None,
                        )
                        if obj:
                            extracted.supplier_ico = obj.get("supplier_ico") or extracted.supplier_ico
                            extracted.doc_number = obj.get("doc_number") or extracted.doc_number
                            extracted.bank_account = obj.get("bank_account") or extracted.bank_account
                            if obj.get("issue_date"):
                                try:
                                    extracted.issue_date = dt.date.fromisoformat(obj["issue_date"])
                                except Exception:
                                    pass
                            if obj.get("total_with_vat") is not None:
                                try:
                                    extracted.total_with_vat = float(obj["total_with_vat"])
                                except Exception:
                                    pass
                            if obj.get("currency"):
                                extracted.currency = str(obj.get("currency"))
                            if obj.get("items"):
                                extracted.items = list(obj.get("items"))
                            extracted.confidence = max(extracted.confidence, 0.85)
                            extracted.requires_review = False
                            extracted.review_reasons = [r for r in extracted.review_reasons if r != "nízká jistota vytěžení"]
                            method = "openai"
                            method_global = "openai"
                    except Exception as e:
                        self.log.warning(f"OpenAI fallback failed: {e}")

            # Decide quarantine for this extracted doc
            requires_review = bool(extracted.requires_review or (ocr_conf < min_conf))
            if ocr_conf < min_conf:
                reasons.append("nízká jistota OCR")

            supplier_id = None
            if extracted.supplier_ico:
                try:
                    extracted.supplier_ico = normalize_ico(extracted.supplier_ico)
                except Exception:
                    pass
                try:
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
        }
