from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

FIELD_LEGEND: Tuple[Tuple[str, str], ...] = (
    ("supplier_ico", "#FF0000"),
    ("doc_number", "#00FF00"),
    ("issue_date", "#0000FF"),
    ("total_with_vat", "#FFD700"),
    ("bank_account", "#FF00FF"),
    ("items_region", "#00FFFF"),
)


def legend_text() -> str:
    return "\n".join(f"{name}: {color}" for name, color in FIELD_LEGEND)


class TemplateSchemaError(ValueError):
    pass


@dataclass(frozen=True)
class TemplateField:
    name: str
    page: int
    box: Tuple[float, float, float, float]


@dataclass(frozen=True)
class TemplateSchema:
    version: int
    fields: Dict[str, TemplateField]


def serialize_template_schema(schema: TemplateSchema) -> str:
    payload = {
        "version": int(schema.version),
        "fields": {
            name: {
                "page": int(field.page),
                "box": [float(field.box[0]), float(field.box[1]), float(field.box[2]), float(field.box[3])],
            }
            for name, field in sorted(schema.fields.items())
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _validate_box(box: Iterable[Any]) -> Tuple[float, float, float, float]:
    values = list(box)
    if len(values) != 4:
        raise TemplateSchemaError("Souradnice boxu musi obsahovat 4 hodnoty.")
    coords: List[float] = []
    for value in values:
        try:
            num = float(value)
        except Exception as exc:
            raise TemplateSchemaError("Souradnice boxu musi byt cisla v rozmezi 0..1.") from exc
        if not (0.0 <= num <= 1.0):
            raise TemplateSchemaError("Souradnice boxu musi byt v intervalu 0..1.")
        coords.append(num)
    x0, y0, x1, y1 = coords
    if x0 >= x1 or y0 >= y1:
        raise TemplateSchemaError("Souradnice boxu musi mit x0<x1 a y0<y1.")
    return (x0, y0, x1, y1)


def parse_template_schema_dict(schema: Mapping[str, Any]) -> TemplateSchema:
    if not isinstance(schema, Mapping):
        raise TemplateSchemaError("Schema musi byt JSON objekt.")
    version = schema.get("version")
    if version != 1:
        raise TemplateSchemaError("Podporovana verze schematu je 1.")
    fields = schema.get("fields")
    if not isinstance(fields, Mapping):
        raise TemplateSchemaError("Schema musi obsahovat objekt fields.")
    parsed: Dict[str, TemplateField] = {}
    required = {"supplier_ico", "doc_number", "issue_date", "total_with_vat"}
    for name, raw in fields.items():
        if not isinstance(raw, Mapping):
            raise TemplateSchemaError(f"Pole {name} musi byt objekt s page/box.")
        page = raw.get("page")
        try:
            page_num = int(page or 1)
        except Exception as exc:
            raise TemplateSchemaError(f"Pole {name}: 'page' musi byt cislo.") from exc
        if page_num < 1:
            raise TemplateSchemaError(f"Pole {name}: 'page' musi byt >= 1.")
        box = raw.get("box")
        if box is None:
            raise TemplateSchemaError(f"Pole {name} chybi box.")
        coords = _validate_box(box)
        parsed[name] = TemplateField(name=name, page=page_num, box=coords)
    missing = required - set(parsed.keys())
    if missing:
        raise TemplateSchemaError(f"Schema musi obsahovat pole {', '.join(sorted(missing))}.")
    return TemplateSchema(version=1, fields=parsed)


def parse_template_schema_text(text: str) -> TemplateSchema:
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TemplateSchemaError(f"Chybny JSON: {exc}") from exc
    return parse_template_schema_dict(doc)


def validate_template_schema_text(text: str) -> Tuple[bool, str | None]:
    try:
        parse_template_schema_text(text)
        return True, None
    except TemplateSchemaError as exc:
        return False, str(exc)


def _normalize_digits(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D+", "", str(value))
    if not digits:
        return None
    if len(digits) < 8:
        return digits.zfill(8)
    return digits


def normalized_box_to_pixel_box(
    box: Tuple[float, float, float, float],
    width: int,
    height: int,
) -> Tuple[int, int, int, int]:
    if width <= 0 or height <= 0:
        raise TemplateSchemaError("Neplatna velikost stranky pro prevod ROI.")
    x0, y0, x1, y1 = _validate_box(box)
    px0 = max(0, min(width, int(round(x0 * width))))
    py0 = max(0, min(height, int(round(y0 * height))))
    px1 = max(0, min(width, int(round(x1 * width))))
    py1 = max(0, min(height, int(round(y1 * height))))
    if px1 <= px0 or py1 <= py0:
        raise TemplateSchemaError("ROI je po prevodu prazdna.")
    return (px0, py0, px1, py1)


def match_template(template: Any, full_text: str) -> bool:
    text = (full_text or "").lower()
    has_rule = False

    ico_norm = getattr(template, "match_supplier_ico_norm", None)
    if ico_norm:
        has_rule = True
        normalized = _normalize_digits(ico_norm)
        if normalized is None:
            return False
        digits = re.sub(r"\D+", "", text)
        if normalized not in digits:
            return False

    match_texts = getattr(template, "match_texts_json", None)
    if match_texts:
        has_rule = True
        try:
            parsed = json.loads(match_texts)
        except Exception:
            parsed = []
        tokens: List[str]
        if isinstance(parsed, Iterable):
            tokens = [str(t).strip().lower() for t in parsed if isinstance(t, (str, int, float)) and str(t).strip()]
        else:
            tokens = []
        for token in tokens:
            if token not in text:
                return False

    if not has_rule:
        return False
    return True


def _find_number_token(text: str) -> Optional[str]:
    for token in re.findall(r"[A-Za-z0-9/\-\.]{3,}", text or ""):
        stripped = token.strip("/-.")
        if len(stripped) >= 3:
            return stripped
    return None


def _parse_date(text: str) -> Optional[Any]:
    from dateutil import parser as dtparser

    if not text:
        return None
    txt = text.strip()
    try:
        parsed = dtparser.parse(txt, dayfirst=True, fuzzy=True)
        return parsed.date()
    except Exception:
        m = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})", txt)
        if not m:
            return None
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        try:
            import datetime as dt

            return dt.date(year, month, day)
        except Exception:
            return None


def _parse_amount_value(text: str) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"([0-9][0-9\s]*[,\.][0-9]{2})", text)
    if not m:
        m = re.search(r"([0-9][0-9\s]*[.,][0-9]{1,2})", text)
    if not m:
        return None
    num = m.group(1).replace("\xa0", " ").replace(" ", "").replace(",", ".")
    try:
        return float(num)
    except Exception:
        return None


