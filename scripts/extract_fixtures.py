from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 2

ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sqlalchemy import select  # noqa: E402

from kajovospend.db.migrate import init_db  # noqa: E402
from kajovospend.db.models import Document, LineItem  # noqa: E402
from kajovospend.db.session import make_engine, make_session_factory  # noqa: E402
from kajovospend.extract.parser import postprocess_items_for_db  # noqa: E402
from kajovospend.service.processor import Processor  # noqa: E402
from kajovospend.utils.config import load_yaml  # noqa: E402
from kajovospend.utils.logging_setup import setup_logging  # noqa: E402
from kajovospend.utils.paths import resolve_app_paths  # noqa: E402


def _ensure_cfg_minimal(cfg: Dict[str, Any]) -> Dict[str, Any]:
    cfg.setdefault("app", {})
    cfg.setdefault("paths", {})
    cfg.setdefault("service", {})
    cfg.setdefault("ocr", {})
    cfg.setdefault("openai", {})
    cfg.setdefault("performance", {})
    return cfg


def _split_reasons(s: Optional[str]) -> List[str]:
    if not s:
        return []
    parts = [p.strip() for p in str(s).split(";") if p.strip()]
    return parts


def _doc_items_as_dicts(items: List[LineItem]) -> List[dict]:
    out: List[dict] = []
    for it in items:
        out.append(
            {
                "name": it.name,
                "quantity": float(it.quantity or 0.0),
                "unit_price": float(it.unit_price or 0.0),
                "vat_rate": float(it.vat_rate or 0.0),
                "line_total": float(it.line_total or 0.0),
            }
        )
    return out


def _compute_sum_ok(doc: Document, items: List[LineItem]) -> bool:
    items_copy = _doc_items_as_dicts(items)
    ok, _reasons = postprocess_items_for_db(items=items_copy, total_with_vat=doc.total_with_vat, reasons=[])
    return bool(ok)


