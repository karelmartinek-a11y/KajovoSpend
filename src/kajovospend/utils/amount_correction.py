from __future__ import annotations

import re
from typing import Callable, Iterable, List, Tuple


_OCR_CHAR_MAP = {
    "O": "0",
    "o": "0",
    "l": "1",
    "I": "1",
    "S": "5",
    "s": "5",
    "B": "8",
}


def normalize_ocr_amount_token(token: str) -> tuple[str, bool]:
    """Normalizuje běžné OCR záměny znaků v numerických tokenech."""
    raw = str(token or "")
    if not raw:
        return "", False
    out = "".join(_OCR_CHAR_MAP.get(ch, ch) for ch in raw)
    out = out.replace("\xa0", " ").strip()
    return out, (out != raw)


def generate_decimal_candidates(token: str) -> list[str]:
    """Generuje kandidáty pro desetinnou čárku/tečku z OCR tokenu."""
    norm, _ = normalize_ocr_amount_token(token)
    compact = re.sub(r"\s+", "", norm)
    compact = re.sub(r"\s*(Kč|CZK|EUR)$", "", compact, flags=re.IGNORECASE)
    compact = compact.replace(".", ",")
    if not compact:
        return []

    # pokud už obsahuje desetinnou část, ponech i variantu s tečkou
    if re.fullmatch(r"-?\d+[,.]\d{2}", compact):
        return [compact, compact.replace(",", ".")]

    # jen číslice -> kandidáti s desetinnou čárkou na posledních 2 místech
    digits = re.sub(r"\D+", "", compact)
    if len(digits) < 3:
        return []

    cands: list[str] = []
    base = f"{digits[:-2]},{digits[-2:]}"
    cands.append(base)
    cands.append(base.replace(",", "."))

    # OCR občas vloží/ubere jednu číslici => zkus i sousední dělení
    if len(digits) >= 4:
        c2 = f"{digits[:-3]},{digits[-3:-1]}"
        cands.append(c2)
        cands.append(c2.replace(",", "."))
    return list(dict.fromkeys(cands))


def parse_amount_candidates(token: str) -> list[float]:
    out: list[float] = []
    for cand in generate_decimal_candidates(token):
        try:
            out.append(float(cand.replace(" ", "").replace(",", ".")))
        except Exception:
            continue
    return list(dict.fromkeys(out))


def validate_candidates_against_invariant(
    candidates: Iterable[float],
    *,
    validator: Callable[[float], bool],
) -> list[float]:
    """Filtr kandidátů podle účetní invarianty (např. sedí na celku)."""
    out: list[float] = []
    for c in candidates:
        try:
            if validator(float(c)):
                out.append(float(c))
        except Exception:
            continue
    return out


def choose_best_candidate(candidates: Iterable[float], *, original_guess: float | None = None) -> float | None:
    vals = [float(x) for x in candidates]
    if not vals:
        return None
    if original_guess is None:
        return vals[0]
    return min(vals, key=lambda x: abs(float(x) - float(original_guess)))
