from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterable

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kajovospend.db.migrate import init_working_db, init_production_db
from kajovospend.db import models as legacy_models
from kajovospend.db import working_models as wm
from kajovospend.db import production_models as pm
from kajovospend.db.production_queries import upsert_supplier as upsert_supplier_prod, insert_document_from_working
from kajovospend.db.working_session import create_working_engine
from kajovospend.db.production_session import create_production_engine
from kajovospend.db.dual_db_guard import ensure_separate_databases, DualDbConfigError


class MigrationError(RuntimeError):
    pass


def _copy_workflow_tables(src: Session, dst: Session):
    # files
    for f in src.query(legacy_models.DocumentFile).all():
        nf = wm.DocumentFile(
            id=f.id,
            sha256=f.sha256,
            original_name=f.original_name,
            mime_type=f.mime_type,
            pages=f.pages,
            current_path=f.current_path,
            status=f.status,
            last_error=f.last_error,
            created_at=f.created_at,
            processed_at=f.processed_at,
        )
        dst.add(nf)
    dst.flush()
    # import_jobs
    for j in src.query(legacy_models.ImportJob).all():
        nj = wm.ImportJob(
            id=j.id,
            processing_id_in=j.processing_id_in,
            created_at=j.created_at,
            started_at=j.started_at,
            finished_at=j.finished_at,
            path=j.path,
            sha256=j.sha256,
            status=j.status,
            error=j.error,
        )
        dst.add(nj)
    # service_state singleton
    st = src.query(legacy_models.ServiceState).get(1)
    if st:
        ns = wm.ServiceState(
            singleton=1,
            running=st.running,
            last_success=st.last_success,
            last_error=st.last_error,
            last_error_at=st.last_error_at,
            queue_size=st.queue_size,
            last_seen=st.last_seen,
            inflight=st.inflight,
            max_workers=st.max_workers,
            current_job_id=st.current_job_id,
            current_path=st.current_path,
            current_phase=st.current_phase,
            current_progress=st.current_progress,
            heartbeat_at=st.heartbeat_at,
            stuck=st.stuck,
            stuck_reason=st.stuck_reason,
        )
        dst.add(ns)
    dst.commit()


def _copy_business_tables(src: Session, dst: Session):
    suppliers = {}
    for s in src.query(legacy_models.Supplier).all():
        suppliers[s.id] = upsert_supplier_prod(
            dst,
            s.ico,
            name=s.name,
            dic=s.dic,
            address=s.address,
            is_vat_payer=s.is_vat_payer,
            ares_last_sync=s.ares_last_sync,
            pending_ares=s.pending_ares,
            legal_form=s.legal_form,
            street=s.street,
            street_number=s.street_number,
            orientation_number=s.orientation_number,
            city=s.city,
            zip_code=s.zip_code,
        )
    dst.flush()
    for d in src.query(legacy_models.Document).all():
        supplier = suppliers.get(d.supplier_id) if d.supplier_id else None
        items = src.query(legacy_models.LineItem).filter_by(document_id=d.id).all()
        insert_document_from_working(dst, supplier, d, items)
    dst.commit()


def migrate_legacy_single_db(legacy_db: Path, working_db: Path, production_db: Path) -> None:
    if not legacy_db.exists():
        raise MigrationError(f"Legacy DB not found: {legacy_db}")
    ensure_separate_databases(str(working_db), str(production_db))

    # create target engines
    w_engine = create_working_engine(working_db)
    p_engine = create_production_engine(production_db)
    init_working_db(w_engine)
    init_production_db(p_engine)

    legacy_engine = create_engine(f"sqlite:///{legacy_db}")
    with legacy_engine.connect() as conn:
        pass

    with Session(legacy_engine) as src, Session(w_engine) as wdst, Session(p_engine) as pdst:
        # idempotence: if targets already contain data, refuse (explicit re-init required)
        if pdst.query(pm.Document).count() > 0 or wdst.query(wm.DocumentFile).count() > 0:
            raise MigrationError("Target DBs not empty; refusing to overwrite (non-idempotent state).")

        _copy_workflow_tables(src, wdst)
        _copy_business_tables(src, pdst)
