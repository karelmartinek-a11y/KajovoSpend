from __future__ import annotations

import json
import logging
import os
import platform
import socket
import sys
import threading
import traceback
from collections import deque
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
    """
    Cross-process file lock using a dedicated lock-file.
    Keeps kajovospend.log safe when GUI + service write concurrently.
    """

    def __init__(self, lock_path: Path):
        self._lock_path = lock_path
        self._fh = None

    def __enter__(self):
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._lock_path, "a+b")
        try:
            if os.name == "nt":
                # lock 1 byte at the beginning (ensure file is at least 1 byte)
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


class LineCappedFileHandler(logging.Handler):
    """
    Single log file with "ring buffer" behavior:
    - appends normally
    - once it grows beyond max_lines (+ small chunk), it truncates to last max_lines

    This keeps one file (kajovospend.log) and effectively overwrites old lines.
    """

    def __init__(
        self,
        filename: Path,
        *,
        max_lines: int = 5000,
        encoding: str = "utf-8",
    ):
        super().__init__()
        self._filename = Path(filename)
        self._encoding = encoding
        self.max_lines = int(max_lines)
        # trim every N extra lines to avoid rewriting on every emit
        self._trim_chunk = max(10, self.max_lines // 100)  # 5000 -> 50
        self._lock_path = self._filename.parent / (self._filename.name + ".lock")
        self._mtx = threading.RLock()
        self._stream = None
        self._line_count = 0
        self._open_and_count()

    def _open_and_count(self) -> None:
        self._filename.parent.mkdir(parents=True, exist_ok=True)
        # keep "errors" tolerant so no log write ever crashes the app
        self._stream = open(self._filename, "a", encoding=self._encoding, errors="backslashreplace")
        try:
            if self._filename.exists():
                with open(self._filename, "r", encoding=self._encoding, errors="ignore") as rf:
                    self._line_count = sum(1 for _ in rf)
            else:
                self._line_count = 0
        except Exception:
            self._line_count = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            if not msg.endswith("\n"):
                msg += "\n"
            with self._mtx:
                with _InterProcessLock(self._lock_path):
                    if self._stream is None:
                        self._open_and_count()
                    assert self._stream is not None
                    self._stream.write(msg)
                    self._stream.flush()
                    self._line_count += 1
                    if self._line_count >= (self.max_lines + self._trim_chunk):
                        self._trim_to_last_max_lines()
        except Exception:
            self.handleError(record)

    def _trim_to_last_max_lines(self) -> None:
        # stream already locked by _InterProcessLock in emit()
        try:
            if self._stream is not None:
                try:
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None

            tail = deque(maxlen=self.max_lines)
            try:
                with open(self._filename, "r", encoding=self._encoding, errors="ignore") as rf:
                    for line in rf:
                        tail.append(line)
            except FileNotFoundError:
                tail = deque(maxlen=self.max_lines)

            with open(self._filename, "w", encoding=self._encoding, errors="backslashreplace") as wf:
                wf.writelines(tail)

            self._line_count = len(tail)
        finally:
            # reopen for append
            self._stream = open(self._filename, "a", encoding=self._encoding, errors="backslashreplace")

    def close(self) -> None:
        with self._mtx:
            try:
                if self._stream is not None:
                    self._stream.close()
            except Exception:
                pass
            self._stream = None
        super().close()


_ROOT_CONFIGURED = False
_ROOT_CONFIG_LOCK = threading.Lock()
_FORENSIC_HOOKS_INSTALLED = False
_FAULT_HANDLER_STREAM = None


class ForensicContextFilter(logging.Filter):
    """Doplní do každého záznamu detailní forenzní metadata."""

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
    """Serializuje log record do JSONL pro strojové forenzní čtení."""

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
        log.critical("Nezachycená výjimka v hlavním vlákně", exc_info=(exc_type, exc, tb))

    def _threading_excepthook(args: threading.ExceptHookArgs) -> None:
        log.critical(
            "Nezachycená výjimka ve vlákně name=%s ident=%s",
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
        log.exception("Nepodařilo se aktivovat faulthandler")

    log.info(
        "Forenzní runtime hooky aktivní; cmdline=%s env_debug=%s",
        sys.argv,
        os.environ.get("KAJOVOSPEND_DEBUG", ""),
    )


def _compute_max_lines() -> int:
    """
    Určí max_lines podle priority:
    1) KAJOVOSPEND_LOG_MAX_LINES
    2) KAJOVOSPEND_LOG_RETENTION_DAYS * KAJOVOSPEND_LOG_LINES_PER_DAY_ESTIMATE
    """
    env_max = os.environ.get("KAJOVOSPEND_LOG_MAX_LINES")
    if env_max:
        try:
            val = int(env_max)
            if val > 0:
                return val
        except Exception:
            pass
    retention_days = int(os.environ.get("KAJOVOSPEND_LOG_RETENTION_DAYS", "7") or 7)
    lines_per_day = int(os.environ.get("KAJOVOSPEND_LOG_LINES_PER_DAY_ESTIMATE", "200000") or 200000)
    return max(1000, retention_days * lines_per_day)


def setup_logging(log_dir: Path, name: str = "kajovospend") -> logging.Logger:
    """
    Configures one shared log file:
      <log_dir>/kajovospend.log
    capped to the last ~5000 lines in a "ring" (old lines are overwritten).

    Console logging is OFF by default to keep "1 log". Enable via:
      KAJOVOSPEND_LOG_CONSOLE=1
    """
    global _ROOT_CONFIGURED

    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)

    max_lines = _compute_max_lines()

    with _ROOT_CONFIG_LOCK:
        if not _ROOT_CONFIGURED:
            log_file = log_dir / "kajovospend.log"
            detail = str(os.environ.get("KAJOVOSPEND_LOG_DETAIL", "1")).strip() not in {"0", "false", "False", "FALSE", "no", "NO"}

            root = logging.getLogger()
            root.setLevel(logging.DEBUG)

            fmt = logging.Formatter(
                "%(asctime)s.%(msecs)03d %(levelname)s "
                "pid=%(process)d tid=%(threadName)s "
                "host=%(hostname)s user=%(user)s "
                "[%(name)s:%(funcName)s:%(lineno)d] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )

            forensic_filter = ForensicContextFilter()

            fh = LineCappedFileHandler(log_file, max_lines=max_lines, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            fh.addFilter(forensic_filter)
            root.addHandler(fh)

            forensic_file = log_dir / "kajovospend_forensic.jsonl"
            fh_json = LineCappedFileHandler(forensic_file, max_lines=int(max_lines * 2), encoding="utf-8")
            fh_json.setLevel(logging.DEBUG)
            fh_json.setFormatter(JsonLineFormatter())
            fh_json.addFilter(forensic_filter)
            root.addHandler(fh_json)

            if os.environ.get("KAJOVOSPEND_LOG_CONSOLE", "").strip() in {"1", "true", "TRUE", "yes", "YES"}:
                ch = logging.StreamHandler()
                ch.setLevel(logging.DEBUG)
                ch.setFormatter(fmt)
                ch.addFilter(forensic_filter)
                root.addHandler(ch)

            setattr(root, "_kajovospend_log_detail", detail)
            _ROOT_CONFIGURED = True

    # let everything propagate into the single root handler
    logger.setLevel(logging.DEBUG)
    logger.propagate = True
    _install_forensic_runtime_hooks(logger, log_dir)
    retention_days = int(os.environ.get("KAJOVOSPEND_LOG_RETENTION_DAYS", "7") or 7)
    lines_per_day = int(os.environ.get("KAJOVOSPEND_LOG_LINES_PER_DAY_ESTIMATE", "200000") or 200000)
    detail_flag = str(os.environ.get("KAJOVOSPEND_LOG_DETAIL", "1")).strip() not in {"0", "false", "False", "FALSE", "no", "NO"}
    logger.info(
        "Logging inicializov?n: log_dir=%s pid=%s ppid=%s retention_days=%s max_lines=%s lines_per_day_estimate=%s detail=%s",
        log_dir,
        os.getpid(),
        os.getppid() if hasattr(os, "getppid") else "n/a",
        retention_days,
        max_lines,
        lines_per_day,
        int(detail_flag),
        extra={
            "event_name": "logging.start",
            "extra_payload": {
                "retention_days": retention_days,
                "max_lines": max_lines,
                "lines_per_day_estimate": lines_per_day,
                "detail": int(detail_flag),
            },
        },
    )
    return logger


def log_event(logger: logging.Logger, event_name: str, message: str, **extra: Any) -> None:
    """
    Helper pro strukturované logování:
    - doplní event_name a extra_payload
    - do textového logu přidá čitelný suffix key=value
    """
    extra_payload: Dict[str, Any] = extra or {}
    # respektuj detail flag (pokud je root logger bez detailu, omez velké hodnoty)
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
