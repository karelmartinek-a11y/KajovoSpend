from __future__ import annotations

import re
import datetime as dt
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict, Iterable, Sequence

from dateutil import parser as dtparser

from kajovospend.extract.vat_math import compute_document_totals, compute_item_derivations
from kajovospend.utils.amount_correction import (
    parse_amount_candidates,
    validate_candidates_against_invariant,
    choose_best_candidate,
    normalize_ocr_amount_token,
)


@dataclass
class Extracted:
    supplier_ico: Optional[str]
    doc_number: Optional[str]
    bank_account: Optional[str]
    issue_date: Optional[dt.date]
    total_with_vat: Optional[float]
    total_without_vat: Optional[float]
    total_vat_amount: Optional[float]
    vat_breakdown_json: Optional[str]
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
    r"^(Základ|Zaklad|Cena celkem|Celkem|Rekapitulace|Součet|Soucet|Zbývá|Zbyva|Celkem k úhradě|CELKEM)\b",
    re.IGNORECASE,
)

def _lines(text: str) -> List[str]:
    return [ln.replace("\xa0", " ").rstrip("\r") for ln in (text or "").splitlines()]


def _find_value_after_label_lines(
    text: str,
    labels: Sequence[str],
    value_re: re.Pattern,
    *,
    max_lookahead_lines: int = 2,
    section_hint_re: re.Pattern | None = None,
) -> Optional[str]:
    """
    Robustní extrakce hodnoty, která bývá:
      - na stejném řádku za label (IČO: 12345678)
      - na dalším řádku (IČO:\n12345678)
      - nebo label je nalepený na číslo / číslo je nalepené na label (26065801IČ:)

    Pokud je section_hint_re zadán, prohledá nejdřív danou sekci (např. Dodavatel ... Odběratel),
    a teprve pak celý text.
    """
    raw = text or ""
    blocks: List[str] = []
    if section_hint_re is not None:
        m = section_hint_re.search(raw)
        if m:
            blocks.append(raw[m.start():m.end()])
    blocks.append(raw)

    for block in blocks:
        ls = _lines(block)
        for i, ln in enumerate(ls):
            low = ln.lower()
            if not any(lbl.lower() in low for lbl in labels):
                continue
            # 1) stejný řádek: zkus najít hodnotu přímo v řádku
            m = value_re.search(ln)
            if m:
                return m.group(1).strip()
            # 2) zbytek řádku za label
            for lbl in labels:
                j = low.find(lbl.lower())
                if j != -1:
                    tail = ln[j + len(lbl):]
                    m2 = value_re.search(tail)
                    if m2:
                        return m2.group(1).strip()
            # 3) další řádky
            for k in range(1, max_lookahead_lines + 1):
                if i + k >= len(ls):
                    break
                nxt = ls[i + k].strip()
                if not nxt:
                    continue
                m3 = value_re.search(nxt)
                if m3:
                    return m3.group(1).strip()
    return None


def _extract_supplier_ico(text: str) -> Optional[str]:
    """
    Robustní IČO dodavatele:
    - preferuj sekci "Dodavatel ... Odběratel" (pomáhá u faktur, kde je IČO dodavatele i odběratele)
    - umí label na dalším řádku i "nalepené" vzory (12345678IČ:)
    """
    t = text or ""
    section_re = re.compile(r"(?is)Dodavatel.*?(?:Odběratel|ODBĚRATEL|Odběratel:|ODBĚRATEL:)", re.IGNORECASE)

    # 1) explicitní label (same-line / next-line)
    raw = _find_value_after_label_lines(
        t,
        labels=("IČO", "ICO", "IČ"),
        value_re=re.compile(r"(?i)(?:IČO|ICO|IČ)\s*[:#]?\s*(\d{8})\b"),
        max_lookahead_lines=2,
        section_hint_re=section_re,
    )
    if raw:
        return _normalize_ico_soft(raw)

    # 2) nalepené "12345678IČ:" / "12345678IČO:" (typicky SIKO PDF)
    m = re.search(r"(?i)\b(\d{8})\s*(?:IČO|ICO|IČ)\s*[:#]\s*", t)
    if m:
        return _normalize_ico_soft(m.group(1))

    # 3) fallback: první 8místné číslo v sekci Dodavatel (už bez labelu)
    raw2 = _find_value_after_label_lines(
        t,
        labels=("Dodavatel",),
        value_re=re.compile(r"(\d{8})"),
        max_lookahead_lines=6,
        section_hint_re=section_re,
    )
    return _normalize_ico_soft(raw2) if raw2 else None


