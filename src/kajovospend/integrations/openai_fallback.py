from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Sequence

import requests


@dataclass
class OpenAIConfig:
    api_key: str
    model: str
    fallback_model: str | None = None
    use_json_schema: bool = True
    temperature: float = 0.0
    max_output_tokens: int = 2000


_MODEL_CACHE: dict[str, Any] = {"ts": 0.0, "ids": []}
_MODEL_CACHE_TTL_SEC = 300

# Prefer nejvyssi kvalitu (s vision) – pokud neni dostupna, padame nize.
_MODEL_PREFER_PRIMARY = [
    "gpt-5.2",
    "gpt-5.1",
    "gpt-5",
    "gpt-4.1",
    "gpt-4o",
    "gpt-4.1-mini",
    "gpt-4o-mini",
]
_MODEL_PREFER_FALLBACK = [
    "gpt-4.1",
    "gpt-4o",
    "gpt-4.1-mini",
    "gpt-4o-mini",
]


def list_models(api_key: str, timeout: int = 20) -> List[str]:
    r = requests.get(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    ids = [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]
    ids = [i for i in ids if isinstance(i, str)]
    # show newest/common first
    return sorted(ids)


_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "document_type": {"type": ["string", "null"], "enum": ["invoice", "receipt", "credit_note", "other", None]},
        "invoice_number": {"type": ["string", "null"]},
        "issue_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "due_date": {"type": ["string", "null"]},
        "currency": {"type": ["string", "null"]},
        "supplier": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": ["string", "null"]},
                "vat_id": {"type": ["string", "null"]},
                "company_id": {"type": ["string", "null"]},
                "address": {"type": ["string", "null"]},
                "iban": {"type": ["string", "null"]},
                "bic": {"type": ["string", "null"]},
            },
        },
        "buyer": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": ["string", "null"]},
                "vat_id": {"type": ["string", "null"]},
                "company_id": {"type": ["string", "null"]},
                "address": {"type": ["string", "null"]},
            },
        },
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "description": {"type": ["string", "null"]},
                    "quantity": {"type": ["number", "null"]},
                    "unit": {"type": ["string", "null"]},
                    "unit_price_net": {"type": ["number", "null"]},
                    "vat_rate": {"type": ["number", "null"]},
                    "vat_amount": {"type": ["number", "null"]},
                    "total_gross": {"type": ["number", "null"]},
                },
            },
        },
        "totals": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "subtotal_net": {"type": ["number", "null"]},
                "vat_total": {"type": ["number", "null"]},
                "total_gross": {"type": ["number", "null"]},
            },
        },
        "payment": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "iban": {"type": ["string", "null"]},
                "bic": {"type": ["string", "null"]},
                "account": {"type": ["string", "null"]},
                "vs": {"type": ["string", "null"]},
            },
        },
    },
    "required": ["line_items", "totals"],
}


def _list_models_cached(api_key: str) -> List[str]:
    now = time.time()
    if (_MODEL_CACHE.get("ids") and (now - float(_MODEL_CACHE.get("ts", 0.0)) < _MODEL_CACHE_TTL_SEC)):
        return list(_MODEL_CACHE.get("ids") or [])
    ids = list_models(api_key)
    _MODEL_CACHE["ts"] = now
    _MODEL_CACHE["ids"] = list(ids)
    return ids


def _resolve_model(api_key: str, model: str | None, prefer: Sequence[str]) -> str:
    if model and str(model).strip() and str(model).strip().lower() != "auto":
        return str(model).strip()
    try:
        ids = _list_models_cached(api_key)
        for cand in prefer:
            if cand in ids:
                return cand
    except Exception:
        pass
    # fallback: prvni preferovany
    return str(prefer[0]) if prefer else (str(model or ""))


def _b64_data_url(mime: str, data: bytes) -> str:
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _build_text_format(use_json_schema: bool) -> Dict[str, Any]:
    if use_json_schema:
        return {
            "type": "json_schema",
            "name": "receipt_extract",
            "schema": _JSON_SCHEMA,
            "strict": True,
        }
    return {"type": "json_object"}


def _build_prompt(ocr_text: str, *, mode: str) -> str:
    base = (
        "Jsi extrakcni system pro ceske doklady (uctenky, faktury). "
        "Vrat POUZE validni JSON podle schematu. "
        "Nevymyslej si hodnoty; kdyz si nejsi jist, dej null nebo prazdne pole. "
        "Polozky vracej tak, jak jsou na dokladu; unit_price bez DPH, line_total s DPH. "
        "Nepredpokladej layout, pracuj jen s obsahem (text + obraz)."
    )
    if mode == "fallback":
        base += (
            " Pokud jsou udaje rozbite, zkus je opravit z kontextu, ale stale nehalucinuj. "
            "Pro datum pouzij format YYYY-MM-DD."
        )
    prompt = base + "\n\nOCR text dokladu:\n" + (ocr_text or "")
    return prompt


