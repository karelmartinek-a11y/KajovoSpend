from __future__ import annotations

from typing import Any, Dict, List, Tuple


def _f(v: Any, default: float = 0.0) -> float:
    if v is None:
        return float(default)
    if isinstance(v, (int, float)):
        return float(v)
    try:
        s = str(v).strip().replace("\xa0", " ").replace(" ", "").replace(",", ".")
        return float(s) if s else float(default)
    except Exception:
        return float(default)


def _r2(v: float) -> float:
    return round(float(v), 2)


def _r4(v: float) -> float:
    return round(float(v), 4)


def _vat_code_from_rate(vat_rate: float) -> str:
    r = _r2(vat_rate)
    if abs(r) < 1e-9:
        return "ZERO"
    if abs(r - 10.0) < 1e-9:
        return "REDUCED_2"
    if abs(r - 12.0) < 1e-9:
        return "REDUCED_0"
    if abs(r - 15.0) < 1e-9:
        return "REDUCED_1"
    if abs(r - 21.0) < 1e-9:
        return "STANDARD"
    return f"RATE_{str(r).replace('.', '_')}"


def compute_item_derivations(item: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministicky dopočítá net/gross/VAT pole položky.

    Vstup očekává kanonický model položky:
    - unit_price = unit net (legacy)
    - line_total = line gross (legacy)
    """
    out = dict(item or {})

    qty = _f(out.get("quantity"), 1.0)
    if qty == 0.0:
        qty = 1.0
    vat_rate = _f(out.get("vat_rate"), 0.0)

    unit_net = out.get("unit_price_net")
    if unit_net is None and out.get("unit_price") is not None:
        unit_net = _f(out.get("unit_price"), 0.0)
    unit_net_f = None if unit_net is None else _f(unit_net, 0.0)

    line_gross = out.get("line_total_gross")
    if line_gross is None and out.get("line_total") is not None:
        line_gross = _f(out.get("line_total"), 0.0)
    line_gross_f = None if line_gross is None else _f(line_gross, 0.0)

    line_net_f = None if out.get("line_total_net") is None else _f(out.get("line_total_net"), 0.0)
    unit_gross_f = None if out.get("unit_price_gross") is None else _f(out.get("unit_price_gross"), 0.0)

    if line_net_f is None and unit_net_f is not None:
        line_net_f = _r2(unit_net_f * qty)
    if line_gross_f is None and line_net_f is not None:
        line_gross_f = _r2(line_net_f * (1.0 + vat_rate / 100.0)) if vat_rate > 0 else _r2(line_net_f)
    if line_net_f is None and line_gross_f is not None:
        line_net_f = _r2(line_gross_f / (1.0 + vat_rate / 100.0)) if vat_rate > 0 else _r2(line_gross_f)

    if unit_net_f is None and line_net_f is not None and qty != 0.0:
        unit_net_f = _r4(line_net_f / qty)
    if unit_gross_f is None and line_gross_f is not None and qty != 0.0:
        unit_gross_f = _r4(line_gross_f / qty)

    vat_amount_f = None
    if line_gross_f is not None and line_net_f is not None:
        vat_amount_f = _r2(line_gross_f - line_net_f)

    out["quantity"] = qty
    out["unit_price"] = unit_net_f
    out["line_total"] = _r2(line_gross_f) if line_gross_f is not None else 0.0

    out["unit_price_net"] = unit_net_f
    out["unit_price_gross"] = unit_gross_f
    out["line_total_net"] = line_net_f
    out["line_total_gross"] = line_gross_f
    out["vat_amount"] = vat_amount_f
    out["vat_code"] = str(out.get("vat_code") or _vat_code_from_rate(vat_rate))
    return out


def compute_document_totals(
    items: List[Dict[str, Any]],
    total_with_vat: float | None,
    total_without_vat_hint: float | None = None,
    *,
    tolerance_abs: float = 2.0,
    tolerance_rel: float = 0.03,
) -> Tuple[float | None, float | None, float | None, List[Dict[str, Any]], Dict[str, bool]]:
    sum_net = 0.0
    sum_gross = 0.0
    has_net = False
    has_gross = False
    by_rate: Dict[float, Dict[str, float]] = {}

    for it in items or []:
        d = compute_item_derivations(it)
        rate = _r2(_f(d.get("vat_rate"), 0.0))
        ln = d.get("line_total_net")
        lg = d.get("line_total_gross")
        va = d.get("vat_amount")

        if ln is not None:
            ln_f = _f(ln, 0.0)
            sum_net += ln_f
            has_net = True
        else:
            ln_f = 0.0
        if lg is not None:
            lg_f = _f(lg, 0.0)
            sum_gross += lg_f
            has_gross = True
        else:
            lg_f = 0.0
        if va is not None:
            va_f = _f(va, 0.0)
        else:
            va_f = _r2(lg_f - ln_f)

        row = by_rate.setdefault(rate, {"rate": rate, "net": 0.0, "vat": 0.0, "gross": 0.0})
        row["net"] += ln_f
        row["vat"] += va_f
        row["gross"] += lg_f

    net = _r2(sum_net) if has_net else None
    gross = _r2(sum_gross) if has_gross else None

    if total_with_vat is not None:
        gross = _r2(_f(total_with_vat, 0.0))
    if total_without_vat_hint is not None:
        net = _r2(_f(total_without_vat_hint, 0.0))

    vat = _r2((gross or 0.0) - (net or 0.0)) if (gross is not None and net is not None) else None

    breakdown = []
    for rate in sorted(by_rate.keys()):
        row = by_rate[rate]
        breakdown.append(
            {
                "vat_rate": _r2(row["rate"]),
                "net": _r2(row["net"]),
                "vat": _r2(row["vat"]),
                "gross": _r2(row["gross"]),
                "vat_code": _vat_code_from_rate(_r2(row["rate"])),
            }
        )

    def _ok(sum_val: float | None, target: float | None) -> bool:
        if target is None or sum_val is None:
            return True
        diff = abs(sum_val - target)
        rel = diff / max(abs(target), 1e-9)
        return (diff <= tolerance_abs) or (rel <= tolerance_rel)

    flags = {
        "sum_ok_gross": _ok(_r2(sum_gross) if has_gross else None, _r2(_f(total_with_vat, 0.0)) if total_with_vat is not None else None),
        "sum_ok_net": _ok(_r2(sum_net) if has_net else None, _r2(_f(total_without_vat_hint, 0.0)) if total_without_vat_hint is not None else (net if total_with_vat is None else None)),
    }
    flags["sum_ok"] = bool(flags["sum_ok_gross"] and flags["sum_ok_net"])
    return net, vat, gross, breakdown, flags
