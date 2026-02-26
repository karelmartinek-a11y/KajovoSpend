from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

from kajovospend.service.processor import Processor


class _Extracted:
    requires_review = True
    review_reasons = ["chybí datum", "chybí dodavatel"]
    supplier_ico = None
    doc_number = None
    issue_date = None
    total_with_vat = None
    items = []


def test_build_forensic_bundle_payload_obsahuje_hlavni_data() -> None:
    p = Processor.__new__(Processor)
    payload = p._build_forensic_bundle_payload(
        source_path=Path("/tmp/INPUT/a.pdf"),
        moved_to=Path("/tmp/OUTPUT/KARANTENA/a.pdf"),
        sha256="abc123",
        status="QUARANTINE",
        text_method="pdf_hybrid",
        text_debug={"reason": "parse_failed"},
        file_record=SimpleNamespace(id=11, last_error="nekompletní vytěžení"),
        per_doc_chunks=[
            {
                "page_from": 1,
                "page_to": 2,
                "ocr_conf": 0.42,
                "full_text": "ukázkový text",
                "extracted": _Extracted(),
            }
        ],
        created_doc_ids=[],
        correlation_id="corr-1",
    )

    assert payload["correlation_id"] == "corr-1"
    assert payload["status"] == "QUARANTINE"
    assert payload["text_method"] == "pdf_hybrid"
    assert payload["documents"][0]["review_reasons"] == ["chybí datum", "chybí dodavatel"]
    assert payload["documents"][0]["text_preview"] == "ukázkový text"


def test_write_forensic_bundle_vytvori_json(tmp_path: Path) -> None:
    p = Processor.__new__(Processor)
    p.cfg = {"paths": {"forensic_dir_name": "FORENSIC"}}
    p.log = logging.getLogger("kajovospend.test_forensic_bundle")

    bundle_path = p._write_forensic_bundle(
        out_base=tmp_path,
        source_path=Path("doklad.pdf"),
        moved_to=Path("OUTPUT/KARANTENA/doklad.pdf"),
        sha256="1234567890abcdef",
        status="QUARANTINE",
        text_method="image_ocr",
        text_debug={"ocr_conf": 0.3},
        file_record=SimpleNamespace(id=77, last_error="test"),
        per_doc_chunks=[],
        created_doc_ids=[],
        correlation_id="corr-77",
    )

    assert bundle_path is not None
    assert bundle_path.exists()
    data = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert data["status"] == "QUARANTINE"
    assert data["sha256"] == "1234567890abcdef"
    assert data["correlation_id"] == "corr-77"