def _normalize_extracted_payload(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Prevede odpoved ze schema OpenAI na interni tvar, ktery ocekava processor."""
    out = dict(obj)

    if not out.get("doc_number") and out.get("invoice_number"):
        out["doc_number"] = out.get("invoice_number")

    supplier = out.get("supplier") if isinstance(out.get("supplier"), dict) else {}
    if not out.get("supplier_ico"):
        company_id = supplier.get("company_id")
        if company_id:
            out["supplier_ico"] = company_id

    payment = out.get("payment") if isinstance(out.get("payment"), dict) else {}
    if not out.get("bank_account"):
        for key in ("iban", "account"):
            val = payment.get(key) or supplier.get(key)
            if val:
                out["bank_account"] = val
                break

    totals = out.get("totals") if isinstance(out.get("totals"), dict) else {}
    if out.get("total_with_vat") is None:
        out["total_with_vat"] = totals.get("total_gross")
    if out.get("total_without_vat") is None:
        out["total_without_vat"] = totals.get("subtotal_net")
    if out.get("total_vat_amount") is None:
        out["total_vat_amount"] = totals.get("vat_total")

    line_items = out.get("line_items")
    if isinstance(line_items, list) and line_items and not out.get("items"):
        normalized_items = []
        for it in line_items:
            if not isinstance(it, dict):
                continue
            normalized_items.append(
                {
                    "name": it.get("description"),
                    "quantity": it.get("quantity"),
                    "unit": it.get("unit"),
                    "unit_price": it.get("unit_price_net"),
                    "vat_rate": it.get("vat_rate"),
                    "vat_amount": it.get("vat_amount"),
                    "line_total": it.get("total_gross"),
                }
            )
        if normalized_items:
            out["items"] = normalized_items

    return out

def extract_with_openai(
    cfg: OpenAIConfig,
    ocr_text: str,
    images: Optional[Sequence[Tuple[str, bytes]]] = None,
    pdf: Optional[Tuple[str, bytes]] = None,
    timeout: int = 40,
) -> Tuple[Optional[Dict[str, Any]], str, str]:
    # Responses API supports multimodal input items (input_text + input_image).
    # image_url může být i base64 data URL.
    # Zapneme JSON mode přes text.format (json_schema/json_object).
    model = _resolve_model(cfg.api_key, cfg.model, _MODEL_PREFER_PRIMARY)
    prompt = _build_prompt(ocr_text, mode="primary")

    content: List[Dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    if pdf and pdf[1]:
        mime, data = pdf
        content.insert(0, {"type": "input_file", "file_data": _b64_data_url(mime, data), "filename": "document.pdf"})
    if images:
        for mime, data in images:
            if not data:
                continue
            mm = mime if mime in ("image/png", "image/jpeg", "image/webp") else "image/png"
            content.append({"type": "input_image", "image_url": _b64_data_url(mm, data)})

    payload = {
        "model": model,
        "input": [{"role": "user", "content": content}],
        "text": {"format": _build_text_format(bool(cfg.use_json_schema))},
        "temperature": float(cfg.temperature or 0.0),
        "max_output_tokens": int(cfg.max_output_tokens or 2000),
    }

    def _post(pl: Dict[str, Any]):
        return requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"},
            json=pl,
            timeout=timeout,
        )

    r = _post(payload)
    if r.status_code >= 400 and cfg.use_json_schema:
        # fallback: json_object (nektere modely/json_schema odmita)
        payload["text"] = {"format": {"type": "json_object"}}
        r = _post(payload)
    r.raise_for_status()
    data = r.json()
    # responses API: output_text may be in 'output' array
    text = ""
    try:
        out = data.get("output", [])
        for item in out:
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        text += c.get("text", "")
    except Exception:
        pass
    if not text:
        text = str(data)
    # parse JSON from text
    try:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            obj = json.loads(text[start:end+1])
            if isinstance(obj, dict):
                return _normalize_extracted_payload(obj), text, model
    except Exception:
        return None, text, model
    return None, text, model


def extract_with_openai_fallback(
    cfg: OpenAIConfig,
    ocr_text: str,
    images: Optional[Sequence[Tuple[str, bytes]]] = None,
    pdf: Optional[Tuple[str, bytes]] = None,
    timeout: int = 40,
) -> Tuple[Optional[Dict[str, Any]], str, str]:
    model = _resolve_model(cfg.api_key, cfg.fallback_model or cfg.model, _MODEL_PREFER_FALLBACK)
    prompt = _build_prompt(ocr_text, mode="fallback")
    content: List[Dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    if pdf and pdf[1]:
        mime, data = pdf
        content.insert(0, {"type": "input_file", "file_data": _b64_data_url(mime, data), "filename": "document.pdf"})
    if images:
        for mime, data in images:
            if not data:
                continue
            mm = mime if mime in ("image/png", "image/jpeg", "image/webp") else "image/png"
            content.append({"type": "input_image", "image_url": _b64_data_url(mm, data)})

    payload = {
        "model": model,
        "input": [{"role": "user", "content": content}],
        "text": {"format": _build_text_format(bool(cfg.use_json_schema))},
        "temperature": float(cfg.temperature or 0.0),
        "max_output_tokens": int(cfg.max_output_tokens or 2000),
    }
    r = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    if r.status_code >= 400 and cfg.use_json_schema:
        payload["text"] = {"format": {"type": "json_object"}}
        r = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
    r.raise_for_status()
    data = r.json()
    text = ""
    try:
        out = data.get("output", [])
        for item in out:
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        text += c.get("text", "")
    except Exception:
        pass
    if not text:
        text = str(data)
    try:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict):
                return _normalize_extracted_payload(obj), text, model
    except Exception:
        return None, text, model
    return None, text, model
