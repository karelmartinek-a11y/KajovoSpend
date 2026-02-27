from __future__ import annotations

from pathlib import Path

from kajovospend.utils import paths


def test_default_data_dir_macos(monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    result = paths.default_data_dir()
    assert result == Path.home() / "Library" / "Application Support" / "KajovoSpend"


def test_default_data_dir_windows_env(monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", "/tmp/localapp")
    monkeypatch.delenv("APPDATA", raising=False)
    result = paths.default_data_dir()
    assert result == Path("/tmp/localapp") / "KajovoSpend"
