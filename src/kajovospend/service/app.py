from __future__ import annotations

import datetime as dt
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
from typing import Any, Dict

from sqlalchemy.orm import Session
from sqlalchemy import select

from kajovospend.db.models import ImportJob, ServiceState
from kajovospend.db.queries import update_service_state, queue_size
from kajovospend.service.watcher import DirectoryWatcher
from kajovospend.service.processor import Processor, safe_move


class ServiceApp:
    def __init__(self, cfg: Dict[str, Any], session_factory, paths, logger):
        self.cfg = cfg
        self.sf = session_factory
        self.paths = paths
        self.log = logger
        self._stop = threading.Event()
        self._watcher = DirectoryWatcher(Path(cfg["paths"]["input_dir"]), self.enqueue_path)
        # Vynuceně sekvenční zpracování (1 worker)
        self._max_workers = 1
        self._executor = ThreadPoolExecutor(max_workers=self._max_workers)
        self._inflight_lock = threading.Lock()
        self._inflight: set[Future] = set()
        self._processor = Processor(cfg, paths, logger)
        self._supported_ext = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}

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

    def _collect_input_files(self) -> list[Path]:
        input_dir = Path(self.cfg["paths"]["input_dir"])
        out_base = Path(self.cfg["paths"]["output_dir"])
        quarantine_dir = out_base / self.cfg["paths"].get("quarantine_dir_name", "KARANTENA")

        files: list[Path] = []
        try:
            if not input_dir.exists():
                return files
            for p in input_dir.rglob("*"):
                if not p.is_file():
                    continue
                if p.suffix.lower() in self._supported_ext:
                    files.append(p)
                else:
                    try:
                        moved = safe_move(p, quarantine_dir, p.name)
                        self.log.warning("Nepodporovaný soubor %s přesunut do karantény jako %s", p, moved)
                    except Exception as exc:
                        self.log.exception("Nelze přesunout nepodporovaný soubor %s: %s", p, exc)
            # vyčistit prázdné podsložky
            dirs = sorted([d for d in input_dir.rglob("*") if d.is_dir()], key=lambda d: len(d.parts), reverse=True)
            for d in dirs:
                try:
                    if not any(d.iterdir()):
                        d.rmdir()
                except Exception:
                    pass
            files.sort(key=lambda p: (p.stat().st_mtime, p.name, str(p)))
        except Exception as exc:
            self.log.exception("Chyba při skenování IN: %s", exc)
        return files

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

    def _watchdog_mark_stuck(self, session: Session, timeout_sec: int) -> int:
        """
        If a job is RUNNING for too long, mark it ERROR to avoid infinite "zaseknutá fronta".
        Deterministic rule: started_at older than now-timeout => ERROR (stuck_timeout).
        """
        if timeout_sec <= 0:
            return 0
        cutoff = dt.datetime.utcnow() - dt.timedelta(seconds=int(timeout_sec))
        stuck_jobs = session.execute(
            select(ImportJob).where(
                (ImportJob.status == "RUNNING") &
                (ImportJob.started_at != None) &  # noqa: E711
                (ImportJob.started_at < cutoff)
            )
        ).scalars().all()
        n = 0
        for j in stuck_jobs:
            j.status = "ERROR"
            j.error = f"stuck_timeout>{timeout_sec}s"
            j.finished_at = dt.datetime.utcnow()
            session.add(j)
            n += 1
        return n

    def _run_job(self, job_id: int):
        with self.sf() as session:
            job = session.get(ImportJob, job_id)
            if not job:
                return
            p = Path(job.path)
            try:
                # best-effort "current job" observability
                update_service_state(
                    session,
                    current_job_id=int(job.id),
                    current_path=str(p),
                    current_phase="processing",
                    current_progress=0.0,
                    heartbeat_at=dt.datetime.utcnow(),
                    inflight=self._inflight_count(),
                    max_workers=int(self._max_workers),
                )
                session.commit()
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
                    st.inflight = self._inflight_count()
                    st.max_workers = int(self._max_workers)
                    st.current_progress = 100.0
                    st.heartbeat_at = dt.datetime.utcnow()
                    st.current_phase = "dispatching"
                    st.current_job_id = None
                    st.current_path = None
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
                    update_service_state(
                        session,
                        last_error=str(e),
                        last_error_at=dt.datetime.utcnow(),
                        current_phase="error",
                        heartbeat_at=dt.datetime.utcnow(),
                        inflight=self._inflight_count(),
                    )
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
                "inflight": int(st.inflight or 0),
                "max_workers": int(st.max_workers or 0),
                "current_job_id": int(st.current_job_id) if st.current_job_id is not None else None,
                "current_path": st.current_path,
                "current_phase": st.current_phase,
                "current_progress": float(st.current_progress) if st.current_progress is not None else None,
                "heartbeat_at": st.heartbeat_at.isoformat() if st.heartbeat_at else None,
                "stuck": bool(st.stuck) if st.stuck is not None else False,
                "stuck_reason": st.stuck_reason,
            }

    def request_stop(self) -> None:
        self._stop.set()

    def run_forever(self):
        # mark running
        with self.sf() as session:
            update_service_state(
                session,
                running=True,
                max_workers=int(self._max_workers),
                inflight=0,
                current_phase="idle",
                heartbeat_at=dt.datetime.utcnow(),
                stuck=False,
                stuck_reason=None,
            )
            session.commit()

        self._watcher.start()
        scan_interval = float(self.cfg["service"].get("scan_interval_sec", 2))
        watchdog_sec = int(self.cfg.get("service", {}).get("watchdog_timeout_sec", 900) or 900)

        try:
            while not self._stop.is_set():
                # watchdog: clear stuck RUNNING jobs (so UI can see what's wrong)
                with self.sf() as session:
                    n_stuck = self._watchdog_mark_stuck(session, watchdog_sec)
                    if n_stuck:
                        update_service_state(
                            session,
                            last_error=f"watchdog: {n_stuck} job(s) stuck_timeout",
                            last_error_at=dt.datetime.utcnow(),
                            stuck=True,
                            stuck_reason=f"stuck_timeout>{watchdog_sec}s",
                        )
                    session.commit()

                # scan for missed files (rekurzivně, včetně podadresářů)
                phase = "scanning"
                for p in self._collect_input_files():
                    self.enqueue_path(p)

                # claim jobs and dispatch
                with self.sf() as session:
                    update_service_state(
                        session,
                        queue_size=queue_size(session),
                        last_seen=dt.datetime.utcnow(),
                        inflight=self._inflight_count(),
                        max_workers=int(self._max_workers),
                        current_phase="dispatching",
                        heartbeat_at=dt.datetime.utcnow(),
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

                # if idle, mark so dashboard shows "nic neběží"
                with self.sf() as session:
                    update_service_state(
                        session,
                        inflight=self._inflight_count(),
                        current_phase=("idle" if (self._inflight_count() == 0 and queue_size(session) == 0) else "dispatching"),
                        heartbeat_at=dt.datetime.utcnow(),
                    )
                    session.commit()

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
                        inflight=0,
                        current_phase="shutdown",
                        heartbeat_at=dt.datetime.utcnow(),
                    )
                    session.commit()
            except Exception:
                self.log.exception("Failed to persist service shutdown state")
