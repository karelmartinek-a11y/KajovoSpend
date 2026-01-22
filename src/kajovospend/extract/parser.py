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
                qty = float(m.group("qty").replace(",", "."))
                vat = float(m.group("vat"))
                line_total = _norm_amount(m.group("total"))
                items.append({"name": name, "quantity": qty, "vat_rate": vat, "line_total": line_total})
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
                    qty = float(m2.group("qty").replace(",", "."))
                    line_total = _norm_amount(m2.group("total"))
                    vat_letter = (m2.group("vat_letter") or "").strip().upper()
                    vat = _VAT_LETTER_MAP.get(vat_letter, 0.0)
                    items.append({"name": pending_name, "quantity": qty, "vat_rate": vat, "line_total": line_total})
                except Exception:
                    pass
                pending_name = None
            else:
                pending_name = None

    # --- kontrola součtu položek vs. celkem ---
    # Cíl: co nejvíc dokladů vyřešit offline, a na OpenAI posílat jen minimum.
    reasons: List[str] = []
    if items and total is not None and total > 0:
        try:
            sum_items = float(sum(float(i.get("line_total") or 0.0) for i in items))
            rel_diff = abs(sum_items - total) / max(total, 1e-9)
            # běžně rounding/DPH rozdíly do ~1.5%; necháme rezervu i pro různé formáty => 3%
            tol = 0.03
            if rel_diff > tol:
                # pokus: položky jsou bez DPH, ale celkem je s DPH
                sum_items_gross = 0.0
                for it in items:
                    lt = float(it.get("line_total") or 0.0)
                    vr = float(it.get("vat_rate") or 0.0)
                    if vr > 0:
                        sum_items_gross += lt * (1.0 + vr / 100.0)
                    else:
                        sum_items_gross += lt
                rel_diff2 = abs(sum_items_gross - total) / max(total, 1e-9)
                if rel_diff2 + 1e-9 < rel_diff and rel_diff2 <= tol:
                    # upravíme line_total na částky s DPH pro položky s DPH > 0
                    for it in items:
                        lt = float(it.get("line_total") or 0.0)
                        vr = float(it.get("vat_rate") or 0.0)
                        if vr > 0:
                            it["line_total"] = round(lt * (1.0 + vr / 100.0), 2)
                    reasons.append("položky přepočteny z bez DPH na s DPH")
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