def _extract_doc_number(text: str) -> Optional[str]:
    t = text or ""
    doc_no = _find_first([
        # explicitní daňový doklad / faktura
        re.compile(r"Variabiln[ií]\s+symbol\s*[: ]\s*(\d{3,})\b", re.IGNORECASE),
        re.compile(r"Č[ií]slo\s+faktury[^:\n]{0,40}[: ]\s*(\d{3,})\b", re.IGNORECASE),
        re.compile(r"DAŇOVÝ\s+DOKLAD\s+č\.?\s*([A-Z0-9][A-Z0-9/-]{2,})\b", re.IGNORECASE),
        re.compile(r"DAŇOVÝ\s+DOKLAD\s*[-–]\s*(\d{4,})\b", re.IGNORECASE),
        re.compile(r"Č[ií]slo\s+faktury\s*[: ]\s*([A-Z0-9][\w/-]{2,})", re.IGNORECASE),
        re.compile(r"Faktura\s+č[ií]slo\s*[: ]\s*([A-Z0-9][\w/-]{2,})", re.IGNORECASE),
        re.compile(r"Faktura\s*-?\s*daňový\s+doklad\s+č\.?\s*([\w/-]+)", re.IGNORECASE),
        re.compile(r"Faktura\s*#\s*(\d{6,})\b", re.IGNORECASE),


        # Money S3: "variabilní:\n24202896"
        re.compile(r"\bvariabiln[ií]\s*:\s*\n?\s*(\d{3,})\b", re.IGNORECASE),

        # účtenky
        re.compile(r"Ú?čtenka\s+č[ií]slo\s*[: ]\s*(\d{3,})\b", re.IGNORECASE),
        re.compile(r"Doklad\s+č[ií]slo\s*[: ]\s*(\d{3,})\b", re.IGNORECASE),

        # VS
        re.compile(r"\bVS\s*[: ]\s*(\d{3,})\b", re.IGNORECASE),
        re.compile(r"\bV\.?\s*S\.?\s*[: ]\s*(\d{3,})\b", re.IGNORECASE),
        re.compile(r"\bV\s+S\s*[: ]\s*(\d{3,})\b", re.IGNORECASE),

        # SIKO: "2011001146č.Daňový doklad - FAKTURA"
        re.compile(r"\b(\d{6,})\s*č\.?\s*Daňov", re.IGNORECASE),
        re.compile(r"\b(\d{6,})č\.\s*Daňov", re.IGNORECASE),
    ], t)
    if doc_no:
        return doc_no.strip()

    # DZV-996/2024 apod. – často v horní části
    top = "\n".join(_lines(t)[:40])
    m = re.search(r"\b([A-Z]{1,6}-\d{2,}(?:/\d{2,4})?)\b", top)
    if m:
        return m.group(1).strip()

    # fallback: samostatné číslo 6-12 znaků v horní části
    for ln in _lines(top):
        s = (ln or "").strip()
        if re.fullmatch(r"\d{6,12}", s):
            return s
    return None


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
    _normalize_items(items, reasons, total_with_vat=total_with_vat)

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

def _normalize_items(items: List[dict], reasons: List[str], *, total_with_vat: Optional[float] = None) -> None:
    """
    Sjednotí položky do deterministické podoby pro výpočty:
    - když chybí line_total a máme qty+unit_price -> dopočítá
    - když line_total zjevně obsahuje jednotkovou cenu (qty>1 a line_total≈unit_price) -> opraví na qty*unit_price
    """
    for idx, it in enumerate(items):
        q = _f(it.get("quantity"), 1.0)
        up = it.get("unit_price")
        upf = None if up is None else _f(up, 0.0)
        lt_raw = it.get("line_total")
        lt = _f(lt_raw, 0.0)

        # OCR post-korekce tokenů částek (PULS-006)
        if isinstance(up, str) and up.strip() and upf == 0.0:
            up_cands = parse_amount_candidates(up)
            if up_cands:
                best_up = choose_best_candidate(up_cands, original_guess=upf)
                if best_up is not None:
                    it["unit_price"] = round(float(best_up), 4)
                    upf = float(best_up)
                    fixed, _changed = normalize_ocr_amount_token(up)
                    reasons.append(f"oprava částky položky unit_price: '{up}' -> '{fixed}'")

        if isinstance(lt_raw, str) and lt_raw.strip() and lt == 0.0:
            lt_cands = parse_amount_candidates(lt_raw)
            if lt_cands:
                # invariant: kandidát by měl zlepšit shodu na total_with_vat (pokud total známe)
                if total_with_vat is not None and float(total_with_vat or 0.0) > 0.0:
                    others = 0.0
                    for j, other in enumerate(items):
                        if j == idx:
                            continue
                        others += _f(other.get("line_total"), 0.0)
                    current_diff = abs((others + lt) - float(total_with_vat))
                    valid = validate_candidates_against_invariant(
                        lt_cands,
                        validator=lambda c: abs((others + float(c)) - float(total_with_vat)) <= current_diff + 1e-9,
                    )
                    if valid:
                        lt_cands = valid
                best_lt = choose_best_candidate(lt_cands, original_guess=lt)
                if best_lt is not None:
                    it["line_total"] = round(float(best_lt), 2)
                    lt = float(best_lt)
                    fixed, _changed = normalize_ocr_amount_token(lt_raw)
                    reasons.append(f"oprava částky položky line_total: '{lt_raw}' -> '{fixed}'")

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






