from __future__ import annotations

import re
import datetime as dt
from dataclasses import dataclass
from typing import List, Optional, Tuple

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


def _norm_amount(s: str) -> float:
    s = s.replace("\xa0", " ")
    s = s.strip()
    s = s.replace(" ", "")
    s = s.replace(",", ".")
    return float(s)


def _find_first(patterns: list[re.Pattern], text: str) -> Optional[str]:
    for p in patterns:
        m = p.search(text)
        if m:
            return m.group(1).strip()
    return None


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

    ico = _find_first([
        re.compile(r"\bIČ\s*[: ]\s*(\d{8})\b", re.IGNORECASE),
        re.compile(r"\bICO\s*[: ]\s*(\d{8})\b", re.IGNORECASE),
        re.compile(r"\b(\d{8})\b")
    ], t)

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
                qty = float(m.group("qty").replace(",", "."))
                vat = float(m.group("vat"))
                line_total = _norm_amount(m.group("total"))
                items.append({"name": name, "quantity": qty, "vat_rate": vat, "line_total": line_total})
            except Exception:
                continue

    # receipts (Albert): lines like "2 x 5,60 Kč 11,20"
    if not items:
        rec_pat = re.compile(r"^(?P<name>[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ0-9 .,'/-]{3,})\s*$")
        qty_price_pat = re.compile(r"^(?P<qty>\d+(?:[.,]\d+)?)\s*[xX]\s*(?P<unit>\d+[\s\d]*[.,]\d{2}).*?(?P<total>\d+[\s\d]*[.,]\d{2})\s*(?:[A-Z])?\s*$")
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
                    qty = float(m2.group("qty").replace(",", "."))
                    line_total = _norm_amount(m2.group("total"))
                    # VAT letter A/B not reliable
                    items.append({"name": pending_name, "quantity": qty, "vat_rate": 0.0, "line_total": line_total})
                except Exception:
                    pass
                pending_name = None
            else:
                pending_name = None

    # confidence heuristic
    reasons: List[str] = []
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

    requires_review = conf < 0.75
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
