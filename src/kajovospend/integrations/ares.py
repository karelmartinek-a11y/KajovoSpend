from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional

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


def fetch_by_ico(ico: str, timeout: int = 15) -> AresRecord:
    ico = ico.strip()
    r = requests.get(ARES_URL.format(ico), timeout=timeout)
    r.raise_for_status()
    j = r.json()
    name = j.get("obchodniJmeno")
    dic = j.get("dic")
    addr = None
    ad = j.get("sidlo") or {}
    # build simple address
    parts = []
    if ad.get("nazevUlice"):
        parts.append(ad.get("nazevUlice"))
    if ad.get("cisloDomovni"):
        parts.append(str(ad.get("cisloDomovni")))
    if ad.get("cisloOrientacni"):
        parts.append(str(ad.get("cisloOrientacni")))
    if ad.get("nazevObce"):
        parts.append(str(ad.get("nazevObce")))
    if ad.get("psc"):
        parts.append(str(ad.get("psc")))
    if parts:
        addr = ", ".join([p for p in parts if p])
    # VAT payer flag is not consistently present; keep None if unknown
    is_vat = j.get("platceDph") if "platceDph" in j else None
    return AresRecord(
        ico=ico,
        name=name,
        dic=dic,
        address=addr,
        is_vat_payer=is_vat,
        fetched_at=dt.datetime.utcnow(),
    )