def _parse_items_deus_inferis(text: str) -> List[dict]:
    """
    Deus Inferis (neplátce DPH): v PDF je jednoduchá tabulka:
      MNOŽSTVÍ JEDNOTKOVÁ CENA ČÁSTKA
      1 48 044,00 Kč 48 044,00 Kč
    a popis je v sekci "POPIS".
    """
    raw = text or ""
    if "DEUS INFERIS" not in raw.upper():
        return []
    lines = [ln.replace("\xa0", " ").strip() for ln in raw.splitlines()]
    # popis (po "POPIS")
    desc = ""
    for i, ln in enumerate(lines):
        if ln.strip().upper() == "POPIS":
            # zbytek řádku/řádky po POPIS
            tail = " ".join([l.strip() for l in lines[i + 1 : i + 4] if l.strip()])
            desc = tail.strip()
            break

    row_re = re.compile(
        r"^(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<unit>\d+[\s\d]*[.,]\d{2})\s*Kč\s+(?P<total>\d+[\s\d]*[.,]\d{2})\s*Kč\s*$",
        re.IGNORECASE,
    )
    for ln in lines:
        m = row_re.match(ln)
        if not m:
            continue
        try:
            qty = _safe_float(m.group("qty"))
            unit_price = _norm_amount(m.group("unit"))
            line_total = _norm_amount(m.group("total"))
            name = desc or "Služba"
            return [{"name": name, "quantity": float(qty), "unit_price": float(unit_price), "vat_rate": 0.0, "line_total": float(line_total)}]
        except Exception:
            continue
    return []


def _parse_items_dobes(text: str) -> List[dict]:
    """
    Data-Design-Dobeš faktura: sekce "pro DPH 21 %", kde jsou často numeric řádky oddělené od popisu.
    Heuristika:
      - sbírá poslední "textový" popis
      - numeric řádek: <qty> <unit> <total>
      - ošetří chybu typu "177900 1779,00" (chybí desetinná čárka): 177900 -> 1779,00 pokud sedí.
    """
    raw = text or ""
    if "DATA-DESIGN-DOBEŠ".upper() not in raw.upper() and "DATA-DESIGN-DOBEŠ".lower() not in raw.lower() and "Data-Design-Dobeš" not in raw:
        # fallback: podle IBAN/email domény
        if "ddatadesign" not in raw.lower():
            return []
    lines = [ln.replace("\xa0", " ").strip() for ln in raw.splitlines()]
    # najdi start sekce
    start = None
    for i, ln in enumerate(lines):
        if re.search(r"pro\s+DPH\s*21\s*%\s*:", ln, re.IGNORECASE) or re.fullmatch(r"pro\s+DPH\s*21\s*%\s*:", ln.strip(), re.IGNORECASE):
            start = i
            break
        if re.fullmatch(r"pro\s+DPH\s*21\s*%\s*", ln.strip(), re.IGNORECASE):
            start = i
            break
    if start is None:
        # někdy bez dvojtečky
        for i, ln in enumerate(lines):
            if re.search(r"pro\s+DPH\s*21\s*%", ln, re.IGNORECASE):
                start = i
                break
    if start is None:
        return []

    # stop at summary
    stop_idx = len(lines)
    for j in range(start, len(lines)):
        if re.search(r"celkem\s+k\s+úhradě|celkem\s+\[Kč\]|celkem\s*:", lines[j], re.IGNORECASE):
            stop_idx = j
            break

    num_re = re.compile(r"-?\d+[\d\s]*[.,]?\d*")
    amt_re = re.compile(r"-?\d+[\d\s]*[.,]\d{2}")
    pending_desc: List[str] = []
    items: List[dict] = []

    def _fix_amount_token(tok: str, next_tok: str | None) -> Optional[float]:
        """
        tok může být "177900" (bez desetinné čárky). Pokud next_tok je "1779,00", opravíme.
        """
        t = (tok or "").replace(" ", "")
        if amt_re.fullmatch(t):
            return _norm_amount(t)
        if t.isdigit() and len(t) >= 4:
            # zkus /100
            try:
                v = float(int(t)) / 100.0
            except Exception:
                return None
            if next_tok:
                nt = (next_tok or "").replace(" ", "")
                if amt_re.fullmatch(nt):
                    try:
                        vn = _norm_amount(nt)
                        if abs(v - vn) < 0.01:
                            return vn
                    except Exception:
                        pass
            return float(v)
        return None

    for ln in lines[start:stop_idx]:
        if not ln:
            continue
        # text line: má písmena a málo čísel
        if re.search(r"[A-Za-zÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]", ln) and len(amt_re.findall(ln)) == 0 and not re.search(r"^\d+\s+\d", ln):
            # vynech hlavičky
            if re.search(r"počet|text|cena/jedn|celkem", ln, re.IGNORECASE):
                continue
            pending_desc.append(ln)
            if len(pending_desc) > 3:
                pending_desc = pending_desc[-3:]
            continue

        # numeric-ish line
        toks = [t.strip() for t in re.split(r"\s+", ln) if t.strip()]
        # pick numeric tokens that are amounts/ints
        nums = [t for t in toks if re.fullmatch(r"-?\d+[\d\s]*[.,]?\d*", t)]
        if len(nums) < 3:
            continue
        qty_tok = nums[0]
        unit_tok = nums[1]
        total_tok = nums[2]
        try:
            qty = _parse_number(qty_tok) or 0.0
            unit = _fix_amount_token(unit_tok, total_tok)
            total = _fix_amount_token(total_tok, None)
            if qty <= 0 or unit is None or total is None:
                continue
            name = " ".join(pending_desc).strip() or ln
            items.append({"name": name, "quantity": float(qty), "unit_price": float(unit), "vat_rate": 21.0, "line_total": float(total)})
            pending_desc = []
        except Exception:
            continue

    return items

