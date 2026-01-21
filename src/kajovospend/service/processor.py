from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

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
from kajovospend.integrations.ares import fetch_by_ico
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

    def _ocr_pdf(self, pdf_path: Path) -> Tuple[str, float, int]:
        # Try embedded text first
        text_parts = []
        try:
            reader = PdfReader(str(pdf_path))
            for page in reader.pages:
                t = page.extract_text() or ""
                if t.strip():
                    text_parts.append(t)
        except Exception:
            pass
        if text_parts:
            txt = "\n".join(text_parts)
            # treat embedded text as high confidence
            return txt, 0.95, len(reader.pages)
        # fallback to image OCR
        if self.ocr_engine is None:
            return "", 0.0, 0
        images = render_pdf_to_images(pdf_path, dpi=int(self.cfg["ocr"].get("pdf_dpi", 200)))
        texts = []
        confs = []
        for img in images:
            t, c = self.ocr_engine.image_to_text(img)
            if t.strip():
                texts.append(t)
                confs.append(c)
        if not texts:
            return "", 0.0, len(images)
        return "\n".join(texts), float(sum(confs) / max(len(confs), 1)), len(images)

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
        ocr_text = ""
        ocr_conf = 0.0
        pages = 1
        if path.suffix.lower() == ".pdf":
            ocr_text, ocr_conf, pages = self._ocr_pdf(path)
        else:
            ocr_text, ocr_conf, pages = self._ocr_image(path)

        min_conf = float(self.cfg["ocr"].get("min_confidence", 0.65))
        extracted = extract_from_text(ocr_text)

        method = "offline"
        if extracted.confidence < 0.75 and self.cfg.get("openai", {}).get("enabled"):
            api_key = str(self.cfg["openai"].get("api_key") or "").strip()
            model = str(self.cfg["openai"].get("model") or "").strip()
            if api_key and model:
                try:
                    obj, raw = extract_with_openai(OpenAIConfig(api_key=api_key, model=model), ocr_text)
                    if obj:
                        # map obj to extracted
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
                except Exception as e:
                    self.log.warning(f"OpenAI fallback failed: {e}")

        # Decide quarantine
        requires_review = extracted.requires_review or (ocr_conf < min_conf)
        reasons = list(extracted.review_reasons)
        if ocr_conf < min_conf:
            reasons.append("nízká jistota OCR")

        status = "PROCESSED"
        out_base = Path(self.cfg["paths"]["output_dir"])
        quarantine_dir = out_base / self.cfg["paths"].get("quarantine_dir_name", "KARANTENA")

        file_record = create_file_record(
            session,
            sha256=sha,
            original_name=path.name,
            path=str(path),
            pages=pages,
            status="NEW",
            mime_type="application/pdf" if path.suffix.lower() == ".pdf" else "image",
        )

        supplier_id = None
        if extracted.supplier_ico:
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
                )
                supplier_id = s.id
            except Exception as e:
                reasons.append(f"ARES selhal: {e}")
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
        )
        rebuild_fts_for_document(session, doc.id, extracted.full_text)

        # Move file
        if requires_review:
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

        return {"status": status, "sha256": sha, "file_id": file_record.id, "document_id": doc.id, "moved_to": str(moved), "confidence": extracted.confidence}
