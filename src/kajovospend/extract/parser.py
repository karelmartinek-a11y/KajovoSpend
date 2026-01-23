from __future__ import annotations

import re
import datetime as dt
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict

from dateutil import parser as dtparser


@dataclass
class Extracted:
    supplier_ico: Optional[str]
    doc_number: Optional[str]
    bank_account: Optional[str]
    issue_date: Optional[dt.date]
    total_with_vat: Optional[float]
    currency: str
    items: List[dict]
    confidence: float
    requires_review: bool
    review_reasons: List[str]
    full_text: str


_amount_re = re.compile(r"(-?\d+[\d\s]*[.,]\d{2})")
_ICO_CTX_RE = re.compile(r"\b(IČO|ICO|IČ)\b", re.IGNORECASE)
_ICO_DIGITS_RE = re.compile(r"\D+")

# Časté mapování DPH písmenem na účtenkách (není univerzální, ale pomáhá u velké části CZ retail).
_VAT_LETTER_MAP: Dict[str, float] = {"A": 21.0, "B": 15.0, "C": 10.0}


def _norm_amount(s: str) -> float:
    s = s.replace("\xa0", " ")
    s = s.strip()
    s = s.replace(" ", "")
    s = s.replace(",", ".")
    return float(s)

def _f(v, default: float = 0.0) -> float:
    if v is None:
        return float(default)
    if isinstance(v, (int, float)):
        return float(v)
    try:
        s = str(v).strip().replace("\xa0", " ").replace(" ", "").replace(",", ".")
        if not s:
            return float(default)
        return float(s)
    except Exception:
        return float(default)

def _normalize_items(items: List[dict], reasons: List[str]) -> None:
    """
    Sjednotí položky do deterministické podoby pro výpočty:
    - když chybí line_total a máme qty+unit_price -> dopočítá
    - když line_total zjevně obsahuje jednotkovou cenu (qty>1 a line_total≈unit_price) -> opraví na qty*unit_price
    """
    for it in items:
        q = _f(it.get("quantity"), 1.0)
        up = it.get("unit_price")
        upf = None if up is None else _f(up, 0.0)
        lt = _f(it.get("line_total"), 0.0)
        # dopočet, když line_total chybí
        if (lt <= 0.0) and (upf is not None) and (q > 0):
            it["line_total"] = round(q * upf, 2)
            continue
        # častý OCR/format case: line_total je ve skutečnosti jednotková cena
        if (upf is not None) and (q > 1.0) and (lt > 0.0):
            if abs(lt - upf) <= 0.02 and abs((q * upf) - lt) > 0.05:
                it["line_total"] = round(q * upf, 2)
                reasons.append("oprava položky: řádková cena dopočtena z qty*unit_price")


def _safe_float(s: str) -> float:
    return float(str(s).strip().replace("\xa0", " ").replace(" ", "").replace(",", "."))


def _find_first(patterns: list[re.Pattern], text: str) -> Optional[str]:
    for p in patterns:
        m = p.search(text)
        if m:
            return m.group(1).strip()
    return None


def _normalize_ico_soft(ico: str | None) -> str | None:
    if ico is None:
        return None
    raw = str(ico).strip()
    if not raw:
        return None
    digits = _ICO_DIGITS_RE.sub("", raw)
    if not digits:
        return None
    if len(digits) > 8:
        # když OCR slije více čísel – raději vrátit původní digit-only; verifikace ARES stejně rozhodne
        return digits
    return digits.zfill(8)


def _parse_date(s: str) -> Optional[dt.date]:
    s = s.strip()
    try:
        # support dd.mm.yyyy and dd/mm/yyyy and ISO
        d = dtparser.parse(s, dayfirst=True).date()
        return d
    except Exception:
        return None


