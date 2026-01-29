from __future__ import annotations

import re
import datetime as dt
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict, Iterable

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

# Položky typu „zaokrouhlení“ (často samostatný řádek na účtence)
_ROUNDING_RE = re.compile(
    r"\b(zaokrouhlen[ií]|zaokr\.?)(?:\s*[: ]\s*)?(?P<amount>-?\d+[\d\s]*[.,]\d{2})\b",
    re.IGNORECASE,
)

_NUM_ONLY_RE = re.compile(r"^-?\d+(?:[.,]\d+)?$")
_AMOUNT_ONLY_RE = re.compile(r"^-?\d[\d\s]*[.,]\d{2}\s*(?:Kč|CZK|EUR)?$", re.IGNORECASE)
_KC_AMOUNT_ONLY_RE = re.compile(r"^-?\d[\d\s]*[.,]\d{2}\s*Kč$", re.IGNORECASE)
_VAT_ONLY_RE = re.compile(r"^(\d{1,2})\s*%?$", re.IGNORECASE)
_STOP_ITEMS_RE = re.compile(
    r"^(Sleva|Základ|Zaklad|Cena celkem|Celkem|Rekapitulace|Součet|Soucet|Zbývá|Zbyva|Celkem k úhradě|CELKEM)\b",
    re.IGNORECASE,
)


def _extract_rounding_items(text: str) -> List[dict]:
    items: List[dict] = []
    if not text:
        return items
    for ln in text.splitlines():
        m = _ROUNDING_RE.search(ln)
        if not m:
            continue
        try:
            amt = _norm_amount(m.group("amount"))
        except Exception:
            continue
        # Zaokrouhlení je samostatná položka; DPH neaplikujeme.
        items.append(
            {
                "name": "Zaokrouhlení",
                "quantity": 1.0,
                "unit_price": amt,   # kanonicky: bez DPH; u zaokrouhlení je to stejné
                "vat_rate": 0.0,
                "line_total": amt,   # včetně DPH; u zaokrouhlení je to stejné
            }
        )
    return items


def _iter_non_rounding(items: Iterable[dict]) -> Iterable[dict]:
    for it in items:
        name = str(it.get("name") or "").strip().lower()
        if "zaokrouhl" in name or name in {"zaokr", "zaokr."}:
            continue
        yield it


def _canonicalize_items_to_unit_net_and_line_gross(
    items: List[dict],
    total_with_vat: Optional[float],
    reasons: List[str],
    *,
    rel_tol: float = 0.03,
    abs_tol: float = 2.0,
) -> bool:
    """
    Kanonická reprezentace položek pro výpočty a DB:
      - unit_price = cena za 1 jednotku bez DPH
      - line_total = řádková cena včetně DPH

    Vrací True, pokud součet položek sedí na total_with_vat v toleranci.
    """
    if not items:
        return False

    # nejdřív doplň chybějící line_total tam, kde to jde
    _normalize_items(items, reasons)

    # u mnoha dokladů jsou částky v položkách net (bez DPH), ale total je gross
    # => vyzkoušíme 2 režimy a vybereme ten s menší odchylkou.
    def _sum_for_mode(mode: str) -> float:
        s = 0.0
        for it in items:
            q = _f(it.get("quantity"), 1.0)
            vr = _f(it.get("vat_rate"), 0.0)
            lt = _f(it.get("line_total"), 0.0)
            up = it.get("unit_price")
            upf = None if up is None else _f(up, 0.0)
            base = lt if lt != 0.0 else (q * (upf or 0.0))
            if mode == "gross":
                s += base
            else:
                # net -> gross
                s += base * (1.0 + (vr / 100.0)) if vr > 0 else base
        return float(s)

    chosen = "gross"
    if total_with_vat is not None and total_with_vat != 0.0:
        sum_g = _sum_for_mode("gross")
        sum_n = _sum_for_mode("net")
        diff_g = abs(sum_g - total_with_vat)
        diff_n = abs(sum_n - total_with_vat)
        chosen = "net" if diff_n + 1e-9 < diff_g else "gross"

    # Kanonizace do (unit_net, line_gross)
    for it in items:
        name = str(it.get("name") or "").strip()
        q = _f(it.get("quantity"), 1.0)
        vr = _f(it.get("vat_rate"), 0.0)
        lt = _f(it.get("line_total"), 0.0)
        up = it.get("unit_price")
        upf = None if up is None else _f(up, 0.0)
        if q == 0.0:
            q = 1.0
            it["quantity"] = 1.0
            reasons.append("oprava položky: quantity=0 nahrazeno 1")

        # zaokrouhlení a podobné položky bereme jako gross==net
        if "zaokrouhl" in name.lower():
            it["vat_rate"] = 0.0
            it["unit_price"] = round(lt if lt != 0.0 else (upf or 0.0), 2)
            it["line_total"] = round(lt if lt != 0.0 else (upf or 0.0), 2)
            continue

        base = lt if lt != 0.0 else (q * (upf or 0.0))
        if chosen == "net":
            line_gross = base * (1.0 + (vr / 100.0)) if vr > 0 else base
            unit_net = (base / q) if q else 0.0
        else:
            line_gross = base
            unit_net = (base / q) / (1.0 + (vr / 100.0)) if (q and vr > 0) else ((base / q) if q else 0.0)

        # finální zápis
        it["unit_price"] = round(unit_net, 4)  # 4 desetinná místa zlepší následné přepočty
        it["line_total"] = round(line_gross, 2)

    # Ověření: podle unit_net * (1+vat) * qty musí sedět line_total a total
    sum_calc = 0.0
    for it in items:
        q = _f(it.get("quantity"), 1.0)
        vr = _f(it.get("vat_rate"), 0.0)
        upn = _f(it.get("unit_price"), 0.0)
        calc = (upn * (1.0 + vr / 100.0) * q) if vr > 0 else (upn * q)
        calc = round(calc, 2)
        lt = _f(it.get("line_total"), 0.0)
        # pokud se liší o víc než haléř, přepiš na konzistentní výsledek
        if abs(calc - lt) > 0.02:
            it["line_total"] = calc
            reasons.append("oprava položky: line_total přepočteno z unit_price_net*DPH*qty")
        sum_calc += _f(it.get("line_total"), 0.0)

    if total_with_vat is None or total_with_vat == 0.0:
        return False
    diff = abs(sum_calc - total_with_vat)
    rel = diff / max(abs(total_with_vat), 1e-9)
    return (diff <= abs_tol) or (rel <= rel_tol)