def _parse_items_wolt(text: str) -> List[dict]:
    """
    Wolt účtenky/faktury (Wolt Market apod.)

    Typicky:
      <název...> 12% 3  15,90  47,70
    - názvy mohou být přes více řádků
    - někdy chybí mezera před "12%" (např. "50 g12% ...")

    Pozn.: Řádky "Sleva ..." (Wolt+ / order-level) se v praxi nevztahují k tabulce položek, a
    u řady dokladů by rozbíjely součet vůči "Celkem v CZK". Proto je zde ignorujeme.
    """
    raw = text or ""
    if "WOLT" not in raw.upper():
        return []

    lines = [ln.replace("\xa0", " ").strip() for ln in raw.splitlines()]
    items: List[dict] = []

    item_re = re.compile(
        r"^(?P<name>.+?)\s*(?P<vat>\d{1,2})%\s*(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<unit>\d+[\s\d]*[.,]\d{2})\s+(?P<total>-?\d+[\s\d]*[.,]\d{2})\s*$"
    )

    meta_re = re.compile(
        r"^(Detaily|Zákazník|Zakaznik|Číslo objednávky|Cislo objednavky|Provozovna|Typ objednávky|Typ objednavky|Čas doručení|Cas doruceni|Způsob platby|Zpusob platby|Apple Pay|Google Pay)\b",
        re.IGNORECASE,
    )
    header_re = re.compile(r"^(Položka\b|Polozka\b|Cena\s+DPH\s+Celkem\b|DPH\s+\d{1,2}%\b|Celkem\b|Detaily\s+prodejce\b)", re.IGNORECASE)

    buf = ""
    for ln in lines:
        if not ln:
            continue

        if ln.strip().lower().startswith("sleva"):
            # intentionally ignore order-level discounts
            buf = ""
            continue

        if meta_re.match(ln) or header_re.match(ln) or ("Jednotková cena" in ln) or ("Jednotkova cena" in ln):
            buf = ""
            continue

        # prefer direct match
        m = item_re.match(ln)
        if m:
            try:
                name = (m.group("name") or "").strip(" -:\t")
                qty = _safe_float(m.group("qty"))
                unit_price = _norm_amount(m.group("unit"))
                vat = float(m.group("vat"))
                line_total = _norm_amount(m.group("total"))
                if name and qty:
                    items.append({"name": name, "quantity": qty, "unit_price": unit_price, "vat_rate": vat, "line_total": line_total})
            except Exception:
                pass
            buf = ""
            continue

        # multi-line name buffering
        buf = (buf + " " + ln).strip() if buf else ln
        if len(buf) > 260:
            buf = buf[-260:]

        m2 = item_re.match(buf)
        if m2:
            try:
                name = (m2.group("name") or "").strip(" -:\t")
                qty = _safe_float(m2.group("qty"))
                unit_price = _norm_amount(m2.group("unit"))
                vat = float(m2.group("vat"))
                line_total = _norm_amount(m2.group("total"))
                if name and qty:
                    items.append({"name": name, "quantity": qty, "unit_price": unit_price, "vat_rate": vat, "line_total": line_total})
            except Exception:
                pass
            buf = ""

    return items


