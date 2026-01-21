from __future__ import annotations

import datetime as dt
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
from typing import Any, Dict

from sqlalchemy.orm import Session
from sqlalchemy import select

from kajovospend.db.models import ImportJob, ServiceState
from kajovospend.db.queries import update_service_state, queue_size
from kajovospend.service.watcher import DirectoryWatcher, scan_directory
from kajovospend.service.processor import Processor


class ServiceApp:
    def __init__(self, cfg: Dict[str, Any], session_factory, paths, logger):
        self.cfg = cfg
        self.sf = session_factory
        self.paths = paths
        self.log = logger
        self._stop = threading.Event()
        self._watcher = DirectoryWatcher(Path(cfg["paths"]["input_dir"]), self.enqueue_path)
        self._max_workers = int(cfg["service"].get("workers", 2))
        self._executor = ThreadPoolExecutor(max_workers=self._max_workers)
        self._inflight_lock = threading.Lock()
        self._inflight: set[Future] = set()
        self._processor = Processor(cfg, paths, logger)

    def _drop_future(self, fut: Future) -> None:
        # callback runs in worker thread; keep it tiny and safe
        try:
            with self._inflight_lock:
                self._inflight.discard(fut)
        except Exception:
            # never let bookkeeping crash service threads
            pass

    def _inflight_count(self) -> int:
        with self._inflight_lock:
            # prune done futures (in case callback didn't run for any reason)
            self._inflight = {f for f in self._inflight if not f.done()}
            return len(self._inflight)

    def _submit_job(self, job_id: int) -> None:
        fut = self._executor.submit(self._run_job, job_id)
        with self._inflight_lock:
            self._inflight.add(fut)
        fut.add_done_callback(self._drop_future)

    def enqueue_path(self, p: Path) -> None:
        # wait briefly for file to finish writing
        time.sleep(0.2)
        if self._stop.is_set():
            return
        with self.sf() as session:
            # if already queued for this path and not finished, skip
            existing = session.execute(select(ImportJob).where(ImportJob.path == str(p), ImportJob.status.in_(["QUEUED", "RUNNING"])) ).scalar_one_or_none()
            if existing:
                return
            job = ImportJob(path=str(p), status="QUEUED")
            session.add(job)
            session.commit()

    def _claim_next_job(self, session: Session) -> ImportJob | None:
        job = session.execute(
            select(ImportJob).where(ImportJob.status == "QUEUED").order_by(ImportJob.created_at).limit(1)
        ).scalar_one_or_none()
        if not job:
            return None
        job.status = "RUNNING"
        job.started_at = dt.datetime.utcnow()
        session.add(job)
        session.flush()
        return job

    def _run_job(self, job_id: int):
        with self.sf() as session:
            job = session.get(ImportJob, job_id)
            if not job:
                return
            p = Path(job.path)
            try:
                if not p.exists():
                    job.status = "ERROR"
                    job.error = "file_missing"
                else:
                    res = self._processor.process_path(session, p)
                    job.sha256 = res.get("sha256")
                    job.status = res.get("status")
                    job.error = None
                job.finished_at = dt.datetime.utcnow()
                session.add(job)

                # update service state
                st = session.get(ServiceState, 1)
                if st:
                    st.queue_size = queue_size(session)
                    if job.status in ("PROCESSED",):
                        st.last_success = dt.datetime.utcnow()
                    elif job.status in ("ERROR",):
                        st.last_error = job.error
                        st.last_error_at = dt.datetime.utcnow()
                session.commit()
            except Exception as e:
                self.log.exception(f"Job failed: {e}")
                try:
                    job.status = "ERROR"
                    job.error = str(e)
                    job.finished_at = dt.datetime.utcnow()
                    session.add(job)
                    update_service_state(session, last_error=str(e), last_error_at=dt.datetime.utcnow())
                    session.commit()
                except Exception:
                    self.log.exception("Failed to persist job failure state")

    def get_status(self) -> Dict[str, Any]:
        with self.sf() as session:
            st = session.get(ServiceState, 1)
            if not st:
                return {"running": False, "queue_size": 0}
            return {
                "running": bool(st.running),
                "last_success": st.last_success.isoformat() if st.last_success else None,
                "last_error": st.last_error,
                "last_error_at": st.last_error_at.isoformat() if st.last_error_at else None,
                "queue_size": int(st.queue_size or 0),
                "last_seen": st.last_seen.isoformat() if st.last_seen else None,
            }

    def request_stop(self) -> None:
        self._stop.set()

    def run_forever(self):
        # mark running
        with self.sf() as session:
            update_service_state(session, running=True)
            session.commit()

        self._watcher.start()
        scan_interval = float(self.cfg["service"].get("scan_interval_sec", 2))

        try:
            while not self._stop.is_set():
                # scan for missed files
                for p in scan_directory(Path(self.cfg["paths"]["input_dir"])):
                    self.enqueue_path(p)

                # claim jobs and dispatch
                with self.sf() as session:
                    update_service_state(
                        session,
                        queue_size=queue_size(session),
                        last_seen=dt.datetime.utcnow(),
                    )
                    session.commit()

                    # dispatch více jobů v jednom cyklu (až do kapacity workerů)
                    available = max(0, self._max_workers - self._inflight_count())
                    dispatched = 0
                    while dispatched < available:
                        job = self._claim_next_job(session)
                        if not job:
                            break
                        job_id = job.id
                        session.commit()
                        self._submit_job(job_id)
                        dispatched += 1

                time.sleep(scan_interval)
        finally:
            # best-effort shutdown; never raise from finally
            try:
                self._watcher.stop()
            except Exception:
                self.log.exception("Watcher stop failed")
            try:
                self._executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                self.log.exception("Executor shutdown failed")
            try:
                with self.sf() as session:
                    update_service_state(
                        session,
                        running=False,
                        last_seen=dt.datetime.utcnow(),
                        queue_size=queue_size(session),
                    )
                    session.commit()
            except Exception:
                self.log.exception("Failed to persist service shutdown state")
