from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import requests


@dataclass(frozen=True)
class AresRecord:
    ico: str
    name: str | None
    dic: str | None
    address: str | None
    is_vat_payer: bool | None
    fetched_at: dt.datetime
    legal_form: str | None = None
    street: str | None = None
    street_number: str | None = None
    orientation_number: str | None = None
    city: str | None = None
    zip_code: str | None = None


_cache: dict[str, AresRecord] = {}


def compose_address(street: str | None, cp: str | None, co: str | None, city: str | None, zip_code: str | None) -> str | None:
    parts = []
    s = (street or "").strip()
    cpv = (cp or "").strip()
    cov = (co or "").strip()
    if s or cpv or cov:
        num = cpv + (f"/{cov}" if cov else "")
        first = (s + " " + num).strip()
        if first:
            parts.append(first)
    if (city or "").strip():
        parts.append(city.strip())
    if (zip_code or "").strip():
        parts.append(zip_code.strip())
    return ", ".join(parts) if parts else None


def fetch_by_ico(ico: str) -> AresRecord:
    ico = (ico or "").strip()
    if not ico:
        raise ValueError("IČO je prázdné")
    if ico in _cache:
        return _cache[ico]

    url = f"https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{ico}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    obj = r.json()

    name = (obj.get("obchodniJmeno") or obj.get("nazev") or None)
    dic = (obj.get("dic") or None)

    legal_form: str | None = None
    pf = obj.get("pravniForma") or obj.get("pravniFormaPodnikani") or obj.get("pravniFormaRZP")
    if isinstance(pf, dict):
        legal_form = pf.get("text") or pf.get("nazev") or pf.get("popis") or None
    elif isinstance(pf, str):
        legal_form = pf or None

    # VAT payer: best-effort (ARES may provide different keys per source)
    is_vat = None
    vat = obj.get("platceDph") or obj.get("jePlatceDph") or obj.get("platceDPH")
    if isinstance(vat, bool):
        is_vat = vat

    street = None
    cp = None
    co = None
    city = None
    zip_code = None
    adr = obj.get("sidlo") or obj.get("adresa") or obj.get("adresaSidla")
    if isinstance(adr, dict):
        street = adr.get("nazevUlice") or adr.get("ulice")
        cp = str(adr.get("cisloDomovni") or adr.get("cisloPopisne") or "") or None
        co = str(adr.get("cisloOrientacni") or "") or None
        city = adr.get("nazevObce") or adr.get("obec")
        zip_code = str(adr.get("psc") or adr.get("PSC") or "") or None

    address = compose_address(street, cp, co, city, zip_code)
    now = dt.datetime.utcnow()
    rec = AresRecord(
        ico=ico,
        name=name,
        dic=dic,
        address=address,
        is_vat_payer=is_vat,
        fetched_at=now,
        legal_form=legal_form,
        street=street,
        street_number=cp,
        orientation_number=co,
        city=city,
        zip_code=zip_code,
    )
    _cache[ico] = rec
    return rec