def _extract_currency(text: str) -> str:
    upper = (text or "").upper()
    if "EUR" in upper:
        return "EUR"
    if "KČ" in upper or "KC" in upper or "CZK" in upper:
        return "CZK"
    return "CZK"


def _extract_bank_account(text: str) -> Optional[str]:
    if not text:
        return None
    token = text.strip()
    m = re.search(r"\b([A-Z]{2}\d{2}[A-Z0-9]{10,})\b", token)
    if m:
        return m.group(1).replace(" ", "")
    m2 = re.search(r"\b(\d{6,}-?\d{2,}/\d{4})\b", token)
    if m2:
        return m2.group(1).replace(" ", "")
    return None


def extract_using_template(
    pdf_path: Path,
    template: Any,
    ocr_engine: Any,
    cfg: Mapping[str, Any],
    *,
    full_text: str,
) -> "Extracted":
    if ocr_engine is None:
        raise TemplateSchemaError("OCR engine neni dostupny pro sablonu.")

    schema = parse_template_schema_text(template.schema_json)
    pages = sorted({field.page for field in schema.fields.values()})
    if not pages:
        raise TemplateSchemaError("Sablona neobsahuje zadna pole.")

    ocr_cfg = cfg.get("ocr") if isinstance(cfg, Mapping) else {}
    dpi = int(ocr_cfg.get("pdf_dpi", 300) or 300)
    dpi = max(200, min(dpi, 600))

    from PIL import Image

    from kajovospend.extract.parser import Extracted, extract_from_text, postprocess_items_for_db

    page_map: Dict[int, Image.Image] = {}
    try:
        from kajovospend.ocr.pdf_render import render_pdf_to_images

        for page_no in pages:
            start_page = max(0, int(page_no) - 1)
            images = render_pdf_to_images(pdf_path, dpi=dpi, start_page=start_page, max_pages=1)
            if images:
                page_map[int(page_no)] = images[0]
    except Exception as exc:
        raise TemplateSchemaError(f"Sablona: nepodarilo se renderovat PDF - {exc}") from exc

    field_texts: Dict[str, str] = {}
    confidences: List[float] = []
    for field in schema.fields.values():
        img = page_map.get(field.page)
        if img is None:
            continue
        w, h = img.size
        try:
            x0, y0, x1, y1 = normalized_box_to_pixel_box(field.box, w, h)
        except TemplateSchemaError:
            continue
        try:
            crop = img.crop((x0, y0, x1, y1))
            text, conf = ocr_engine.image_to_text(crop)
            raw_text = (text or "").strip()
            field_texts[field.name] = raw_text
            cval = float(conf or 0.0)
            confidences.append(cval)
        except Exception:
            continue

    supplier_txt = field_texts.get("supplier_ico", "")
    doc_txt = field_texts.get("doc_number", "")
    issue_txt = field_texts.get("issue_date", "")
    total_txt = field_texts.get("total_with_vat", "")
    bank_txt = field_texts.get("bank_account", "")
    items_txt = field_texts.get("items_region", "")

    supplier_ico = _normalize_digits(supplier_txt)
    doc_number = _find_number_token(doc_txt)
    issue_date = _parse_date(issue_txt)
    total_value = _parse_amount_value(total_txt)
    bank_account = _extract_bank_account(bank_txt)
    currency = _extract_currency(total_txt or full_text)

    items: List[dict] = []
    reasons: List[str] = []
    total_without_vat: Optional[float] = None
    total_vat_amount: Optional[float] = None
    vat_breakdown_json: Optional[str] = None
    sum_ok = False
    if items_txt:
        items_ex = extract_from_text(items_txt)
        items = list(items_ex.items or [])
        base_reasons = list(items_ex.review_reasons or [])
        sum_ok, base_reasons, total_without_vat, total_vat_amount, vat_breakdown_json = postprocess_items_for_db(
            items=items,
            total_with_vat=total_value,
            reasons=base_reasons,
        )
        reasons.extend(base_reasons)

    conf = float(sum(confidences) / len(confidences)) if confidences else 0.0
    if supplier_ico:
        conf = min(1.0, conf + 0.15)
    if doc_number:
        conf = min(1.0, conf + 0.1)
    if total_value:
        conf = min(1.0, conf + 0.15)
    if items:
        conf = min(1.0, conf + 0.05)

    review_reasons: List[str] = list(dict.fromkeys(reasons))
    if not supplier_ico:
        review_reasons.append("Sablona: chybi ICO.")
    if not doc_number:
        review_reasons.append("Sablona: chybi cislo dokladu.")
    if issue_date is None:
        review_reasons.append("Sablona: chybi datum.")
    if total_value is None or (total_value is not None and total_value <= 0.0):
        review_reasons.append("Sablona: chybi nebo nulova castka.")
    if items_txt and not items:
        review_reasons.append("Sablona: nelze extrahovat polozky.")

    requires_review = (
        not (supplier_ico and doc_number and issue_date and total_value and total_value > 0.0)
        or (items_txt and not sum_ok)
    )

    extracted = Extracted(
        supplier_ico=supplier_ico,
        doc_number=doc_number,
        bank_account=bank_account,
        issue_date=issue_date,
        total_with_vat=total_value,
        total_without_vat=total_without_vat,
        total_vat_amount=total_vat_amount,
        vat_breakdown_json=vat_breakdown_json,
        currency=currency,
        items=items,
        confidence=min(1.0, max(0.0, conf)),
        requires_review=requires_review,
        review_reasons=review_reasons,
        full_text=full_text or "",
    )
    return extracted