def run(fixtures_dir: Path, *, cfg: Dict[str, Any], db_path: Path, snapshot_path: Path, work_dir: Path, log_dir: Path, reset: bool) -> Dict[str, Any]:
    fixtures_dir = Path(fixtures_dir)
    if not fixtures_dir.exists():
        raise SystemExit(f"fixtures-dir neexistuje: {fixtures_dir}")

    work_dir = Path(work_dir)
    in_dir = work_dir / "input"
    out_dir = work_dir / "output"

    db_path = Path(db_path)
    snapshot_path = Path(snapshot_path)
    log_dir = Path(log_dir)

    if reset:
        try:
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass
        try:
            if db_path.exists():
                db_path.unlink()
        except Exception:
            pass
        try:
            if snapshot_path.exists():
                snapshot_path.unlink()
        except Exception:
            pass

    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    cfg = _ensure_cfg_minimal(cfg)
    cfg["paths"]["output_dir"] = str(out_dir)

    paths = resolve_app_paths(
        cfg["app"].get("data_dir"),
        str(db_path),
        str(log_dir),
        cfg.get("ocr", {}).get("models_dir"),
    )
    log = setup_logging(paths.log_dir, name="kajovospend_fixture_extract")

    engine = make_engine(str(paths.db_path))
    init_db(engine)
    sf = make_session_factory(engine)

    processor = Processor(cfg, paths, log)

    pdfs = sorted([p for p in fixtures_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"], key=lambda p: p.name.lower())
    if not pdfs:
        raise SystemExit(f"V {fixtures_dir} nejsou žádné PDF.")

    results: List[Dict[str, Any]] = []
    total_docs = 0
    total_complete = 0
    total_review = 0

    with sf() as session:
        for src in pdfs:
            dest = in_dir / src.name
            # overwrite for repeatability
            try:
                if dest.exists():
                    dest.unlink()
            except Exception:
                pass
            shutil.copy2(src, dest)

            log.info("=== FIXTURE START name=%s path=%s ===", src.name, str(src))
            res = processor.process_path(session, dest)
            session.commit()

            file_id = res.get("file_id")
            text_method = res.get("text_method")
            text_debug = res.get("text_debug") or {}

            docs_out: List[Dict[str, Any]] = []
            if file_id:
                doc_ids = list(res.get("document_ids") or [])
                for did in doc_ids:
                    doc = session.execute(select(Document).where(Document.id == int(did))).scalar_one_or_none()
                    if not doc:
                        continue
                    items = session.execute(select(LineItem).where(LineItem.document_id == doc.id).order_by(LineItem.id)).scalars().all()
                    sum_ok = _compute_sum_ok(doc, list(items))
                    # Pro regresi chceme měřit to, co rozhoduje o karanténě: complete = !requires_review
                    complete = bool(not bool(doc.requires_review))
                    rr = _split_reasons(doc.review_reasons)
                    method = getattr(doc, "method", None) or res.get("method") or getattr(doc, "extraction_method", None) or "offline"
                    docs_out.append(
                        {
                            "doc_id": int(doc.id),
                            "page_from": int(doc.page_from or 1),
                            "page_to": int(doc.page_to or doc.page_from or 1),
                            "method": method,
                            "text_method": text_method,
                            "complete": bool(complete),
                            "review_reasons": rr,
                            "doc_number": doc.doc_number,
                            "issue_date": doc.issue_date.isoformat() if doc.issue_date else None,
                            "total": float(doc.total_with_vat) if doc.total_with_vat is not None else None,
                            "items_count": int(len(items)),
                            "sum_ok": bool(sum_ok),
                        }
                    )
                    total_docs += 1
                    total_complete += 1 if complete else 0
                    total_review += 1 if (doc.requires_review or (not complete)) else 0

            results.append(
                {
                    "fixture": src.name,
                    "source_path": str(src),
                    "status": res.get("status"),
                    "sha256": res.get("sha256"),
                    "file_id": file_id,
                    "moved_to": res.get("moved_to"),
                    "text_method": text_method,
                    "text_debug": text_debug,
                    "documents": docs_out,
                }
            )
            log.info("=== FIXTURE END name=%s status=%s docs=%s ===", src.name, res.get("status"), len(docs_out))

    snapshot = {
        "schema": "kajovospend.extract_fixtures.v2",
        "schema_version": SCHEMA_VERSION,
        "fixtures_dir": str(fixtures_dir),
        "db_path": str(db_path),
        "work_dir": str(work_dir),
        "log_dir": str(log_dir),
        "summary": {
            "files": int(len(results)),
            "documents": int(total_docs),
            "complete": int(total_complete),
            "requires_review_or_incomplete": int(total_review),
        },
        "results": results,
    }

    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    print(f"OK: snapshot={snapshot_path}")
    print(f"OK: test_db={db_path}")
    print(f"OK: log_file={(log_dir / 'kajovospend.log')}")
    print(f"SUMMARY: files={len(results)} docs={total_docs} complete={total_complete} review_or_incomplete={total_review}")
    return snapshot


def main() -> int:
    ap = argparse.ArgumentParser(description="KájovoSpend baseline harness: zpracuje sadu PDF a uloží JSON snapshot + test SQLite DB.")
    ap.add_argument("--fixtures-dir", required=True, help="Adresář se známými PDF doklady (rekurzivně).")
    ap.add_argument("--config", default=str(ROOT_DIR / "config.yaml"), help="Volitelný config.yaml (použije se hlavně OCR/models_dir).")
    ap.add_argument("--db-path", default=str(ROOT_DIR / "var" / "test_extract.db"), help="SQLite DB pro test (separátní od produkce).")
    ap.add_argument("--snapshot-path", default=str(ROOT_DIR / "var" / "extract_snapshot.json"), help="Výstupní JSON snapshot.")
    ap.add_argument("--work-dir", default=str(ROOT_DIR / "var" / "extract_work"), help="Pracovní adresář (kopie vstupů + output).")
    ap.add_argument("--log-dir", default=str(ROOT_DIR / "var" / "logs"), help="Logy harnessu.")
    ap.add_argument("--reset", action="store_true", help="Smaže work_dir + db + snapshot (čistý běh).")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    cfg: Dict[str, Any] = {}
    if cfg_path.exists():
        cfg = load_yaml(cfg_path)

    run(
        Path(args.fixtures_dir),
        cfg=cfg,
        db_path=Path(args.db_path),
        snapshot_path=Path(args.snapshot_path),
        work_dir=Path(args.work_dir),
        log_dir=Path(args.log_dir),
        reset=bool(args.reset),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
