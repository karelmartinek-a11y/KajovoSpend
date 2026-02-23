from __future__ import annotations

import re
from dataclasses import dataclass
from statistics import median
from typing import Iterable, List, Optional, Sequence


@dataclass
class LayoutOcrItem:
    box: List[List[float]]
    text: str
    confidence: float = 0.0


_AMOUNT_RE = re.compile(r"^-?\d[\d\s]*[.,]\d{2}$")
_NUMBER_RE = re.compile(r"^-?\d+(?:[.,]\d+)?$")
_VAT_RE = re.compile(r"^(\d{1,2})(?:\s*%)?$")


def _f(v: str | float | int | None, default: float = 0.0) -> float:
    if v is None:
        return float(default)
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("\xa0", " ").replace(" ", "").replace(",", ".")
    if not s:
        return float(default)
    try:
        return float(s)
    except Exception:
        return float(default)


def _norm_token(t: str) -> str:
    return str(t or "").strip().replace("\xa0", " ")


def _is_amount_token(t: str) -> bool:
    tt = _norm_token(t)
    tt = re.sub(r"\s*(Kč|CZK|EUR)$", "", tt, flags=re.IGNORECASE)
    return bool(_AMOUNT_RE.match(tt))


def _parse_amount_token(t: str) -> Optional[float]:
    tt = _norm_token(t)
    tt = re.sub(r"\s*(Kč|CZK|EUR)$", "", tt, flags=re.IGNORECASE)
    if not _AMOUNT_RE.match(tt):
        return None
    return _f(tt, 0.0)


def _parse_qty_token(t: str) -> Optional[float]:
    tt = _norm_token(t)
    if not _NUMBER_RE.match(tt):
        return None
    q = _f(tt, 0.0)
    if q <= 0:
        return None
    if q > 10000:
        return None
    return q


def _parse_vat_token(t: str) -> Optional[float]:
    tt = _norm_token(t).replace(" ", "")
    m = _VAT_RE.match(tt)
    if not m:
        return None
    v = _f(m.group(1), 0.0)
    if v > 30:
        return None
    return v


def _box_metrics(item: LayoutOcrItem) -> tuple[float, float, float, float, str, float]:
    if not item.box:
        return (0.0, 0.0, 0.0, 0.0, _norm_token(item.text), float(item.confidence or 0.0))
    xs = [float(p[0]) for p in item.box]
    ys = [float(p[1]) for p in item.box]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    yc = (y0 + y1) / 2.0
    h = max(1.0, y1 - y0)
    return (yc, x0, x1, h, _norm_token(item.text), float(item.confidence or 0.0))


def _cluster_rows(items: Sequence[LayoutOcrItem]) -> List[List[tuple[float, float, float, float, str, float]]]:
    rows = [_box_metrics(it) for it in items if _norm_token(it.text)]
    if not rows:
        return []
    heights = [r[3] for r in rows]
    med_h = float(median(heights)) if heights else 12.0
    tol = max(5.0, 0.60 * med_h)

    rows.sort(key=lambda r: r[0])
    buckets: List[List[tuple[float, float, float, float, str, float]]] = []
    for r in rows:
        if not buckets:
            buckets.append([r])
            continue
        last_y = float(sum(x[0] for x in buckets[-1]) / max(1, len(buckets[-1])))
        if abs(r[0] - last_y) <= tol:
            buckets[-1].append(r)
        else:
            buckets.append([r])

    for b in buckets:
        b.sort(key=lambda x: x[1])
    return buckets


def _detect_vat_default(document_text: str | None) -> float:
    t = str(document_text or "")
    m = re.search(r"\b(21|15|12|10)\s*%", t)
    if not m:
        return 0.0
    return _f(m.group(1), 0.0)


def _row_to_item(row: List[tuple[float, float, float, float, str, float]], vat_default: float) -> Optional[dict]:
    toks = [x[4] for x in row if x[4]]
    if not toks:
        return None

    amount_positions = [(i, _parse_amount_token(tok)) for i, tok in enumerate(toks)]
    amount_positions = [(i, v) for i, v in amount_positions if v is not None]
    if not amount_positions:
        return None

    # rightmost amount bereme jako line_total (gross)
    gross_idx, gross_val = amount_positions[-1]

    vat_rate: Optional[float] = None
    qty_idx: Optional[int] = None
    for i, tok in enumerate(toks):
        if i >= gross_idx:
            break
        if _is_amount_token(tok):
            continue
        q = _parse_qty_token(tok)
        if q is not None:
            qty_idx = i
            break

    for i in range(max(0, gross_idx - 1), -1, -1):
        tok = toks[i]
        vr = _parse_vat_token(tok)
        if vr is None:
            continue
        # nepřepisuj qty sloupec jako VAT (typicky první malé číslo vlevo)
        if qty_idx is not None and i == qty_idx and _norm_token(tok) in {"1", "2", "3", "4", "5"}:
            continue
        vat_rate = vr
        break
    if vat_rate is None:
        vat_rate = vat_default

    qty: Optional[float] = None
    for i, tok in enumerate(toks):
        if i >= gross_idx:
            break
        if _is_amount_token(tok):
            continue
        q = _parse_qty_token(tok)
        if q is not None:
            qty = q
            break
    if qty is None:
        qty = 1.0

    unit_gross: Optional[float] = None
    if len(amount_positions) >= 2:
        # amount pred gross často bývá unit price gross
        unit_gross = amount_positions[-2][1]

    if unit_gross is None:
        unit_gross = gross_val / qty if qty else gross_val

    # name = text před číselnou částí
    name_toks: List[str] = []
    stop_i = min(gross_idx, amount_positions[0][0] if amount_positions else gross_idx)
    for i, tok in enumerate(toks):
        if i >= stop_i:
            break
        if _parse_qty_token(tok) is not None or _is_amount_token(tok) or _parse_vat_token(tok) is not None:
            continue
        name_toks.append(tok)
    name = " ".join(name_toks).strip() or "Položka"

    unit_net = unit_gross / (1.0 + vat_rate / 100.0) if vat_rate > 0 else unit_gross
    line_net = gross_val / (1.0 + vat_rate / 100.0) if vat_rate > 0 else gross_val
    vat_amount = gross_val - line_net

    return {
        "name": name,
        "quantity": round(float(qty), 3),
        "unit_price": round(float(unit_net), 4),
        "vat_rate": round(float(vat_rate), 2),
        "line_total": round(float(gross_val), 2),
        "unit_price_net": round(float(unit_net), 4),
        "unit_price_gross": round(float(unit_gross), 4),
        "line_total_net": round(float(line_net), 2),
        "line_total_gross": round(float(gross_val), 2),
        "vat_amount": round(float(vat_amount), 2),
        "vat_code": None,
    }


def extract_items_from_ocr_layout(
    ocr_items: Iterable[LayoutOcrItem],
    *,
    document_text: str | None = None,
) -> List[dict]:
    """Layout-aware extrakce položek z OCR bbox tokenů.

    Výstup je v kanonickém formátu položek (unit_price=net, line_total=gross).
    """
    items = [it for it in ocr_items if _norm_token(getattr(it, "text", ""))]
    if not items:
        return []

    rows = _cluster_rows(items)
    vat_default = _detect_vat_default(document_text)

    out: List[dict] = []
    for row in rows:
        item = _row_to_item(row, vat_default=vat_default)
        if not item:
            continue
        # odfiltruj souhrnné řádky
        name_l = str(item.get("name") or "").lower()
        if any(k in name_l for k in ["celkem", "součet", "rekapitulace", "základ", "dph"]):
            continue
        out.append(item)

    return out