def _norm_amount(s: str) -> float:
    s = s.replace("\xa0", " ")
    s = s.strip()
    s = s.replace(" ", "")
    s = s.replace(",", ".")
    return float(s)

def _parse_number(s: str) -> Optional[float]:
    """
    Tolerantní parser pro čísla (množství i částky bez měny).
    Vrací None při nevalidním vstupu.
    """
    try:
        return float(str(s).replace("\xa0", " ").strip().replace(" ", "").replace(",", "."))
    except Exception:
        return None

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


def _looks_amount_line(ln: str) -> bool:
    return bool(_AMOUNT_ONLY_RE.fullmatch((ln or "").strip().replace("\xa0", " ")))


def _looks_kc_amount_line(ln: str) -> bool:
    return bool(_KC_AMOUNT_ONLY_RE.fullmatch((ln or "").strip().replace("\xa0", " ")))


def _parse_vat_only(ln: str) -> Optional[float]:
    m = _VAT_ONLY_RE.fullmatch((ln or "").strip())
    if not m:
        return None
    try:
        v = float(m.group(1))
    except Exception:
        return None
    if v > 30:
        return None
    return v


def _strip_currency(s: str) -> str:
    return re.sub(r"\s*(Kč|CZK|EUR)\s*$", "", (s or "").strip(), flags=re.IGNORECASE)