def _parse_items_omv(text: str) -> List[dict]:
    """
    OMV účtenky / daňové doklady (typicky 1-2 palivové položky).
    OCR bývá relativně spolehlivé, ale řádky mohou být rozbité.

    Heuristika:
      - detekuj OMV v textu
      - najdi palivové názvy (Natural/Nafta/Diesel/LPG/MaxxMotion/AdBlue)
      - kolem nich hledej množství (l) + jednotkovou cenu (Kč/l) + částku (Kč)
      - pokud chybí některé části, vrátí prázdné (fallback pak může vytvořit syntetickou položku nebo OpenAI).
    """
    raw = text or ""
    u = raw.upper()
    if "OMV" not in u:
        return []

    lines = [ln.replace("\xa0", " ").strip() for ln in raw.splitlines()]
    # odhad sazby DPH z dokumentu
    vat_rate = 0.0
    for ln in lines:
        m = re.search(r"\bDPH\b.*?\b(\d{1,2})\s*%?", ln, re.IGNORECASE)
        if m:
            try:
                v = float(m.group(1))
                if 0 <= v <= 30:
                    vat_rate = v
                    break
            except Exception:
                pass

    # kandidáti názvů paliva
    fuel_re = re.compile(
        r"\b(NATURAL|BENZIN|BENZÍN|DIESEL|NAFTA|MAXX?MOTION|LPG|ADBLUE|AD\-?BLUE)\b",
        re.IGNORECASE,
    )
    qty_re = re.compile(r"(?P<qty>\d+(?:[.,]\d+)?)\s*(?:l|L)\b")
    unit_re = re.compile(
        r"(?P<unit>\d+(?:[.,]\d+)?)\s*(?:Kc|Kč|CZK)\s*/\s*(?:l|L)\b",
        re.IGNORECASE,
    )
    amt_re = re.compile(r"(?P<amt>\d[\d\s]*[.,]\d{2})\s*(?:Kc|Kč|CZK)\b", re.IGNORECASE)

    def _pick_amounts(s: str) -> List[str]:
        return [a for a in _amount_re.findall(s)]

    items: List[dict] = []
    for i, ln in enumerate(lines):
        if not ln:
            continue
        if not fuel_re.search(ln):
            continue

        # name: celý řádek, očisti drobnosti
        name = re.sub(r"\s{2,}", " ", ln).strip(" -:\t")
        if len(name) < 2:
            continue

        window = " ".join([lines[j] for j in range(i, min(len(lines), i + 4)) if lines[j]])
        qty = None
        unit_price = None
        line_total = None

        mq = qty_re.search(window)
        if mq:
            qty = _parse_number(mq.group("qty"))

        mu = unit_re.search(window)
        if mu:
            unit_price = _parse_number(mu.group("unit"))

        # částky: vezmeme největší částku v okně jako line_total
        amts = _pick_amounts(window)
        if amts:
            try:
                nums = [_norm_amount(a) for a in amts]
                if nums:
                    line_total = max(nums)
            except Exception:
                pass

        # pokud jednotková cena není, a máme qty+total, dopočítej unit_price
        try:
            if (unit_price is None) and qty and line_total and qty > 0:
                unit_price = float(line_total) / float(qty)
        except Exception:
            pass

        if qty and unit_price and (line_total is not None):
            items.append(
                {
                    "name": name,
                    "quantity": float(qty),
                    "unit_price": float(unit_price),
                    "vat_rate": float(vat_rate),
                    "line_total": float(line_total),
                }
            )

    

    # fallback: pokud OCR nevyčetlo název paliva, ale máme řádky s "l" + dvě částky
    if not items:
        pending = None
        for i, ln in enumerate(lines):
            if _STOP_ITEMS_RE.search(ln):
                break
            # kandidát názvu: řádek s písmeny bez částek
            if re.search(r"[A-Za-zÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]", ln) and not _amount_re.search(ln) and len(ln) >= 4:
                pending = ln.strip(" -:\t")
                continue
            if "l" not in ln.lower() or "x" not in ln.lower():
                continue
            amts = _pick_amounts(ln)
            mq = re.search(r"(\d+(?:[.,]\d+)?)\s*[lL]\b", ln)
            mu = re.search(r"[xX×]\s*(\d+(?:[.,]\d+)?)", ln)
            if mq and mu and len(amts) >= 1:
                try:
                    qty = _parse_number(mq.group(1))
                    unit_price = _parse_number(mu.group(1))
                    # total vezmi největší částku na řádku
                    nums = [_norm_amount(a) for a in amts]
                    line_total = max(nums) if nums else None
                    if qty and unit_price and (line_total is not None):
                        items.append({
                            "name": pending or "OMV - palivo",
                            "quantity": float(qty),
                            "unit_price": float(unit_price),
                            "vat_rate": float(vat_rate),
                            "line_total": float(line_total),
                        })
                        pending = None
                except Exception:
                    continue

    # dedupe (někdy OCR zdvojí řádky)
    uniq: List[dict] = []
    seen = set()
    for it in items:
        key = (it.get("name"), round(float(it.get("line_total", 0.0)), 2), round(float(it.get("quantity", 0.0)), 3))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)

    return uniq


def _parse_date(s: str) -> Optional[dt.date]:
    s = (s or "").strip()
    if not s:
        return None

    # české názvy měsíců (leden/ledna, únor/února, ...)
    month_map = {
        "leden": "01", "ledna": "01",
        "únor": "02", "unor": "02", "února": "02", "unora": "02",
        "březen": "03", "brezen": "03", "března": "03", "brezna": "03",
        "duben": "04", "dubna": "04",
        "květen": "05", "kveten": "05", "května": "05", "kvetna": "05",
        "červen": "06", "cerven": "06", "června": "06", "cervna": "06",
        "červenec": "07", "cervenec": "07", "července": "07", "cervence": "07",
        "srpen": "08", "srpna": "08",
        "září": "09", "zari": "09",
        "říjen": "10", "rijen": "10", "října": "10", "rijna": "10",
        "listopad": "11", "listopadu": "11",
        "prosinec": "12", "prosince": "12",
    }
    m = re.search(r"\b(\d{1,2})\.?\s*([A-Za-zÁČĎÉĚÍŇÓŘŠŤÚŮÝŽáčďéěíňóřšťúůýž]+)\s*(\d{4})\b", s)
    if m:
        day = m.group(1).zfill(2)
        mon = month_map.get(m.group(2).strip().lower())
        year = m.group(3)
        if mon:
            s = f"{day}.{mon}.{year}"

    try:
        d = dtparser.parse(s, dayfirst=True).date()
        return d
    except Exception:
        return None




