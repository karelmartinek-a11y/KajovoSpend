from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
import re
from typing import Optional

import requests

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10
DEFAULT_CACHE_TTL_SECONDS = 7 * 24 * 3600
MAX_CACHE_SIZE = 5_000

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


def _compose_delivery_address(addr: Optional[dict]) -> Optional[str]:
    if not isinstance(addr, dict):
        return None
    lines = [
        (addr.get("radekAdresy1") or "").strip(),
        (addr.get("radekAdresy2") or "").strip(),
        (addr.get("radekAdresy3") or "").strip(),
    ]
    lines = [l for l in lines if l]
    return ", ".join(lines) if lines else None


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
    if timeout <= 0:
        raise ValueError("timeout musi byt kladne cislo")
    if cache_ttl_seconds < 0:
        raise ValueError("cache_ttl_seconds nesmi byt zaporne")

    ico_norm = normalize_ico(ico)

    now = dt.datetime.utcnow()
    cached = _ARES_CACHE.get(ico_norm)
    if cached:
        fetched_at, rec = cached
        if (now - fetched_at).total_seconds() <= cache_ttl_seconds:
            return rec

    url = f"{_ARES_BASE_URL}/ekonomicke-subjekty/{ico_norm}"
    start = dt.datetime.utcnow()
    try:
        resp = requests.get(
            url,
            timeout=(min(timeout, 5), timeout),
            headers={
                "Accept": "application/json",
                "User-Agent": "KajovoSpend/0.1 (ARES client)",
            },
        )
        resp.raise_for_status()
        obj = resp.json()
    except Exception as e:
        raise AresError(f"Nepodařilo se načíst ARES pro IČO {ico_norm}: {e}") from e
    finally:
        try:
            elapsed = (dt.datetime.utcnow() - start).total_seconds()
            log.debug("ares-fetch", extra={"ico": ico_norm, "seconds": elapsed})
        except Exception:
            pass

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
    if stav_dph is not None:
        sval = str(stav_dph).strip().lower()
        if sval in {"a", "akt", "aktivni", "ano", "true", "1"} or sval.startswith("akt"):
            is_vat_payer = True
        elif sval in {"n", "ne", "neaktivni", "false", "0"} or sval.startswith("neakt"):
            is_vat_payer = False
    # fallback na starší klíče
    if is_vat_payer is None:
        vat = obj.get("platceDph") or obj.get("jePlatceDph") or obj.get("platceDPH")
        if isinstance(vat, bool):
            is_vat_payer = vat
        elif isinstance(vat, str):
            sval = vat.strip().lower()
            if sval in {"true", "1", "ano", "a"}:
                is_vat_payer = True
            elif sval in {"false", "0", "ne", "n"}:
                is_vat_payer = False
    # pokud máme DIČ typu CZ123..., je to silný indikátor plátce DPH
    if is_vat_payer is None and isinstance(dic, str) and dic.strip().upper().startswith("CZ"):
        is_vat_payer = True

    # address
    adr = obj.get("sidlo") or {}
    street = adr.get("nazevUlice") or adr.get("ulice") or None
    street_number = (
        str(adr.get("cisloDomovni") or adr.get("cisloPopisne") or "") or None
    )
    orientation_number = str(adr.get("cisloOrientacni") or "") or None
    orient_letter = (adr.get("cisloOrientacniPismeno") or "").strip()
    if orient_letter:
        orientation_number = (orientation_number or "") + orient_letter
    city = adr.get("nazevObce") or adr.get("obec") or None
    zip_code = str(adr.get("psc") or "") or None

    address = _compose_delivery_address(obj.get("adresaDorucovaci"))
    if not address and isinstance(adr.get("textovaAdresa"), str) and adr.get("textovaAdresa").strip():
        address = adr.get("textovaAdresa").strip()
    if not address:
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
    if len(_ARES_CACHE) > MAX_CACHE_SIZE:
        oldest_key = min(_ARES_CACHE.items(), key=lambda item: item[1][0])[0]
        _ARES_CACHE.pop(oldest_key, None)
    return rec