def _parse_items_rohlik_vertical(text: str) -> List[dict]:
    """
    Rohlík / Velká Pecka PDF: pypdf často vrací tabulku po buňkách (každý sloupec na vlastním řádku):
      <název...>
      <qty>
      <ks>
      <cena/jed vč DPH> Kč
      <vat> %
      <bez DPH> Kč
      <DPH> Kč
      <vč DPH> Kč
    """
    raw = text or ""
    u = raw.upper()
    if ("VELKÁ PECKA" not in u) and ("ROHLIK" not in u) and ("ROHLÍK" not in u):
        return []

    lines = [ln.strip().replace("\xa0", " ") for ln in raw.splitlines()]
    # najdi hlavičku tabulky
    start = None
    for i, ln in enumerate(lines):
        if ln.lower() in {"položka", "polozka"}:
            start = i
            break
    if start is None:
        return []

    i = start + 1
    headers = {
        "množství", "mnozstvi",
        "cena za jed. vč. dph", "cena za jed. vc. dph",
        "sazba dph",
        "cena bez dph",
        "dph",
        "cena vč. dph", "cena vc. dph",
    }
    while i < len(lines) and ((lines[i].lower() in headers) or (lines[i] == "")):
        i += 1

    items: List[dict] = []
    unit_re = re.compile(r"^(ks|kus|kg|g|l|ml)$", re.IGNORECASE)

    while i < len(lines):
        if _STOP_ITEMS_RE.search(lines[i]):
            break

        # 1) název (může být multi-line)
        name_parts: List[str] = []
        while i < len(lines) and lines[i] and (not _NUM_ONLY_RE.fullmatch(lines[i])) and (not _STOP_ITEMS_RE.search(lines[i])):
            name_parts.append(lines[i])
            i += 1

        if i >= len(lines) or _STOP_ITEMS_RE.search(lines[i]):
            break
        if not _NUM_ONLY_RE.fullmatch(lines[i]):
            i += 1
            continue

        # 2) qty
        qty = _parse_number(lines[i])
        i += 1
        if qty is None:
            continue

        # 3) jednotka (volitelně)
        if i < len(lines) and unit_re.match(lines[i] or ""):
            i += 1

        # 4) cena/jed vč DPH (musí být s Kč, aby to nebyla rekapitulace)
        if i >= len(lines) or (not _looks_kc_amount_line(lines[i])):
            break
        unit_price = _norm_amount(_strip_currency(lines[i]))
        i += 1

        # 5) DPH %
        if i >= len(lines):
            break
        vat = _parse_vat_only(lines[i])
        if vat is None:
            break
        i += 1

        # 6-8) bez DPH, DPH, vč DPH (vše s Kč)
        if i + 2 >= len(lines):
            break
        if (not _looks_kc_amount_line(lines[i])) or (not _looks_kc_amount_line(lines[i + 1])) or (not _looks_kc_amount_line(lines[i + 2])):
            break
        # net = _norm_amount(_strip_currency(lines[i]))  # aktuálně nepotřebujeme
        # vat_amt = _norm_amount(_strip_currency(lines[i + 1]))
        gross = _norm_amount(_strip_currency(lines[i + 2]))
        i += 3

        name = " ".join(name_parts).strip()
        if not name:
            continue
        items.append(
            {
                "name": name,
                "quantity": float(qty),
                "unit_price": float(unit_price),
                "vat_rate": float(vat),
                "line_total": float(gross),
            }
        )

    return items


def _parse_items_money_s3_vertical(text: str) -> List[dict]:
    """
    Money S3 faktura: pypdf často vrací sloupce jako samostatné řádky:
      <qty> (např. 1 000,00)
      <název...>
      <vat> (např. 21)
      <cena za m.j.>
      <celkem> (gross)
      <základ> (net)
      <DPH>
      %   (nebo prázdné/separátor)
    """
    raw = text or ""
    lines = [ln.strip().replace("\xa0", " ") for ln in raw.splitlines()]
    if not any(("Označení dodávky" in ln) or ("Označení dodavky" in ln) for ln in lines):
        return []

    # locate header
    start = None
    for idx, ln in enumerate(lines):
        lnl = ln.lower()
        if lnl.startswith("označení dodávky") or lnl.startswith("oznaceni dodavky"):
            start = idx
            break
    if start is None:
        return []

    qty_re = re.compile(r"^-?\d[\d\s]*[.,]\d{2}$")
    i = start + 1
    while i < len(lines) and (not qty_re.fullmatch(lines[i] or "")):
        i += 1

    items: List[dict] = []
    while i < len(lines):
        if _STOP_ITEMS_RE.search(lines[i]):
            break
        if not qty_re.fullmatch(lines[i] or ""):
            i += 1
            continue

        qty = _norm_amount(lines[i])
        i += 1
        if i >= len(lines):
            break

        # name until vat-only line
        name_parts: List[str] = []
        while i < len(lines) and lines[i] and (_parse_vat_only(lines[i]) is None) and (not qty_re.fullmatch(lines[i] or "")) and (not _STOP_ITEMS_RE.search(lines[i])):
            name_parts.append(lines[i])
            i += 1
        if i >= len(lines) or _STOP_ITEMS_RE.search(lines[i]):
            break

        vat = _parse_vat_only(lines[i]) or 0.0
        i += 1
        if i >= len(lines) or (not _looks_amount_line(lines[i])):
            continue
        unit_price = _norm_amount(_strip_currency(lines[i]))
        i += 1
        if i >= len(lines) or (not _looks_amount_line(lines[i])):
            continue
        gross = _norm_amount(_strip_currency(lines[i]))
        i += 1

        # skip net + vat amount if present
        if i < len(lines) and _looks_amount_line(lines[i]):
            i += 1
        if i < len(lines) and _looks_amount_line(lines[i]):
            i += 1
        # skip percent markers/separators
        if i < len(lines) and (lines[i].strip() == "%" or lines[i].strip().endswith("%")):
            i += 1

        name = " ".join(name_parts).strip() or "Položka"
        items.append(
            {
                "name": name,
                "quantity": float(qty),
                "unit_price": float(unit_price),
                "vat_rate": float(vat),
                "line_total": float(gross),
            }
        )
    return items