def _parse_items_booking(text: str) -> List[dict]:
    """Booking.com faktury (provize + poplatek za platební služby).
    Typicky embedded text, měna EUR, a v položkách není klasická tabulka.
    """
    raw = text or ""
    if "BOOKING.COM" not in raw.upper():
        return []
    lines = [ln.replace("\xa0"," ").strip() for ln in raw.splitlines() if ln.strip()]
    items: List[dict] = []
    # Rezervace ... Provize: "Rezervace EUR 15 760,91 EUR 3 625,00"
    for ln in lines:
        if ln.lower().startswith("rezervace"):
            amts = _amount_re.findall(ln)
            if amts:
                try:
                    prov = _norm_amount(amts[-1])
                    items.append({
                        "name": "Provize (rezervace)",
                        "quantity": 1.0,
                        "unit_price": float(prov),
                        "vat_rate": 0.0,
                        "line_total": float(prov),
                    })
                except Exception:
                    pass
        if "poplatek" in ln.lower() and "platebn" in ln.lower():
            amts = _amount_re.findall(ln)
            if amts:
                try:
                    fee = _norm_amount(amts[-1])
                    items.append({
                        "name": "Poplatek za platební služby",
                        "quantity": 1.0,
                        "unit_price": float(fee),
                        "vat_rate": 0.0,
                        "line_total": float(fee),
                    })
                except Exception:
                    pass
    # dedupe
    uniq: List[dict] = []
    seen=set()
    for it in items:
        key=(it["name"], round(float(it["line_total"]),2))
        if key in seen: 
            continue
        seen.add(key)
        uniq.append(it)
    return uniq


def _parse_items_organic_restaurant(text: str) -> List[dict]:
    """Faktura Organic Restaurant (POHODA) – 2 řádky položek v tabulce.

    OCR často rozbije tabulku, ale sazba DPH (%) a KČ celkem bývají na stejné řádce.
    """
    raw = text or ""
    if "ORGANIC RESTAUR" not in raw.upper():
        return []
    lines = [ln.replace("\xa0"," ").strip() for ln in raw.splitlines()]
    # najdi sekci tabulky
    start = None
    for i, ln in enumerate(lines):
        if "OZNACENI" in ln.upper() and "DOD" in ln.upper():
            start = i
            break
    if start is None:
        start = 0

    items: List[dict] = []
    pending_name: Optional[str] = None
    for i in range(start, len(lines)):
        ln = lines[i]
        if not ln:
            continue
        if ln.strip().startswith("-"):
            nm = ln.strip().lstrip("-").strip()
            if nm:
                pending_name = nm
            continue
        if _STOP_ITEMS_RE.search(ln):
            break
        # řádek s čísly a % – např. "1 3 392,86 ... 12% ... 3 800,00"
        mvat = re.search(r"\b(\d{1,2})\s*%\b", ln)
        amts = _amount_re.findall(ln)
        qty_m = re.match(r"^(\d+(?:[.,]\d+)?)\b", ln)
        if mvat and len(amts) >= 2 and qty_m and pending_name:
            try:
                qty = _safe_float(qty_m.group(1))
                unit_price = _norm_amount(amts[0])
                line_total = _norm_amount(amts[-1])
                vat = float(mvat.group(1))
                items.append({
                    "name": pending_name,
                    "quantity": float(qty),
                    "unit_price": float(unit_price),
                    "vat_rate": float(vat),
                    "line_total": float(line_total),
                })
                pending_name = None
            except Exception:
                continue
    return items


