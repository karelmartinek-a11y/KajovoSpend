import logging
import os

from kajovospend.utils import logging_setup


def test_compute_max_lines_env(monkeypatch):
    monkeypatch.setenv("KAJOVOSPEND_LOG_MAX_LINES", "")
    monkeypatch.setenv("KAJOVOSPEND_LOG_RETENTION_DAYS", "3")
    monkeypatch.setenv("KAJOVOSPEND_LOG_LINES_PER_DAY_ESTIMATE", "1000")
    val = logging_setup._compute_max_lines()
    assert val == 3000


def test_log_event_respects_detail(monkeypatch, caplog):
    root = logging.getLogger()
    caplog.set_level(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    try:
        setattr(root, "_kajovospend_log_detail", False)
        logging_setup.log_event(logging.getLogger("kajovospend.test"), "detail.test", "msg", big="x" * 1000, small="ok")
    finally:
        root.removeHandler(handler)
    assert "small='ok'" in caplog.text
    # big should be trimmed away when detail disabled
    assert "big=" not in caplog.text