def _parse_items_ks_line_based(text: str) -> List[dict]:
    """
    Řádky typu SIKO: "... 1,000 KS  1 590,00  1 314,05 21  1 590,00  1 590,00"
    Fallback parser: vezme qty+KS, poslední částku jako line_total, první částku po KS jako unit_price,
    a první rozumnou sazbu DPH (0/10/12/15/21) v okolí.
    """
    items: List[dict] = []
    if not text:
        return items
    vat_candidates = {"0", "10", "12", "15", "21"}
    for ln in (text or "").splitlines():
        s = (ln or "").replace("\xa0", " ").strip()
        if not s or len(s) < 10:
            continue
        if "KS" not in s.upper():
            continue
        if re.search(r"\b(CELKEM|DPH|ZÁKLAD|ZAKLAD|SOUBĚH|SOUCET|SOUČET)\b", s, re.IGNORECASE):
            continue

        # qty + KS
        m = re.search(r"(?P<qty>-?\d+(?:[.,]\d+)?)\s*KS\b", s, re.IGNORECASE)
        if not m:
            continue
        qty = _parse_number(m.group("qty")) or 0.0
        if qty == 0.0:
            continue

        # find all amounts in line
        amts = [a for a in _amount_re.findall(s)]
        if len(amts) < 2:
            continue
        try:
            line_total = _norm_amount(amts[-1])
            unit_price = _norm_amount(amts[0])
        except Exception:
            continue

        # VAT: first small integer token in line
        vat = 0.0
        toks = re.findall(r"\b\d{1,2}\b", s)
        for t in toks:
            if t in vat_candidates:
                vat = float(t)
                break

        # name: everything before qty match
        name = s[: m.start()].strip(" -:\t")
        if not name or len(re.findall(r"[A-Za-zÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]", name)) < 2:
            continue
        items.append({"name": name, "quantity": float(qty), "unit_price": float(unit_price), "vat_rate": float(vat), "line_total": float(line_total)})
    return items


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
        re.compile(r"Faktura\s+č[ií]slo\s*[: ]\s*([\w-]+)", re.IGNORECASE),
        re.compile(r"Faktura\s*#\s*(\d+)", re.IGNORECASE),
        re.compile(r"Č[ií]slo\s+objednávky\s*[: ]\s*([\w-]+)", re.IGNORECASE),
        # Daňové doklady často mají číslo hned pod nadpisem bez "č."
        re.compile(r"DAŇOVÝ\s+DOKLAD\s*(?:č\.?\s*)?\n?\s*([A-Z0-9][A-Z0-9-]{2,})\b", re.IGNORECASE),
        re.compile(r"Faktura\s*-?\s*daňový\s+doklad\s+č\.?\s*([\w-]+)", re.IGNORECASE),
        # Účtenky
        re.compile(r"Ú?čtenka\s+č[ií]slo\s*[: ]\s*(\d{3,})\b", re.IGNORECASE),
        re.compile(r"Doklad\s+č[ií]slo\s*[: ]\s*(\d{3,})\b", re.IGNORECASE),
        # Variabilní symbol (u faktur často funguje jako stabilní identifikátor)
        re.compile(r"\bVS\s*[: ]\s*(\d{3,})\b", re.IGNORECASE),
        re.compile(r"\bV\.?\s*S\.?\s*[: ]\s*(\d{3,})\b", re.IGNORECASE),   # V.S.
        re.compile(r"\bV\s+S\s*[: ]\s*(\d{3,})\b", re.IGNORECASE),         # V S
    ], t)

    bank_account = _find_first([
        re.compile(r"\bIBAN\s*[: ]\s*([A-Z]{2}\d{2}[A-Z0-9]{10,})\b"),
        re.compile(r"\bÚčet\s*[: ]\s*(\d{6,}-?\d{2,}/\d{4})\b", re.IGNORECASE),
        re.compile(r"\b(\d{6,}-?\d{2,})\s*/\s*(\d{4})\b"),
    ], t)
    if bank_account and " " in bank_account:
        bank_account = bank_account.replace(" ", "")

    date_s = _find_first([
        re.compile(r"Datum\s+vystaven[ií]\s*[: ]\s*([0-9]{1,2}\.\s*[0-9]{1,2}\.\s*[0-9]{2,4})", re.IGNORECASE),
        re.compile(r"Datum\s*[: ]\s*([0-9]{1,2}\.\s*[0-9]{1,2}\.\s*[0-9]{2,4})", re.IGNORECASE),
        # Účtenky často mají datum bez labelu, někdy i s časem (čas ignorujeme)
        re.compile(r"\b([0-9]{1,2}\.[0-9]{1,2}\.[0-9]{2,4})\b"),
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
        # Účtenky: "Celkem 68,20" / "PRODEJ 68,20 Kč"
        re.compile(r"\bCelkem\s*[: ]\s*([0-9\s]+[.,][0-9]{2})\b", re.IGNORECASE),
        re.compile(r"Celkem\s+v\s+\w+.*?([0-9\s]+[.,][0-9]{2})", re.IGNORECASE),
        re.compile(r"\bPRODEJ\s*([0-9\s]+[.,][0-9]{2})\b", re.IGNORECASE),
        # tolerantnější "Celkem k úhradě" bez dvojtečky, s textem mezi
        re.compile(r"\bCelkem\s+k\s+úhradě\b[^\d\-]{0,40}([0-9][0-9\s]*[.,][0-9]{2})\b", re.IGNORECASE),
        re.compile(r"\bCelkem\s+k\s+uhradě\b[^\d\-]{0,40}([0-9][0-9\s]*[.,][0-9]{2})\b", re.IGNORECASE),
    ], t)

    total = None
    if total_s:
        try:
            total = _norm_amount(total_s)
        except Exception:
            total = None

    items: List[dict] = []
    # 1) Special-case: vertical table extraction (pypdf) for Rohlik / Money S3
    #    This fixes the common failure mode where each table cell is on its own line.
    items = _parse_items_rohlik_vertical(t)
    if not items:
        items = _parse_items_money_s3_vertical(t)

    # 2) Fallback: SIKO-like "KS" single-line items
    if not items:
        items = _parse_items_ks_line_based(t)

    
    # Wolt faktury: řádky typu "<název> 12% 2 214,90 429,80" (bez "Kč" u čísel)
    if not items:
        wolt_pat = re.compile(
            r"^(?P<name>.+?)\s+(?P<vat>\d{1,2})%\s+(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<unit>\d+[\s\d]*[.,]\d{2})\s+(?P<total>-?\d+[\s\d]*[.,]\d{2})\s*$"
        )
        for ln in t.splitlines():
            ln = ln.strip()
            if not ln or len(ln) < 6:
                continue
            m = wolt_pat.match(ln)
            if not m:
                continue
            try:
                name = m.group("name").strip()
                qty = _safe_float(m.group("qty"))
                unit_price = _norm_amount(m.group("unit"))
                vat = float(m.group("vat"))
                line_total = _norm_amount(m.group("total"))
                items.append({"name": name, "quantity": qty, "unit_price": unit_price, "vat_rate": vat, "line_total": line_total})
            except Exception:
                continue

    # Better-hotel / Mevris: popis položky je často na 1-2 řádcích a ceny jsou ve formátu "... 294.14 CZK 1 294.14 CZK 355.91 CZK"
    if not items:
        # default VAT: vezmeme první explicitní sazbu (např. "21%") z dokumentu
        vat_default = 0.0
        mvat = re.search(r"\b(\d{1,2})%\b", t)
        if mvat:
            try:
                vat_default = float(mvat.group(1))
            except Exception:
                vat_default = 0.0
        bh_pat = re.compile(
            r"(?P<net>\d+[\s\d]*[.,]\d{2})\s*CZK\s+(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<net_total>\d+[\s\d]*[.,]\d{2})\s*CZK\s+(?P<gross>\d+[\s\d]*[.,]\d{2})\s*CZK",
            re.IGNORECASE,
        )
        pending_desc: List[str] = []
        for ln in t.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            m = bh_pat.search(ln)
            if not m:
                # bereme jen "popis" řádky, ignorujeme hlavičky
                if not re.search(r"^(POLOŽKA|CENA|POČET|CELKEM|DPH|DODAVATEL|ODBĚRATEL)\b", ln, re.IGNORECASE):
                    pending_desc.append(ln)
                continue
            try:
                name = " ".join(pending_desc).strip() or "Položka"
                pending_desc = []
                qty = _safe_float(m.group("qty"))
                unit_net = _norm_amount(m.group("net"))
                gross = _norm_amount(m.group("gross"))
                items.append({"name": name, "quantity": qty, "unit_price": unit_net, "vat_rate": float(vat_default or 0.0), "line_total": gross})
            except Exception:
                pending_desc = []
                continue