def _parse_items_albert(text: str) -> List[dict]:
    """Albert účtenky (často sken s 2 účtenkami vedle sebe).

    Cíl: vytěžit položky alespoň z jedné účtenky deterministicky i při rozbitém OCR.
    Podporuje:
      - kusové položky: "6 x 3,90 Kč"
      - vážené položky: "0,875 kg x 29,90 Kč/kg 26,2"
      - řádky kde je název zvlášť a kvantita/cena na dalším řádku
    """
    raw = text or ""
    if "ALBERT" not in raw.lower():
        return []

    lines = [ln.replace("\xa0"," ").strip() for ln in raw.splitlines() if ln.strip()]
    # pomocné regexy
    count_line = re.compile(
        r"^(?P<qty>\d+(?:[.,]\d+)?)\s*[xX×]\s*(?P<unit>\d+[\d\s]*[.,]\d{2})\s*(?:Kc|Kč|CZK)?\s*$",
        re.IGNORECASE,
    )
    weight_line = re.compile(
        r"^(?P<qty>\d+(?:[.,]\d+)?)\s*kg\s*[xX×]\s*(?P<unit>\d+[\d\s]*[.,]\d{2})\s*(?:Kc|Kč|CZK)?\s*/\s*kg\s*(?P<total>\d+[\d\s]*[.,]\d{2})?\s*(?P<vat_letter>[A-Z])?\s*$",
        re.IGNORECASE,
    )
    inline_weight = re.compile(
        r"^(?P<name>.+?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s*kg\s*[xX×]\s*(?P<unit>\d+[\d\s]*[.,]\d{2}).*?(?P<total>\d+[\d\s]*[.,]\d{2})\s*(?P<vat_letter>[A-Z])?\s*$",
        re.IGNORECASE,
    )
    inline_count = re.compile(
        r"^(?P<name>.+?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s*[xX×]\s*(?P<unit>\d+[\d\s]*[.,]\d{2}).*?(?P<total>\d+[\d\s]*[.,]\d{2})\s*(?P<vat_letter>[A-Z])?\s*$",
        re.IGNORECASE,
    )

    def _vat_from_letter(letter: str | None) -> float:
        if not letter:
            return 0.0
        return float(_VAT_LETTER_MAP.get(str(letter).upper(), 0.0))

    items: List[dict] = []
    pending_name: Optional[str] = None

    for ln in lines:
        if _STOP_ITEMS_RE.search(ln):
            break
        # skip obvious headers
        if re.search(r"^(DATUM|DOKLAD|POKLADNA|VRATIT|KORUNA|CELKEM|BODY|AKTIVUJTE)\b", ln, re.IGNORECASE):
            continue

        # inline parsers first
        m = inline_weight.match(ln)
        if m:
            try:
                name = m.group("name").strip(" -:")
                qty = _safe_float(m.group("qty"))
                unit = _norm_amount(m.group("unit"))
                total = _norm_amount(m.group("total"))
                vat = _vat_from_letter(m.group("vat_letter"))
                items.append({"name": name, "quantity": float(qty), "unit_price": float(unit), "vat_rate": float(vat), "line_total": float(total)})
                pending_name = None
                continue
            except Exception:
                pass

        m = inline_count.match(ln)
        if m:
            try:
                name = m.group("name").strip(" -:")
                qty = _safe_float(m.group("qty"))
                unit = _norm_amount(m.group("unit"))
                total = _norm_amount(m.group("total"))
                vat = _vat_from_letter(m.group("vat_letter"))
                items.append({"name": name, "quantity": float(qty), "unit_price": float(unit), "vat_rate": float(vat), "line_total": float(total)})
                pending_name = None
                continue
            except Exception:
                pass

        # name-only line
        if re.fullmatch(r"[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ0-9 .,'/\-]{4,}", ln) and not re.search(r"\d+[\s\d]*[.,]\d{2}", ln):
            pending_name = ln.strip()
            continue

        # weight line without name (use pending_name)
        mw = weight_line.match(ln)
        if mw and pending_name:
            try:
                qty = _safe_float(mw.group("qty"))
                unit = _norm_amount(mw.group("unit"))
                total_s = mw.group("total")
                total = _norm_amount(total_s) if total_s else float(qty) * float(unit)
                vat = _vat_from_letter(mw.group("vat_letter"))
                items.append({"name": pending_name, "quantity": float(qty), "unit_price": float(unit), "vat_rate": float(vat), "line_total": float(total)})
                pending_name = None
                continue
            except Exception:
                pass

        mc = count_line.match(ln)
        if mc and pending_name:
            try:
                qty = _safe_float(mc.group("qty"))
                unit = _norm_amount(mc.group("unit"))
                total = float(qty) * float(unit)
                items.append({"name": pending_name, "quantity": float(qty), "unit_price": float(unit), "vat_rate": 0.0, "line_total": float(total)})
                pending_name = None
                continue
            except Exception:
                pass

    # filtruj nesmysly a dedupe
    uniq: List[dict] = []
    seen=set()
    for it in items:
        nm = str(it.get("name") or "").strip()
        if len(nm) < 2:
            continue
        key=(nm, round(float(it.get("line_total") or 0.0),2), round(float(it.get("quantity") or 0.0),3))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    return uniq



