from __future__ import annotations

import re
import string
import unicodedata
from typing import Any, Dict, List, Tuple

_AMOUNT_RE = re.compile(r"\b\d{1,6}(?:[ \u00a0]\d{3})*(?:[.,]\d{2})\b")

_TOKEN_GROUPS = [
    # G1 currency / money
    ("kč", "kc", "czk", "eur", "usd"),
    # G2 totals
    ("celkem", "k úhradě", "součet", "total"),
    # G3 identification
    ("ičo", "ico", "dič", "dic"),
    # G4 dates-ish
    ("datum", "vystaven", "splatn", "duzp", "zdanit"),
    # G5 doc type
    ("faktura", "daňový doklad", "uctenka", "účtenka", "pokladna", "prodej", "paragon"),
]


def _clamp(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else float(x)


def text_quality_score(text: str) -> Tuple[float, Dict[str, Any]]:
    """
    Deterministické skóre kvality textu (0..1) dle pevné specifikace.
    Vrací: (score, metrics) kde metrics obsahuje i dílčí složky pro audit/debug.
    """
    t = (text or "").replace("\xa0", " ").strip()
    N = len(t)
    if N == 0:
        return 0.0, {
            "N": 0,
            "token_groups": 0,
            "amount_matches": 0,
            "lines": 0,
            "score": 0.0,
        }

    non_ws = 0
    alnum = 0
    punct = 0
    ctrl = 0
    repl = t.count("\ufffd")

    for ch in t:
        if ch.isspace():
            continue
        non_ws += 1
        if ch.isalnum():
            alnum += 1
        cat = unicodedata.category(ch)
        if cat.startswith("P") or ch in string.punctuation:
            punct += 1
        if cat.startswith("C") and ch not in "\t\n\r":
            ctrl += 1

    ctrl += repl
    whitespace = N - non_ws

    alnum_ratio = alnum / max(1, non_ws)
    whitespace_ratio = whitespace / max(1, N)
    punct_ratio = punct / max(1, non_ws)

    lower = t.lower()
    token_groups = 0
    for grp in _TOKEN_GROUPS:
        if any(tok in lower for tok in grp):
            token_groups += 1

    amount_matches = len(_AMOUNT_RE.findall(t))
    lines = sum(1 for ln in t.splitlines() if ln.strip())

    # max non-space run
    max_run = 0
    for m in re.finditer(r"\S+", t):
        ml = len(m.group(0))
        if ml > max_run:
            max_run = ml

    # components 0..1 (pevné transformace)
    c_len = _clamp(N / 600.0)
    c_alnum = _clamp((alnum_ratio - 0.45) / 0.35)
    c_space = _clamp(1.0 - (abs(whitespace_ratio - 0.22) / 0.22))
    c_tokens = _clamp(token_groups / 3.0)
    c_amounts = _clamp(amount_matches / 3.0)
    c_lines = _clamp(lines / 10.0)

    # penalties 0..1 (pevné transformace)
    p_ctrl = _clamp(ctrl / 3.0)
    p_run = _clamp((max_run - 40.0) / 60.0)
    p_punct = _clamp((punct_ratio - 0.25) / 0.25)

    positives = (
        0.15 * c_len
        + 0.25 * c_alnum
        + 0.15 * c_space
        + 0.20 * c_tokens
        + 0.10 * c_amounts
        + 0.15 * c_lines
    )
    penalties = (0.20 * p_ctrl) + (0.20 * p_run) + (0.15 * p_punct)
    score = _clamp(positives - penalties)

    metrics: Dict[str, Any] = {
        "N": N,
        "non_ws": non_ws,
        "alnum_ratio": alnum_ratio,
        "whitespace_ratio": whitespace_ratio,
        "punct_ratio": punct_ratio,
        "token_groups": token_groups,
        "amount_matches": amount_matches,
        "lines": lines,
        "ctrl": ctrl,
        "max_run": max_run,
        "c_len": c_len,
        "c_alnum": c_alnum,
        "c_space": c_space,
        "c_tokens": c_tokens,
        "c_amounts": c_amounts,
        "c_lines": c_lines,
        "p_ctrl": p_ctrl,
        "p_run": p_run,
        "p_punct": p_punct,
        "positives": positives,
        "penalties": penalties,
        "score": score,
    }
    return score, metrics


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
