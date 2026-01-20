from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Iterable

import logging
import platform
import sys

from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler


SUPPORTED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_EXT


class _Handler(FileSystemEventHandler):
    def __init__(self, on_file: Callable[[Path], None]):
        self.on_file = on_file

    def on_created(self, event):
        if event.is_directory:
            return
        p = Path(event.src_path)
        if is_supported(p):
            self.on_file(p)


class DirectoryWatcher:
    def __init__(self, directory: Path, on_file: Callable[[Path], None]):
        self.directory = directory
        self.on_file = on_file
        # Python 3.13 on Windows clashes with watchdog's WindowsApiEmitter `_handle`
        # attribute (watchdog uses the same name as threading.Thread), which explodes
        # with TypeError: 'handle' must be a _ThreadHandle. Prefer polling there,
        # but keep the faster native observer elsewhere.
        if platform.system() == "Windows" and sys.version_info >= (3, 13):
            self._observer = PollingObserver()
        else:
            self._observer = Observer()

    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        handler = _Handler(self.on_file)
        try:
            self._observer.schedule(handler, str(self.directory), recursive=False)
            self._observer.start()
        except TypeError as exc:
            # Fallback for environments where the native observer breaks (see above).
            logging.getLogger(__name__).warning("watchdog native observer failed (%s), falling back to polling", exc)
            self._observer = PollingObserver()
            self._observer.schedule(handler, str(self.directory), recursive=False)
            self._observer.start()

    def stop(self):
        try:
            self._observer.stop()
            self._observer.join(timeout=5)
        except Exception:
            pass


def scan_directory(directory: Path) -> Iterable[Path]:
    if not directory.exists():
        return []
    for name in os.listdir(directory):
        p = directory / name
        if p.is_file() and is_supported(p):
            yield p
