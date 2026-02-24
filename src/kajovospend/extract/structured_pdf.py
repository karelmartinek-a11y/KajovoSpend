from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict
from pathlib import Path
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

from pypdf import PdfReader
import xml.etree.ElementTree as ET

from kajovospend.extract.parser import Extracted

_DIGITS_RE = re.compile(r"\D+")
_AMOUNT_RE = re.compile(r"-?\d+[\d\s]*[.,]\d+")

def _to_float(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    s = s.strip().replace("\xa0", " ").replace(" ", "").replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except Exception:
        m = _AMOUNT_RE.search(s)
        if m:
            try:
                return float(m.group(0).replace(" ", "").replace(",", "."))
            except Exception:
                return None
    return None

def _to_date_yyyymmdd(s: Optional[str]) -> Optional[dt.date]:
    if not s:
        return None
    s = s.strip()
    if re.fullmatch(r"\d{8}", s):
        try:
            return dt.date(int(s[0:4]), int(s[4:6]), int(s[6:8]))
        except Exception:
            return None
    # try ISO
    try:
        return dt.date.fromisoformat(s[:10])
    except Exception:
        return None

def _strip_ico(val: Optional[str]) -> Optional[str]:
    if not val:
        return None
    d = _DIGITS_RE.sub("", val)
    if len(d) == 8:
        return d
    return d if d else None

def _xml_root(xml_bytes: bytes) -> Optional[ET.Element]:
    try:
        return ET.fromstring(xml_bytes)
    except Exception:
        return None

def _find_text(node: ET.Element, *paths: str) -> Optional[str]:
    # paths are XPath-ish with wildcard namespaces allowed via { * }
    for p in paths:
        try:
            el = node.find(p)
            if el is not None and el.text and el.text.strip():
                return el.text.strip()
        except Exception:
            continue
    # wildcard search helper: allow caller to pass './/{*}Tag'
    for p in paths:
        try:
            el = node.find(p.replace("{*}", "{*}"))
            if el is not None and el.text and el.text.strip():
                return el.text.strip()
        except Exception:
            continue
    return None

def _first(node: ET.Element, path: str) -> Optional[ET.Element]:
    try:
        return node.find(path)
    except Exception:
        return None

def _iter(node: ET.Element, path: str) -> List[ET.Element]:
    try:
        return list(node.findall(path))
    except Exception:
        return []

def extract_pdf_attachments(pdf_path: Path) -> Dict[str, bytes]:
    reader = PdfReader(BytesIO(pdf_path.read_bytes()))
    out: Dict[str, bytes] = {}

    # Newer pypdf exposes reader.attachments
    attachments = getattr(reader, "attachments", None)
    if attachments:
        try:
            for name, data_list in attachments.items():
                # pypdf returns list[bytes] for same name
                if not data_list:
                    continue
                data = data_list[0]
                if isinstance(data, (bytes, bytearray)):
                    out[str(name)] = bytes(data)
        except Exception:
            pass

    if out:
        return out

    # Fallback: walk EmbeddedFiles name tree
    try:
        root = reader.trailer["/Root"]
        names = root.get("/Names")
        if not names:
            return out
        emb = names.get("/EmbeddedFiles")
        if not emb:
            return out
        name_tree = emb.get("/Names")
        # name_tree is [name1, fileSpec1, name2, fileSpec2, ...]
        if isinstance(name_tree, list):
            for i in range(0, len(name_tree), 2):
                try:
                    nm = str(name_tree[i])
                    fs = name_tree[i + 1]
                    ef = fs.get("/EF")
                    if not ef:
                        continue
                    f = ef.get("/F")
                    if not f:
                        continue
                    data = f.get_data()
                    out[nm] = data
                except Exception:
                    continue
    except Exception:
        pass

    return out

def _parse_isdoc(root: ET.Element) -> Optional[Extracted]:
    # ISDOC has root like {http://isdoc.cz/namespace/2013}Invoice
    u = (root.tag or "").lower()
    if "isdoc" not in u and "invoice" not in u:
        # still might be isdoc with other tag; we'll just try anyway
        pass

    # header fields
    doc_number = (
        _find_text(root, ".//{*}ID", ".//{*}InvoiceNumber", ".//{*}DocumentNumber")
    )
    issue_date = _to_date_yyyymmdd(_find_text(root, ".//{*}IssueDate", ".//{*}IssueDateTime/.//{*}DateTimeString"))
    currency = _find_text(root, ".//{*}DocumentCurrencyCode") or "CZK"

    supplier_ico = _strip_ico(_find_text(root, ".//{*}SellerSupplierParty//{*}CompanyID", ".//{*}Seller//{*}CompanyID", ".//{*}Seller//{*}ID"))

    # totals
    total = (
        _to_float(_find_text(root, ".//{*}TaxInclusiveAmount", ".//{*}LegalMonetaryTotal//{*}TaxInclusiveAmount", ".//{*}PayableAmount"))
        or _to_float(_find_text(root, ".//{*}LegalMonetaryTotal//{*}PayableAmount"))
    )

    items: List[dict] = []
    for ln in _iter(root, ".//{*}InvoiceLine"):
        name = _find_text(ln, ".//{*}Item//{*}Description", ".//{*}Item//{*}Name", ".//{*}ItemName", ".//{*}Description") or "Položka"
        qty = _to_float(_find_text(ln, ".//{*}InvoicedQuantity", ".//{*}Quantity", ".//{*}DeliveredQuantity")) or 1.0
        unit_price = _to_float(_find_text(ln, ".//{*}PriceAmount", ".//{*}UnitPrice", ".//{*}NetPriceAmount"))
        line_total = _to_float(_find_text(ln, ".//{*}LineExtensionAmount", ".//{*}LineTotalAmount", ".//{*}TaxInclusiveAmount"))
        vat_rate = _to_float(_find_text(ln, ".//{*}Percent", ".//{*}RateApplicablePercent")) or 0.0

        if line_total is None and unit_price is not None:
            try:
                line_total = float(qty) * float(unit_price)
            except Exception:
                pass

        if unit_price is None and line_total is not None and qty:
            try:
                unit_price = float(line_total) / float(qty)
            except Exception:
                pass

        if line_total is None:
            continue

        items.append({
            "name": name,
            "quantity": float(qty),
            "unit_price": float(unit_price or 0.0),
            "vat_rate": float(vat_rate or 0.0),
            "line_total": float(line_total),
        })

    if not items and total is None and not doc_number:
        return None

    return Extracted(
        supplier_ico=supplier_ico,
        doc_number=doc_number,
        bank_account=None,
        issue_date=issue_date,
        total_with_vat=float(total) if total is not None else None,
        currency=currency,
        items=items,
        confidence=0.99,
        requires_review=False if items or total else True,
        review_reasons=[],
        full_text="",
    )

def _parse_cii(root: ET.Element) -> Optional[Extracted]:
    # CrossIndustryInvoice / Factur-X / ZUGFeRD
    doc_number = _find_text(root, ".//{*}ExchangedDocument//{*}ID", ".//{*}ExchangedDocument//{*}ID")
    issue_date = None
    dt_str = _find_text(root, ".//{*}ExchangedDocument//{*}IssueDateTime//{*}DateTimeString")
    issue_date = _to_date_yyyymmdd(dt_str) if dt_str else None

    # currency
    currency = _find_text(root, ".//{*}ApplicableHeaderTradeSettlement//{*}InvoiceCurrencyCode", ".//{*}DocumentCurrencyCode") or "CZK"

    # supplier ico - try schemeID and raw IDs
    supplier_ico = None
    for cand in [
        _find_text(root, ".//{*}SellerTradeParty//{*}ID"),
        _find_text(root, ".//{*}SellerTradeParty//{*}GlobalID"),
        _find_text(root, ".//{*}SellerTradeParty//{*}SpecifiedTaxRegistration//{*}ID"),
    ]:
        c = _strip_ico(cand)
        if c and len(c) == 8:
            supplier_ico = c
            break

    # totals
    total = _to_float(_find_text(root, ".//{*}SpecifiedTradeSettlementMonetarySummation//{*}GrandTotalAmount",
                                 ".//{*}SpecifiedTradeSettlementMonetarySummation//{*}DuePayableAmount"))

    items: List[dict] = []
    for li in _iter(root, ".//{*}IncludedSupplyChainTradeLineItem"):
        name = _find_text(li, ".//{*}SpecifiedTradeProduct//{*}Name", ".//{*}Product//{*}Name") or "Položka"
        qty = _to_float(_find_text(li, ".//{*}BilledQuantity", ".//{*}SpecifiedLineTradeDelivery//{*}BilledQuantity")) or 1.0
        unit_price = _to_float(_find_text(li, ".//{*}NetPriceProductTradePrice//{*}ChargeAmount", ".//{*}GrossPriceProductTradePrice//{*}ChargeAmount"))
        line_total = _to_float(_find_text(li, ".//{*}SpecifiedTradeSettlementLineMonetarySummation//{*}LineTotalAmount"))
        vat_rate = _to_float(_find_text(li, ".//{*}ApplicableTradeTax//{*}RateApplicablePercent")) or 0.0

        if line_total is None and unit_price is not None:
            try:
                line_total = float(qty) * float(unit_price)
            except Exception:
                pass
        if unit_price is None and line_total is not None and qty:
            try:
                unit_price = float(line_total) / float(qty)
            except Exception:
                pass
        if line_total is None:
            continue

        items.append({
            "name": name,
            "quantity": float(qty),
            "unit_price": float(unit_price or 0.0),
            "vat_rate": float(vat_rate or 0.0),
            "line_total": float(line_total),
        })

    if not items and total is None and not doc_number:
        return None

    return Extracted(
        supplier_ico=supplier_ico,
        doc_number=doc_number,
        bank_account=None,
        issue_date=issue_date,
        total_with_vat=float(total) if total is not None else None,
        currency=currency,
        items=items,
        confidence=0.99,
        requires_review=False if items or total else True,
        review_reasons=[],
        full_text="",
    )

def extract_structured_from_pdf(pdf_path: Path) -> Tuple[Optional[Extracted], Dict[str, Any]]:
    """Try to extract structured invoice data from PDF attachments (ISDOC / Factur-X / ZUGFeRD / generic XML).
    Returns (Extracted|None, debug_meta).
    """
    debug: Dict[str, Any] = {"used": False, "attachment_names": [], "matched": None, "errors": []}
    att = extract_pdf_attachments(pdf_path)
    if not att:
        return None, debug
    debug["attachment_names"] = sorted(att.keys())

    # try XML attachments by name and content
    candidates: List[Tuple[str, bytes]] = []
    for name, data in att.items():
        if name.lower().endswith(".xml") or b"<?xml" in data[:200]:
            candidates.append((name, data))

    for name, data in candidates:
        root = _xml_root(data)
        if root is None:
            continue
        tag = (root.tag or "").lower()
        try:
            if "crossindustryinvoice" in tag or "crossindustryinvoice" in ET.tostring(root, encoding="unicode").lower()[:500]:
                ex = _parse_cii(root)
                if ex:
                    debug["used"] = True
                    debug["matched"] = f"CII:{name}"
                    return ex, debug
            # ISDOC typically has isdoc namespace, but also invoice root
            if "isdoc" in tag or "invoice" in tag:
                ex = _parse_isdoc(root)
                if ex and (ex.items or ex.total_with_vat or ex.doc_number):
                    debug["used"] = True
                    debug["matched"] = f"ISDOC:{name}"
                    return ex, debug
            # try both parsers heuristically
            ex = _parse_isdoc(root)
            if ex and (ex.items or ex.total_with_vat):
                debug["used"] = True
                debug["matched"] = f"ISDOC?:{name}"
                return ex, debug
            ex = _parse_cii(root)
            if ex and (ex.items or ex.total_with_vat):
                debug["used"] = True
                debug["matched"] = f"CII?:{name}"
                return ex, debug
        except Exception as e:
            debug["errors"].append(f"{name}: {e}")

    return None, debug
