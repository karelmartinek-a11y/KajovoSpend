from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from kajovospend.utils.config import load_yaml
from kajovospend.utils.paths import resolve_app_paths, default_data_dir
from kajovospend.utils.logging_setup import setup_logging
from kajovospend.db.session import make_engine, make_session_factory
from kajovospend.db.migrate import init_db
from kajovospend.service.app import ServiceApp
from kajovospend.service.control import ControlContext, ControlServer
from kajovospend.service.sync_ares import sync_pending_suppliers


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(Path(__file__).parent / "config.yaml"))
    sub = ap.add_subparsers(dest="command")

    ap_run = sub.add_parser("run")

    ap_sync = sub.add_parser("sync-ares")
    ap_sync.add_argument("--limit", type=int, default=500)

    args = ap.parse_args()
    if not getattr(args, "command", None):
        args.command = "run"

    cfg = load_yaml(Path(args.config))
    # Merge missing sections with defaults
    cfg.setdefault("app", {})
    cfg.setdefault("paths", {})
    cfg.setdefault("service", {})
    cfg.setdefault("ocr", {})
    cfg.setdefault("openai", {})
    cfg.setdefault("performance", {})
    cfg.setdefault("ares", {})

    paths = resolve_app_paths(
        cfg["app"].get("data_dir"),
        cfg["app"].get("db_path"),
        cfg["app"].get("log_dir"),
        cfg.get("ocr", {}).get("models_dir"),
    )
    log = setup_logging(paths.log_dir, name="kajovospend_service")

    engine = make_engine(str(paths.db_path))
    init_db(engine)
    sf = make_session_factory(engine)

    if args.command == "sync-ares":
        ttl_hours = float(cfg.get("ares", {}).get("ttl_hours", 24.0) or 24.0)
        limit = int(getattr(args, "limit", 500) or 500)
        stats = sync_pending_suppliers(sf, log, ttl_hours=ttl_hours, limit=limit)
        log.info("sync-ares done: %s", stats)
        return 0

    # store pid
    pid_path = paths.data_dir / "service.pid"
    pid_path.write_text(str(os.getpid()), encoding="utf-8")

    app = ServiceApp(cfg, sf, paths, log)

    ctrl = ControlServer(
        cfg["service"].get("host", "127.0.0.1"),
        int(cfg["service"].get("port", 8765)),
        ControlContext(get_status=app.get_status, request_stop=app.request_stop),
    )
    ctrl.start()

    def _sig(_signum, _frame):
        app.request_stop()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    try:
        log.info("Service started")
        app.run_forever()
        log.info("Service stopped")
        return 0
    finally:
        try:
            ctrl.shutdown()
        except Exception:
            pass
        try:
            if pid_path.exists():
                pid_path.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
