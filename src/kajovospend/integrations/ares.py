from __future__ import annotations

import datetime as dt
import logging
import logging
from dataclasses import dataclass, field
import re
from typing import Optional

import requests

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10
DEFAULT_CACHE_TTL_SECONDS = 7 * 24 * 3600

_ARES_CACHE: dict[str, tuple[dt.datetime, "AresRecord"]] = {}


@dataclass(frozen=True)
class AresRecord:
    ico: str
    name: Optional[str] = None
    dic: Optional[str] = None
    legal_form: Optional[str] = None
    is_vat_payer: Optional[bool] = None
    address: Optional[str] = None
    street: Optional[str] = None
    street_number: Optional[str] = None
    orientation_number: Optional[str] = None
    city: Optional[str] = None
    zip_code: Optional[str] = None
    fetched_at: dt.datetime = field(default_factory=dt.datetime.utcnow)


class AresError(RuntimeError):
    pass


_ARES_BASE_URL = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest"

_ICO_DIGITS_RE = re.compile(r"\D+")


def _compose_address(
    street: Optional[str],
    street_number: Optional[str],
    orientation_number: Optional[str],
    city: Optional[str],
    zip_code: Optional[str],
) -> Optional[str]:
    parts = []
    s = (street or "").strip()
    sn = (street_number or "").strip()
    on = (orientation_number or "").strip()
    first = " ".join([p for p in [s, sn + (f"/{on}" if on else "")] if p]).strip()
    if first:
        parts.append(first)
    if (city or "").strip():
        parts.append(city.strip())
    if (zip_code or "").strip():
        parts.append(zip_code.strip())
    return ", ".join(parts) if parts else None


def normalize_ico(ico: str) -> str:
    """
    Normalizuje IČO do kanonického tvaru:
    - ponechá jen číslice
    - doplní zleva nuly na délku 8
    """
    if ico is None:
        raise ValueError("IČO je prázdné")
    raw = str(ico).strip()
    if not raw:
        raise ValueError("IČO je prázdné")
    digits = _ICO_DIGITS_RE.sub("", raw)
    if not digits:
        raise ValueError(f"IČO neobsahuje číslice: {ico!r}")
    if len(digits) > 8:
        raise ValueError(f"IČO má více než 8 číslic: {ico!r}")
    return digits.zfill(8)


def fetch_by_ico(
    ico: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
) -> AresRecord:
    ico_norm = normalize_ico(ico)

    now = dt.datetime.utcnow()
    cached = _ARES_CACHE.get(ico_norm)
    if cached:
        fetched_at, rec = cached
        if (now - fetched_at).total_seconds() <= cache_ttl_seconds:
            return rec

    url = f"{_ARES_BASE_URL}/ekonomicke-subjekty/{ico_norm}"
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "User-Agent": "KajovoSpend/0.1 (ARES client)",
            },
        )
        resp.raise_for_status()
        obj = resp.json()
    except Exception as e:
        raise AresError(f"Nepodařilo se načíst ARES pro IČO {ico_norm}: {e}") from e

    # name & identifiers
    name = obj.get("obchodniJmeno") or obj.get("nazev")
    dic = obj.get("dic") or obj.get("dicDph")

    # legal form
    legal_form = None
    pf = obj.get("pravniForma")
    if isinstance(pf, dict):
        legal_form = pf.get("text") or pf.get("nazev") or pf.get("kod")
    else:
        legal_form = pf

    # VAT payer (ARES REST: seznamRegistraci.stavZdrojeDph)
    is_vat_payer: Optional[bool] = None
    regs = obj.get("seznamRegistraci") or {}
    stav_dph = regs.get("stavZdrojeDph")
    if stav_dph is None:
        # konzervativní fallback: pokud ARES neposkytne stav, necháme None
        # (nebudeme hádat z DIČ, protože DIČ může existovat i mimo DPH registr)
        is_vat_payer = None
    else:
        sval = str(stav_dph).strip().lower()
        # typicky očekávané hodnoty (různé implementace / ciselníky)
        if sval in {"a", "akt", "aktivni", "ano", "true", "1"} or sval.startswith("akt"):
            is_vat_payer = True
        elif sval in {"n", "ne", "neaktivni", "false", "0"} or sval.startswith("neakt"):
            is_vat_payer = False
        else:
            # neznámý stav -> ponecháme None (raději kontrola uživatelem)
            is_vat_payer = None

    # address
    adr = obj.get("sidlo") or {}
    street = adr.get("nazevUlice") or adr.get("ulice") or None
    street_number = (
        str(adr.get("cisloDomovni") or adr.get("cisloPopisne") or "") or None
    )
    orientation_number = str(adr.get("cisloOrientacni") or "") or None
    city = adr.get("nazevObce") or adr.get("obec") or None
    zip_code = str(adr.get("psc") or "") or None
    address = _compose_address(street, street_number, orientation_number, city, zip_code)

    rec = AresRecord(
        ico=ico_norm,
        name=name,
        dic=dic,
        legal_form=legal_form,
        is_vat_payer=is_vat_payer,
        address=address,
        street=street,
        street_number=street_number,
        orientation_number=orientation_number,
        city=city,
        zip_code=zip_code,
        fetched_at=now,
    )
    _ARES_CACHE[ico_norm] = (now, rec)
    return rec
