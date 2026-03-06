from __future__ import annotations

from kajovospend.service.processor import Processor


def test_primary_disabled_skips_primary_branch_gate() -> None:
    flags = Processor._resolve_openai_runtime_flags(
        {
            "enabled": True,
            "auto_enable": False,
            "primary_enabled": False,
            "fallback_enabled": True,
            "only_openai": False,
        },
        {"openai_fallback": {"enabled": False}},
        api_key="sk-abcdefghijklmnopqrstuvwxyz123456",
        backend_available=True,
    )
    assert flags["openai_available"] is True
    assert flags["primary_allowed"] is False
    assert flags["fallback_allowed"] is True


def test_fallback_disabled_skips_fallback_branch_gate() -> None:
    flags = Processor._resolve_openai_runtime_flags(
        {
            "enabled": True,
            "auto_enable": False,
            "primary_enabled": True,
            "fallback_enabled": False,
            "only_openai": False,
        },
        {"openai_fallback": {"enabled": False}},
        api_key="sk-abcdefghijklmnopqrstuvwxyz123456",
        backend_available=True,
    )
    assert flags["openai_available"] is True
    assert flags["primary_allowed"] is True
    assert flags["fallback_allowed"] is False


def test_only_openai_without_api_key_reports_reason() -> None:
    flags = Processor._resolve_openai_runtime_flags(
        {
            "enabled": True,
            "auto_enable": False,
            "primary_enabled": True,
            "fallback_enabled": True,
            "only_openai": True,
        },
        {"openai_fallback": {"enabled": False}},
        api_key="",
        backend_available=True,
    )
    notes = Processor._openai_only_mode_notes(
        openai_only=flags["openai_only"],
        has_api_key=flags["has_api_key"],
        openai_enabled=flags["openai_available"],
        primary_enabled=flags["primary_enabled"],
        fallback_enabled=flags["fallback_enabled"],
    )
    assert "OpenAI only režim: chybí API key – online extrakce přeskočena" in notes


def test_only_openai_combination_is_deterministic() -> None:
    flags = Processor._resolve_openai_runtime_flags(
        {
            "enabled": True,
            "auto_enable": False,
            "primary_enabled": False,
            "fallback_enabled": True,
            "only_openai": True,
        },
        {"openai_fallback": {"enabled": False}},
        api_key="sk-abcdefghijklmnopqrstuvwxyz123456",
        backend_available=True,
    )
    assert flags["openai_only"] is True
    assert flags["openai_available"] is True
    assert flags["primary_allowed"] is False
    assert flags["fallback_allowed"] is True
