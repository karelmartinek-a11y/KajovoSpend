from __future__ import annotations

from typing import Any, Dict, List


def compute_text_quality(text: str) -> Dict[str, Any]:
    t = text or ""
    total = len(t)
    non_ws = sum(1 for ch in t if not ch.isspace())
    printable = sum(1 for ch in t if ch.isprintable())
    letters = sum(1 for ch in t if ch.isalpha())
    digits = sum(1 for ch in t if ch.isdigit())
    repl = t.count("\ufffd")
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    avg_line_len = (sum(len(ln) for ln in lines) / len(lines)) if lines else 0.0
    unique_ratio = (len(set(t)) / total) if total else 0.0

    def _r(num: int, den: int) -> float:
        return float(num) / float(den) if den else 0.0

    return {
        "chars_total": int(total),
        "chars_non_ws": int(non_ws),
        "chars_printable": int(printable),
        "chars_letters": int(letters),
        "chars_digits": int(digits),
        "replacement_chars": int(repl),
        "lines_nonempty": int(len(lines)),
        "avg_line_len": float(avg_line_len),
        "unique_char_ratio": float(unique_ratio),
        "ratio_non_ws": _r(non_ws, total),
        "ratio_printable": _r(printable, total),
        "ratio_letters": _r(letters, non_ws),
        "ratio_digits": _r(digits, non_ws),
        "ratio_replacement": _r(repl, total),
    }


def summarize_text_quality(metrics: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not metrics:
        return {
            "pages": 0,
            "pages_nonempty": 0,
            "chars_total": 0,
            "chars_non_ws": 0,
            "ratio_printable": 0.0,
            "ratio_non_ws": 0.0,
            "ratio_letters": 0.0,
            "ratio_digits": 0.0,
            "ratio_replacement": 0.0,
            "avg_line_len": 0.0,
        }

    pages = len(metrics)
    pages_nonempty = sum(1 for m in metrics if int(m.get("chars_non_ws") or 0) > 0)
    total = sum(int(m.get("chars_total") or 0) for m in metrics)
    non_ws = sum(int(m.get("chars_non_ws") or 0) for m in metrics)
    printable = sum(int(m.get("chars_printable") or 0) for m in metrics)
    letters = sum(int(m.get("chars_letters") or 0) for m in metrics)
    digits = sum(int(m.get("chars_digits") or 0) for m in metrics)
    repl = sum(int(m.get("replacement_chars") or 0) for m in metrics)

    def _r(num: int, den: int) -> float:
        return float(num) / float(den) if den else 0.0

    # average line length: average of per-page averages weighted by nonempty lines count
    line_count = sum(int(m.get("lines_nonempty") or 0) for m in metrics)
    avg_line_len = (
        sum(float(m.get("avg_line_len") or 0.0) * float(int(m.get("lines_nonempty") or 0)) for m in metrics) / float(line_count)
        if line_count
        else 0.0
    )

    return {
        "pages": int(pages),
        "pages_nonempty": int(pages_nonempty),
        "chars_total": int(total),
        "chars_non_ws": int(non_ws),
        "ratio_printable": _r(printable, total),
        "ratio_non_ws": _r(non_ws, total),
        "ratio_letters": _r(letters, non_ws),
        "ratio_digits": _r(digits, non_ws),
        "ratio_replacement": _r(repl, total),
        "avg_line_len": float(avg_line_len),
    }
