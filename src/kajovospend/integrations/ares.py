from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional, Dict, Tuple

import requests


@dataclass
class AresRecord:
    ico: str
    name: Optional[str]
    dic: Optional[str]
    address: Optional[str]
    is_vat_payer: Optional[bool]
    fetched_at: dt.datetime


ARES_URL = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{}"

_CACHE: Dict[str, Tuple[dt.datetime, AresRecord]] = {}
_CACHE_TTL = dt.timedelta(hours=24)


def _is_valid_ico(ico: str) -> bool:
    """
    Czech IČO checksum validation (8 digits).
    """
    if not ico.isdigit() or len(ico) != 8:
        return False
    weights = [8, 7, 6, 5, 4, 3, 2]
    s = sum(int(ico[i]) * weights[i] for i in range(7))
    mod = s % 11
    chk = 11 - mod
    if chk == 10:
        chk = 0
    elif chk == 11:
        chk = 1
    return chk == int(ico[7])


def _format_address(ad: dict) -> Optional[str]:
    """
    Build a readable one-line address from ARES 'sidlo'.
    """
    if not ad:
        return None
    street = ad.get("nazevUlice") or ""
    cd = ad.get("cisloDomovni")
    co = ad.get("cisloOrientacni")
    part = ad.get("nazevCastiObce") or ""
    city = ad.get("nazevObce") or ""
    psc = ad.get("psc")

    house = ""
    if cd:
        house = str(cd)
    if co:
        house = f"{house}/{co}" if house else str(co)

    left = " ".join([p for p in [street, house] if p]).strip()
    mid = ", ".join([p for p in [part, city] if p]).strip(", ")
    right = str(psc) if psc else ""

    parts = [p for p in [left, mid, right] if p]
    return ", ".join(parts) if parts else None


def fetch_by_ico(ico: str, timeout: int = 15) -> AresRecord:
    ico = (ico or "").strip()
    if not ico:
        raise ValueError("IČO je prázdné.")
    if not _is_valid_ico(ico):
        raise ValueError("Neplatné IČO (kontrolní součet).")

    # cache
    now = dt.datetime.utcnow()
    cached = _CACHE.get(ico)
    if cached:
        ts, rec = cached
        if now - ts <= _CACHE_TTL:
            return rec

    headers = {
        "User-Agent": "KajovoSpend/1.0 (+https://kajovo.example.local)",
        "Accept": "application/json",
    }

    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            r = requests.get(ARES_URL.format(ico), timeout=timeout, headers=headers)
            if r.status_code == 404:
                raise LookupError("IČO nebylo v ARES nalezeno.")
            r.raise_for_status()
            j = r.json()
            name = j.get("obchodniJmeno")
            dic = j.get("dic")
            addr = _format_address(j.get("sidlo") or {})
            is_vat = j.get("platceDph") if "platceDph" in j else None
            rec = AresRecord(
                ico=ico,
                name=name,
                dic=dic,
                address=addr,
                is_vat_payer=is_vat,
                fetched_at=now,
            )
            _CACHE[ico] = (now, rec)
            return rec
        except Exception as e:
            last_err = e
            # simple backoff
            try:
                import time
                time.sleep(0.5 * (attempt + 1))
            except Exception:
                pass

    raise ConnectionError(f"ARES request selhal: {last_err}")