def extract_from_text(text: str) -> Extracted:
    raw = text or ""
    t = raw

    # IČO: dříve se bralo libovolné 8-číslí => často sebralo VS/číslo dokladu.
    # Teď vyžadujeme kontext (IČO/ICO/IČ) a normalizujeme.
    ico = _find_first(
        [
            re.compile(r"\bIČO\s*[: ]\s*([0-9][0-9\s-]{6,}[0-9])\b", re.IGNORECASE),
            re.compile(r"\bICO\s*[: ]\s*([0-9][0-9\s-]{6,}[0-9])\b", re.IGNORECASE),
            re.compile(r"\bIČ\s*[: ]\s*([0-9][0-9\s-]{6,}[0-9])\b", re.IGNORECASE),
        ],
        t,
    )
    ico = _normalize_ico_soft(ico)

    doc_no = _find_first([
        re.compile(r"Č[ií]slo\s+faktury\s*[: ]\s*([\w-]+)", re.IGNORECASE),
        re.compile(r"DAŇOVÝ\s+DOKLAD\s+č\.?\s*([\w-]+)", re.IGNORECASE),
        re.compile(r"Faktura\s*-?\s*daňový\s+doklad\s+č\.?\s*([\w-]+)", re.IGNORECASE),
        re.compile(r"\bVS\s*[: ]\s*(\d{3,})\b", re.IGNORECASE),
    ], t)

    bank_account = _find_first([
        re.compile(r"\bIBAN\s*[: ]\s*([A-Z]{2}\d{2}[A-Z0-9]{10,})\b"),
        re.compile(r"\bÚčet\s*[: ]\s*(\d{6,}-?\d{2,}/\d{4})\b", re.IGNORECASE),
        re.compile(r"\b(\d{6,}-?\d{2,})\s*/\s*(\d{4})\b"),
    ], t)
    if bank_account and " " in bank_account:
        bank_account = bank_account.replace(" ", "")

    date_s = _find_first([
        re.compile(r"Datum\s+vystaven[ií]\s*[: ]\s*([0-9]{1,2}[./][0-9]{1,2}[./][0-9]{2,4})", re.IGNORECASE),
        re.compile(r"Datum\s*[: ]\s*([0-9]{1,2}[./][0-9]{1,2}[./][0-9]{2,4})", re.IGNORECASE),
        re.compile(r"\b([0-9]{2}/[0-9]{2}/[0-9]{4})\b"),
    ], t)
    issue_date = _parse_date(date_s) if date_s else None

    # currency
    currency = "CZK" if re.search(r"\bCZK\b|Kč", t) else "EUR" if re.search(r"\bEUR\b", t) else "CZK"

    # total
    total_s = _find_first([
        re.compile(r"CELKEM\s+K\s+ÚHRADĚ\s*\n?\s*([0-9\s]+[.,][0-9]{2})", re.IGNORECASE),
        re.compile(r"Celkem\s+k\s+úhradě\s*[: ]\s*([0-9\s]+[.,][0-9]{2})", re.IGNORECASE),
        re.compile(r"K\s+zaplacení\s+celkem\s+EUR\s*([0-9\s]+[.,][0-9]{2})", re.IGNORECASE),
        re.compile(r"Cena\s+celkem\s*([0-9\s]+[.,][0-9]{2})", re.IGNORECASE),
        re.compile(r"Koruna\s+česká\s+Kč\s*([0-9\s]+[.,][0-9]{2})", re.IGNORECASE),
    ], t)

    total = None
    if total_s:
        try:
            total = _norm_amount(total_s)
        except Exception:
            total = None

    items: List[dict] = []
    # attempt parse tabular items (e.g., Rohlik PDF text)
    # pattern: <name> <qty> ks <unit> Kč <vat%> % <...> <line_total> Kč
    line_pat = re.compile(
        r"^(?P<name>.+?)\s+(?P<qty>-?\d+(?:[.,]\d+)?)\s*(?:ks|x)?\s+(?P<unit>\d+[\s\d]*[.,]\d{2})\s*Kč\s+(?P<vat>\d{1,2})\s*%\s+.*?\s+(?P<total>-?\d+[\s\d]*[.,]\d{2})\s*Kč\s*$",
        re.IGNORECASE
    )

    for ln in t.splitlines():
        ln = ln.strip()
        if not ln or len(ln) < 6:
            continue
        m = line_pat.match(ln)
        if m:
            try:
                name = m.group("name").strip()
                qty = _safe_float(m.group("qty"))
                unit_price = _norm_amount(m.group("unit"))
                vat = float(m.group("vat"))
                line_total = _norm_amount(m.group("total"))
                items.append({"name": name, "quantity": qty, "unit_price": unit_price, "vat_rate": vat, "line_total": line_total})
            except Exception:
                continue

    # receipts (Albert): lines like "2 x 5,60 Kč 11,20"
    if not items:
        rec_pat = re.compile(r"^(?P<name>[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ0-9 .,'/-]{3,})\s*$")
        qty_price_pat = re.compile(
            r"^(?P<qty>\d+(?:[.,]\d+)?)\s*[xX]\s*(?P<unit>\d+[\s\d]*[.,]\d{2}).*?(?P<total>\d+[\s\d]*[.,]\d{2})\s*(?P<vat_letter>[A-Z])?\s*$"
        )
        pending_name: Optional[str] = None
        for ln in t.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            if pending_name is None:
                if rec_pat.match(ln) and not re.search(r"(Celkem|DPH|Datum|Děkujeme|Kč|EUR|IBAN)", ln, re.IGNORECASE):
                    pending_name = ln
                continue
            m2 = qty_price_pat.match(ln)
            if m2:
                try:
                    qty = _safe_float(m2.group("qty"))
                    unit_price = _norm_amount(m2.group("unit"))
                    line_total = _norm_amount(m2.group("total"))
                    vat_letter = (m2.group("vat_letter") or "").strip().upper()
                    vat = _VAT_LETTER_MAP.get(vat_letter, 0.0)
                    items.append({"name": pending_name, "quantity": qty, "unit_price": unit_price, "vat_rate": vat, "line_total": line_total})
                except Exception:
                    pass
                pending_name = None
            else:
                pending_name = None

    # --- kontrola součtu položek vs. celkem ---
    # Cíl: co nejvíc dokladů vyřešit offline, a na OpenAI posílat jen minimum.
    reasons: List[str] = []
    if items:
        _normalize_items(items, reasons)
    if items and total is not None and total > 0:
        try:
            sum_items = float(sum(_f(i.get("line_total"), 0.0) for i in items))
            diff = abs(sum_items - total)
            rel_diff = diff / max(total, 1e-9)

            # tolerance: kombinace relativní + absolutní (u malých účtenek absolutní hraje velkou roli)
            rel_tol = 0.03   # ~3% (rezerva pro různé formáty)
            abs_tol = 2.00   # 2 Kč (zaokrouhlení, drobné přepočty)

            def _ok(d: float, r: float) -> bool:
                return (d <= abs_tol) or (r <= rel_tol)

            if not _ok(diff, rel_diff):
                # pokus A: položky jsou bez DPH, celkem je s DPH
                sum_items_gross = 0.0
                for it in items:
                    lt = _f(it.get("line_total"), 0.0)
                    vr = _f(it.get("vat_rate"), 0.0)
                    sum_items_gross += (lt * (1.0 + vr / 100.0)) if vr > 0 else lt
                diff_g = abs(sum_items_gross - total)
                rel_g = diff_g / max(total, 1e-9)
                if _ok(diff_g, rel_g) and (diff_g + 1e-9 < diff):
                    for it in items:
                        lt = _f(it.get("line_total"), 0.0)
                        vr = _f(it.get("vat_rate"), 0.0)
                        if vr > 0:
                            it["line_total"] = round(lt * (1.0 + vr / 100.0), 2)
                    reasons.append("položky přepočteny z bez DPH na s DPH")
                else:
                    # pokus B: položky jsou s DPH, ale celkem je bez DPH (méně časté, ale existuje)
                    sum_items_net = 0.0
                    for it in items:
                        lt = _f(it.get("line_total"), 0.0)
                        vr = _f(it.get("vat_rate"), 0.0)
                        sum_items_net += (lt / (1.0 + vr / 100.0)) if vr > 0 else lt
                    diff_n = abs(sum_items_net - total)
                    rel_n = diff_n / max(total, 1e-9)
                    if _ok(diff_n, rel_n) and (diff_n + 1e-9 < diff):
                        for it in items:
                            lt = _f(it.get("line_total"), 0.0)
                            vr = _f(it.get("vat_rate"), 0.0)
                            if vr > 0:
                                it["line_total"] = round(lt / (1.0 + vr / 100.0), 2)
                        reasons.append("položky přepočteny ze s DPH na bez DPH")
                    else:
                        reasons.append("nesedí součet položek vs. celkem")
        except Exception:
            reasons.append("nelze ověřit součet položek")

    # confidence heuristic
    conf = 0.0
    if ico:
        conf += 0.25
    else:
        reasons.append("chybí IČO")
    if doc_no:
        conf += 0.15
    else:
        reasons.append("chybí číslo dokladu")
    if issue_date:
        conf += 0.15
    else:
        reasons.append("chybí datum")
    if total is not None:
        conf += 0.25
    else:
        reasons.append("chybí celková cena")
    if items:
        conf += 0.20
    else:
        reasons.append("chybí položky")

    # Pokud máme explicitní problém se součtem, zvyšujeme šanci karantény i když conf vyjde „OK“.
    requires_review = (conf < 0.75) or any("nesedí součet položek" in r for r in reasons)
    if requires_review:
        reasons.append("nízká jistota vytěžení")

    return Extracted(
        supplier_ico=ico,
        doc_number=doc_no,
        bank_account=bank_account,
        issue_date=issue_date,
        total_with_vat=total,
        currency=currency,
        items=items,
        confidence=min(conf, 1.0),
        requires_review=requires_review,
        review_reasons=reasons,
        full_text=raw,
    )
