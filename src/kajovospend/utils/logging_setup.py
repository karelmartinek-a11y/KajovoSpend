from __future__ import annotations

import json
import logging
from logging.handlers import TimedRotatingFileHandler
import os
import platform
import socket
import sys
import threading
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict

import faulthandler

from kajovospend.utils.forensic_context import get_forensic_fields

if os.name == "nt":
    import msvcrt  # type: ignore
else:
    import fcntl  # type: ignore


class _InterProcessLock:
    """Cross-process file lock using a dedicated lock-file."""

    def __init__(self, lock_path: Path):
        self._lock_path = lock_path
        self._fh = None

    def __enter__(self):
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._lock_path, "a+b")
        try:
            if os.name == "nt":
                if self._fh.tell() == 0 and self._fh.seek(0, os.SEEK_END) == 0:
                    self._fh.write(b"\0")
                    self._fh.flush()
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self._fh:
            return
        try:
            if os.name == "nt":
                try:
                    self._fh.seek(0)
                except Exception:
                    pass
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None


class SafeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """Daily rotating handler with cross-process lock."""

    def __init__(self, filename: Path, *, backup_count: int = 7, encoding: str = "utf-8"):
        self._filename = Path(filename)
        self._filename.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self._filename.parent / (self._filename.name + ".lock")
        self._mtx = threading.RLock()
        super().__init__(
            filename=str(self._filename),
            when="midnight",
            interval=1,
            backupCount=int(max(1, backup_count)),
            encoding=encoding,
            utc=True,
            delay=False,
            errors="backslashreplace",
        )

    def emit(self, record: logging.LogRecord) -> None:
        with self._mtx:
            with _InterProcessLock(self._lock_path):
                super().emit(record)

    def doRollover(self) -> None:
        with self._mtx:
            with _InterProcessLock(self._lock_path):
                super().doRollover()


_ROOT_CONFIGURED = False
_ROOT_CONFIG_LOCK = threading.Lock()
_ROOT_LOG_DIR: Path | None = None
_FORENSIC_HOOKS_INSTALLED = False
_FAULT_HANDLER_STREAM = None


class ForensicContextFilter(logging.Filter):
    """Attach forensic runtime metadata to every log record."""

    def __init__(self) -> None:
        super().__init__()
        self._hostname = socket.gethostname()
        self._platform = platform.platform(terse=False)
        self._python = sys.version.replace("\n", " ").strip()
        self._user = os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"

    def filter(self, record: logging.LogRecord) -> bool:
        record.hostname = self._hostname
        record.platform = self._platform
        record.python = self._python
        record.user = self._user
        record.cwd = os.getcwd()
        try:
            record.forensic = get_forensic_fields()
        except Exception:
            record.forensic = None
        return True