def extract_from_text(text: str) -> Extracted:
    raw = text or ""
    t = raw

    # IČO a číslo dokladu robustněji (same-line/next-line/glued)
    ico = _extract_supplier_ico(t)
    doc_no = _extract_doc_number(t)

    bank_account = _find_first([
        re.compile(r"\bIBAN\s*[: ]\s*([A-Z]{2}\d{2}[A-Z0-9]{10,})\b"),
        re.compile(r"\bÚčet\s*[: ]\s*(\d{6,}-?\d{2,}/\d{4})\b", re.IGNORECASE),
        re.compile(r"\b(\d{6,}-?\d{2,})\s*/\s*(\d{4})\b"),
    ], t)
    if bank_account and " " in bank_account:
        bank_account = bank_account.replace(" ", "")

    date_s = _find_first([
        re.compile(r"Datum\s+vystaven[ií]\s*[: ]\s*([0-9]{1,2}\.\s*[0-9]{1,2}\.\s*[0-9]{2,4})", re.IGNORECASE),
        re.compile(r"Datum\s+vystaven[ií]\s*[: ]\s*(\d{1,2}\.\s*[A-Za-zÁČĎÉĚÍŇÓŘŠŤÚŮÝŽáčďéěíňóřšťúůýž]+\s*\d{4})", re.IGNORECASE),
        re.compile(r"Datum\s*[: ]\s*([0-9]{1,2}\.\s*[0-9]{1,2}\.\s*[0-9]{2,4})", re.IGNORECASE),
        # Účtenky často mají datum bez labelu, někdy i s časem (čas ignorujeme)
        re.compile(r"\b([0-9]{1,2}\.[0-9]{1,2}\.[0-9]{2,4})\b"),
        re.compile(r"\b([0-9]{2}/[0-9]{2}/[0-9]{4})\b"),
    ], t)
    issue_date = _parse_date(date_s) if date_s else None

    # currency
    currency = "EUR" if re.search(r"\bEUR\b", t) else "CZK" if re.search(r"\bCZK\b|Kč", t) else "CZK"

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

    pre_reasons: List[str] = []
    total = None
    if total_s:
        try:
            total = _norm_amount(total_s)
        except Exception:
            total = None
            cands = parse_amount_candidates(total_s)
            if cands:
                best = choose_best_candidate(cands)
                if best is not None:
                    total = float(best)
                    fixed, changed = normalize_ocr_amount_token(total_s)
                    if changed:
                        pre_reasons.append(f"oprava částky total: '{total_s}' -> '{fixed}'")
                    else:
                        pre_reasons.append(f"oprava částky total: '{total_s}' -> '{best:.2f}'")

    items: List[dict] = []
    # 1) Special-case: vertical table extraction (pypdf) for Rohlik / Money S3
    #    This fixes the common failure mode where each table cell is on its own line.
    items = _parse_items_rohlik_vertical(t)
    if not items:
        items = _parse_items_money_s3_vertical(t)

    # 2) Fallback: SIKO-like "KS" single-line items
    if not items:
        items = _parse_items_ks_line_based(t)

    
    # Wolt faktury / účtenky (Wolt Market apod.)
    if (not items) or ("WOLT" in t.upper() and len(items) < 3):
        items = _parse_items_wolt(t)


    # OMV účtenky (palivo) - typicky sken/OCR
    if not items:
        items = _parse_items_omv(t)


    # Deus Inferis (neplátce DPH): 1 řádek tabulky + "POPIS"
    if not items:
        items = _parse_items_deus_inferis(t)

    # Data-Design-Dobeš: numeric řádky oddělené od popisu (často rozbitá desetinná čárka)
    if not items:
        items = _parse_items_dobes(t)


    # Booking.com faktury: provize + poplatek
    if not items:
        items = _parse_items_booking(t)

    # Organic Restaurant (POHODA): tabulka 2 položek
    if not items:
        items = _parse_items_organic_restaurant(t)

    # Albert účtenky (retail)
    if not items:
        items = _parse_items_albert(t)


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
    reasons: List[str] = list(pre_reasons)
    sum_ok = False
    total_without_vat: Optional[float] = None
    total_vat_amount: Optional[float] = None
    vat_breakdown_json: Optional[str] = None
    try:
        sum_ok = _canonicalize_items_to_unit_net_and_line_gross(items, total, reasons)
        if items and (total is not None) and not sum_ok:
            reasons.append("nesedí součet položek vs. celkem")

        # PULS-002: deterministické dopočty net/gross/vat + VAT breakdown.
        derived_items: List[dict] = []
        for it in items:
            d = compute_item_derivations(it)
            derived_items.append(d)
        items = derived_items

        net, vat, gross, breakdown, flags = compute_document_totals(items, total_with_vat=total)
        total_without_vat = net
        total_vat_amount = vat
        try:
            import json
            vat_breakdown_json = json.dumps(breakdown, ensure_ascii=False)
        except Exception:
            vat_breakdown_json = None

        if not bool(flags.get("sum_ok_gross", True)):
            reasons.append("gross nesedí na total_with_vat")
        if not bool(flags.get("sum_ok_net", True)):
            reasons.append("net nesedí na dopočtený základ")
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
        total_without_vat=total_without_vat,
        total_vat_amount=total_vat_amount,
        vat_breakdown_json=vat_breakdown_json,
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
    total_without_vat_hint: Optional[float] = None,
    reasons: Optional[List[str]] = None,
) -> Tuple[bool, List[str], Optional[float], Optional[float], Optional[str]]:
    """
    Normalizuje položky do kanonického formátu používaného v DB a provede kontrolu součtu.

    Použití:
      - po offline extrakci je voláno implicitně v extract_from_text()
      - po OpenAI fallbacku je potřeba volat znovu, protože OpenAI vrací položky v různém základu

    Vrací (sum_ok, reasons, total_without_vat, total_vat_amount, vat_breakdown_json).
    """
    rr: List[str] = list(reasons or [])
    ok = False
    total_without_vat: Optional[float] = None
    total_vat_amount: Optional[float] = None
    vat_breakdown_json: Optional[str] = None
    try:
        ok = _canonicalize_items_to_unit_net_and_line_gross(items, total_with_vat, rr)
        if items and (total_with_vat is not None) and not ok:
            rr.append("nesedí součet položek vs. celkem")

        derived_items: List[dict] = []
        for it in items:
            derived_items.append(compute_item_derivations(it))
        items[:] = derived_items

        net, vat, _gross, breakdown, flags = compute_document_totals(
            items,
            total_with_vat=total_with_vat,
            total_without_vat_hint=total_without_vat_hint,
        )
        total_without_vat = net
        total_vat_amount = vat
        try:
            import json
            vat_breakdown_json = json.dumps(breakdown, ensure_ascii=False)
        except Exception:
            vat_breakdown_json = None

        if not bool(flags.get("sum_ok_gross", True)):
            rr.append("gross nesedí na total_with_vat")
        if not bool(flags.get("sum_ok_net", True)):
            rr.append("net nesedí na total_without_vat")
        ok = bool(flags.get("sum_ok", ok))
    except Exception:
        if items and (total_with_vat is not None):
            rr.append("nelze ověřit součet položek")
    # de-dup reasons (stabilní pořadí)
    rr = list(dict.fromkeys(rr))
    return ok, rr, total_without_vat, total_vat_amount, vat_breakdown_json