# receipts (Albert): lines like "2 x 5,60 Kč 11,20"
    if not items:
        rec_pat = re.compile(r"^(?P<name>[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ0-9 .,'/-]{3,})\s*$")
        qty_price_pat = re.compile(
            r"^(?P<qty>\d+(?:[.,]\d+)?)\s*[xX]\s*(?P<unit>\d+[\s\d]*[.,]\d{2}).*?(?P<total>\d+[\s\d]*[.,]\d{2})\s*(?P<vat_letter>[A-Z])?\s*$"
        )
        # single-line: "Název 1 x 12,90 12,90" / "Název 2ks 19,00 38,00"
        single_line_re = re.compile(
            r"^\s*(?P<name>[^0-9]{3,}?)\s+"
            r"(?P<qty>\d+(?:[.,]\d+)?)\s*(?:x|ks|KUS|PCS|pc|×)?\s*"
            r"(?P<unit>\d[\d\s]*[.,]\d{2})\s+"
            r"(?P<total>\d[\d\s]*[.,]\d{2})\s*$",
            re.IGNORECASE,
        )
        pending_name: Optional[str] = None
        for ln in t.splitlines():
            ln = ln.strip()
            if not ln:
                continue

            msl = single_line_re.match(ln)
            if msl:
                name = (msl.group("name") or "").strip(" -:").strip()
                qty = _parse_number(msl.group("qty"))
                unit = _parse_number(msl.group("unit"))
                line_total = _parse_number(msl.group("total"))
                if name and qty and unit and line_total:
                    items.append({"name": name, "quantity": qty, "unit_price": unit, "vat_rate": 0.0, "line_total": line_total})
                    pending_name = None
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

    # zaokrouhlení (pokud existuje) přidáme jako samostatnou položku
    items.extend(_extract_rounding_items(t))

    # --- kontrola součtu položek vs. celkem ---
    # Cíl: co nejvíc dokladů vyřešit offline, a na OpenAI posílat jen minimum.
    reasons: List[str] = []
    sum_ok = False
    try:
        sum_ok = _canonicalize_items_to_unit_net_and_line_gross(items, total, reasons)
        if items and (total is not None) and not sum_ok:
            reasons.append("nesedí součet položek vs. celkem")
    except Exception:
        if items and total is not None:
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

    # Pokud máme explicitní problém se součtem, je to vždy NEROZPOZNANÉ (neprojde do OUT).
    # „nízká jistota vytěžení“ ale přidáváme jen při nízké confidence, ne při součtových problémech.
    requires_review = False
    if conf < 0.75:
        requires_review = True
        reasons.append("nízká jistota vytěžení")
    if items and (total is not None) and (not sum_ok):
        requires_review = True

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


def postprocess_items_for_db(
    *,
    items: List[dict],
    total_with_vat: Optional[float],
    reasons: Optional[List[str]] = None,
) -> Tuple[bool, List[str]]:
    """
    Normalizuje položky do kanonického formátu používaného v DB a provede kontrolu součtu.

    Použití:
      - po offline extrakci je voláno implicitně v extract_from_text()
      - po OpenAI fallbacku je potřeba volat znovu, protože OpenAI vrací položky v různém základu

    Vrací (sum_ok, reasons).
    """
    rr: List[str] = list(reasons or [])
    ok = False
    try:
        ok = _canonicalize_items_to_unit_net_and_line_gross(items, total_with_vat, rr)
        if items and (total_with_vat is not None) and not ok:
            rr.append("nesedí součet položek vs. celkem")
    except Exception:
        if items and (total_with_vat is not None):
            rr.append("nelze ověřit součet položek")
    # de-dup reasons (stabilní pořadí)
    rr = list(dict.fromkeys(rr))
    return ok, rr