class JsonLineFormatter(logging.Formatter):
    """Serialize record to JSONL for forensic processing."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "funcName": record.funcName,
            "line": record.lineno,
            "pathname": record.pathname,
            "process": record.process,
            "processName": record.processName,
            "thread": record.thread,
            "threadName": record.threadName,
            "hostname": getattr(record, "hostname", None),
            "user": getattr(record, "user", None),
            "cwd": getattr(record, "cwd", None),
            "platform": getattr(record, "platform", None),
            "python": getattr(record, "python", None),
            "event_name": getattr(record, "event_name", None),
        }

        payload["forensic"] = getattr(record, "forensic", None) or get_forensic_fields()

        extra_obj = getattr(record, "extra_payload", None)
        if extra_obj:
            payload["extra"] = extra_obj

        if record.exc_info:
            payload["exception"] = "".join(traceback.format_exception(*record.exc_info))

        if record.stack_info:
            payload["stack"] = record.stack_info

        return json.dumps(payload, ensure_ascii=False, default=str)


def _install_forensic_runtime_hooks(log: logging.Logger, log_dir: Path) -> None:
    global _FAULT_HANDLER_STREAM
    global _FORENSIC_HOOKS_INSTALLED
    if _FORENSIC_HOOKS_INSTALLED:
        return

    _FORENSIC_HOOKS_INSTALLED = True

    def _sys_excepthook(exc_type, exc, tb):
        log.critical("Unhandled exception in main thread", exc_info=(exc_type, exc, tb))

    def _threading_excepthook(args: threading.ExceptHookArgs) -> None:
        log.critical(
            "Unhandled exception in thread name=%s ident=%s",
            getattr(args.thread, "name", "unknown"),
            getattr(args.thread, "ident", "unknown"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _sys_excepthook
    threading.excepthook = _threading_excepthook
    logging.captureWarnings(True)

    try:
        crash_file = log_dir / "kajovospend_faulthandler.log"
        _FAULT_HANDLER_STREAM = open(crash_file, "a", encoding="utf-8", errors="backslashreplace")
        faulthandler.enable(_FAULT_HANDLER_STREAM, all_threads=True)
    except Exception:
        log.exception("Failed to enable faulthandler")

    log.info(
        "Forensic runtime hooks active; cmdline=%s env_debug=%s",
        sys.argv,
        os.environ.get("KAJOVOSPEND_DEBUG", ""),
    )


def _compute_retention_days() -> int:
    raw = str(os.environ.get("KAJOVOSPEND_LOG_RETENTION_DAYS", "")).strip()
    try:
        val = int(raw) if raw else 7
    except Exception:
        val = 7
    return max(1, min(val, 365))


def _compute_max_lines() -> int:
    """
    Backward-compatible helper for tests and legacy code paths.
    Daily rotating logs now use retention days directly, but callers may still
    rely on a computed line budget.
    """
    raw_max = str(os.environ.get("KAJOVOSPEND_LOG_MAX_LINES", "")).strip()
    if raw_max:
        try:
            max_lines = int(raw_max)
            if max_lines > 0:
                return max_lines
        except Exception:
            pass

    raw_lines = str(os.environ.get("KAJOVOSPEND_LOG_LINES_PER_DAY_ESTIMATE", "")).strip()
    try:
        per_day = int(raw_lines) if raw_lines else 20000
    except Exception:
        per_day = 20000
    per_day = max(100, per_day)
    return int(_compute_retention_days() * per_day)


def _remove_owned_handlers(root: logging.Logger) -> None:
    for handler in list(root.handlers):
        if not getattr(handler, "_kajovospend_owned", False):
            continue
        try:
            root.removeHandler(handler)
        except Exception:
            pass
        try:
            handler.close()
        except Exception:
            pass


def setup_logging(log_dir: Path, name: str = "kajovospend") -> logging.Logger:
    """Configure shared text + forensic logs with daily rotation."""
    global _ROOT_CONFIGURED, _ROOT_LOG_DIR

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    resolved_log_dir = log_dir.resolve()
    logger = logging.getLogger(name)

    retention_days = _compute_retention_days()
    detail = str(os.environ.get("KAJOVOSPEND_LOG_DETAIL", "1")).strip() not in {
        "0", "false", "False", "FALSE", "no", "NO"
    }

    with _ROOT_CONFIG_LOCK:
        root = logging.getLogger()
        need_reconfigure = (not _ROOT_CONFIGURED) or (_ROOT_LOG_DIR is None) or (_ROOT_LOG_DIR != resolved_log_dir)
        if need_reconfigure:
            _remove_owned_handlers(root)
            root.setLevel(logging.DEBUG)

            fmt = logging.Formatter(
                "%(asctime)s.%(msecs)03d %(levelname)s "
                "pid=%(process)d tid=%(threadName)s "
                "host=%(hostname)s user=%(user)s "
                "[%(name)s:%(funcName)s:%(lineno)d] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )

            forensic_filter = ForensicContextFilter()

            text_handler = SafeTimedRotatingFileHandler(
                log_dir / "kajovospend.log",
                backup_count=retention_days,
                encoding="utf-8",
            )
            text_handler.setLevel(logging.DEBUG)
            text_handler.setFormatter(fmt)
            text_handler.addFilter(forensic_filter)
            setattr(text_handler, "_kajovospend_owned", True)
            root.addHandler(text_handler)

            forensic_handler = SafeTimedRotatingFileHandler(
                log_dir / "kajovospend_forensic.jsonl",
                backup_count=retention_days,
                encoding="utf-8",
            )
            forensic_handler.setLevel(logging.DEBUG)
            forensic_handler.setFormatter(JsonLineFormatter())
            forensic_handler.addFilter(forensic_filter)
            setattr(forensic_handler, "_kajovospend_owned", True)
            root.addHandler(forensic_handler)

            if os.environ.get("KAJOVOSPEND_LOG_CONSOLE", "").strip() in {"1", "true", "TRUE", "yes", "YES"}:
                console_handler = logging.StreamHandler()
                console_handler.setLevel(logging.DEBUG)
                console_handler.setFormatter(fmt)
                console_handler.addFilter(forensic_filter)
                setattr(console_handler, "_kajovospend_owned", True)
                root.addHandler(console_handler)

            _ROOT_CONFIGURED = True
            _ROOT_LOG_DIR = resolved_log_dir

        setattr(root, "_kajovospend_log_detail", detail)

    logger.setLevel(logging.DEBUG)
    logger.propagate = True
    _install_forensic_runtime_hooks(logger, log_dir)
    logger.info(
        "Logging initialized: log_dir=%s pid=%s ppid=%s retention_days=%s rotate=%s detail=%s",
        log_dir,
        os.getpid(),
        os.getppid() if hasattr(os, "getppid") else "n/a",
        retention_days,
        "daily",
        int(detail),
        extra={
            "event_name": "logging.start",
            "extra_payload": {
                "retention_days": retention_days,
                "rotation": "daily",
                "detail": int(detail),
                "log_dir": str(log_dir),
            },
        },
    )
    return logger


def log_event(logger: logging.Logger, event_name: str, message: str, **extra: Any) -> None:
    """
    Structured log helper:
    - adds event_name and extra_payload
    - appends key=value suffix to text log for readability
    """
    extra_payload: Dict[str, Any] = extra or {}
    detail_enabled = True
    try:
        root = logging.getLogger()
        detail_enabled = bool(getattr(root, "_kajovospend_log_detail", True))
    except Exception:
        detail_enabled = True

    if not detail_enabled:
        pruned = {}
        for k, v in extra_payload.items():
            try:
                s = repr(v)
                if len(s) <= 400:
                    pruned[k] = v
            except Exception:
                continue
        extra_payload = pruned

    suffix = ""
    if extra_payload:
        try:
            suffix = " | " + " ".join(f"{k}={extra_payload[k]!r}" for k in sorted(extra_payload.keys()))
        except Exception:
            suffix = ""

    logger.info(
        f"{message}{suffix}",
        extra={
            "event_name": event_name,
            "extra_payload": extra_payload,
            "forensic": get_forensic_fields(),
        },
    )
