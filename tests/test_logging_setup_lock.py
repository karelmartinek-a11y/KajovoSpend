from pathlib import Path

from kajovospend.utils.logging_setup import _InterProcessLock


def test_interprocess_lock_is_reentrant_within_same_thread(tmp_path: Path):
    lock_path = tmp_path / "kajovospend.log.lock"

    with _InterProcessLock(lock_path):
        with _InterProcessLock(lock_path):
            assert lock_path.exists()
