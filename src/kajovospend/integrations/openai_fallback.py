from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Sequence

import requests

from kajovospend.utils.forensic_context import forensic_scope, get_forensic_fields
from kajovospend.utils.logging_setup import log_event


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
_RETRYABLE_HTTP_STATUSES = {408, 409, 429, 500, 502, 503, 504}
_MAX_HTTP_RETRIES = 2
_RETRY_BASE_DELAY_SEC = 0.5

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


class SchemaInvariantError(ValueError):
    """Schéma porušuje interní invarianty požadované OpenAI strict json_schema."""


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_json(obj: Any) -> str:
    return _sha256_bytes(_canonical_json(obj).encode("utf-8"))


def _redact_value(val: Any) -> Dict[str, Any]:
    if val is None:
        return {"value": None, "redaction": True}
    s = str(val)
    mask = None
    if len(s) >= 4:
        mask = f"***{s[-4:]}"
    return {
        "hash": _sha256_bytes(s.encode("utf-8")),
        "mask": mask,
        "redaction": True,
        "version": 1,
    }


def redact(obj: Any) -> Any:
    """
    Lehká redakce citlivých polí – vrací strukturu se stejným tvarem, ale citlivé hodnoty jsou
    nahrazeny hash/maskou. Používáme jen pro logování.
    """
    sensitive_keys = {"iban", "bic", "account", "bank_account", "vat_id", "dic", "ico", "company_id", "address"}
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in sensitive_keys:
                out[k] = _redact_value(v)
            else:
                out[k] = redact(v)
        out["redaction"] = {"enabled": True, "version": 1}
        return out
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


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
        "document_type": {"type": ["string", "null"]},
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
            "required": ["name", "vat_id", "company_id", "address", "iban", "bic"],
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
            "required": ["name", "vat_id", "company_id", "address"],
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
                "required": [],
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
            "required": ["subtotal_net", "vat_total", "total_gross"],
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
            "required": ["iban", "bic", "account", "vs"],
        },
    },
    "required": [
        "document_type",
        "invoice_number",
        "issue_date",
        "due_date",
        "currency",
        "supplier",
        "buyer",
        "line_items",
        "totals",
        "payment",
    ],
}


def canonicalize_openai_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Zkanonizuje schema pro OpenAI strict režim:
    - každý object node s properties má required = všechny keys v properties
    - pokud object node nemá additionalProperties, doplní false
    """

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {k: _walk(v) for k, v in node.items()}
            node_type = out.get("type")
            has_object = node_type == "object" or (isinstance(node_type, list) and "object" in node_type)
            props = out.get("properties")
            if has_object and isinstance(props, dict):
                out["required"] = sorted(props.keys())
                if "additionalProperties" not in out:
                    out["additionalProperties"] = False
            return out
        if isinstance(node, list):
            return [_walk(item) for item in node]
        return node

    return _walk(schema)


def _extract_schema_path_hint(error_message: str | None) -> Optional[str]:
    if not error_message:
        return None
    match = re.search(r"context\s*\(([^\)]+)\)", error_message)
    if not match:
        return None
    items = [part.strip().strip("'\"") for part in match.group(1).split(",")]
    cleaned = [it for it in items if it]
    if not cleaned:
        return None
    return "$." + ".".join(cleaned)


def validate_schema_invariants_or_raise(schema: Dict[str, Any], *, log: logging.Logger | None = None) -> None:
    """Fail-fast kontrola konzistence OpenAI json_schema."""

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            node_type = node.get("type")
            has_object = node_type == "object" or (isinstance(node_type, list) and "object" in node_type)
            props = node.get("properties")
            if has_object and isinstance(props, dict):
                required = node.get("required")
                if not isinstance(required, list):
                    if log:
                        log.error("Schema invariant porusen: required neni list", extra={"schema_path": path})
                    raise SchemaInvariantError(f"{path}: required must be list")
                missing = sorted(key for key in props.keys() if key not in required)
                if missing:
                    if log:
                        log.error(
                            "Schema invariant porusen: required neobsahuje vsechny properties",
                            extra={"schema_path": path, "missing_required_keys": missing},
                        )
                    raise SchemaInvariantError(f"{path}: missing required keys {missing}")
            for key, value in node.items():
                _walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                _walk(value, f"{path}[{idx}]")

    _walk(schema, "$")


_OPENAI_JSON_SCHEMA: Dict[str, Any] = canonicalize_openai_schema(_JSON_SCHEMA)
_JSON_SCHEMA_HASH = _sha256_json(_OPENAI_JSON_SCHEMA)


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
        validate_schema_invariants_or_raise(_OPENAI_JSON_SCHEMA)
        return {
            "type": "json_schema",
            "name": "receipt_extract",
            "schema": _OPENAI_JSON_SCHEMA,
            "strict": True,
        }
    return {"type": "json_object"}


def _build_prompt(ocr_text: str, *, mode: str) -> str:
    base = (
        "Jsi extrakcni system pro ceske doklady (uctenky, faktury). "
        "Vrat POUZE validni JSON podle schematu. "
        "Vrat VSECHNY klice definovane schematem; pokud hodnotu neznas, nastav null pro string/number, {} pro object, [] pro array. "
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


def _validate_type(value: Any, expected) -> bool:
    if isinstance(expected, list):
        return any(_validate_type(value, e) for e in expected)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float))
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "null":
        return value is None
    return False


def _validate_against_schema(obj: Any, schema: Dict[str, Any], path: str = "") -> List[str]:
    """Minimální validační logika bez externích závislostí."""
    errors: List[str] = []
    typ = schema.get("type")
    if typ and not _validate_type(obj, typ):
        errors.append(f"{path or '$'}: type")
        return errors

    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = schema.get("required", []) if isinstance(schema, dict) else []

    # additionalProperties check: pokud je False, properties musí existovat
    if schema.get("additionalProperties") is False and not props:
        errors.append(f"{path or '$'}: additionalProperties_false_no_props")

    # required musí být podmnožinou properties
    for req in required:
        if props and req not in props:
            errors.append(f"{path or '$'}: required_missing_property {req}")

    if isinstance(obj, dict):
        allowed_keys = set(props.keys())
        additional = schema.get("additionalProperties", True)
        for k, v in obj.items():
            child_path = f"{path}.{k}" if path else k
            if k in props:
                errors.extend(_validate_against_schema(v, props[k], child_path))
            elif additional is False:
                errors.append(f"{child_path}: additionalProperties")
        for req in required:
            if req not in obj:
                errors.append(f"{path or '$'}: missing {req}")
    elif isinstance(obj, list):
        item_schema = schema.get("items")
        if item_schema:
            for idx, itm in enumerate(obj):
                errors.extend(_validate_against_schema(itm, item_schema, f"{path}[{idx}]"))
    return errors


def _extract_output_text(data: Dict[str, Any]) -> str:
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
    return text


def ensure_schema_defaults(obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    Doplňuje chybějící klíče dle _JSON_SCHEMA na výchozí hodnoty (null/{} / [] podle typu).
    """

    schema_root = _OPENAI_JSON_SCHEMA

    def _default_for(schema: Dict[str, Any]):
        t = schema.get("type")
        if isinstance(t, list):
            # prefer null as safe default
            if "null" in t:
                return None
            t = t[0] if t else None
        if t == "object":
            return {}
        if t == "array":
            return []
        return None

    def _fill(data: Any, schema: Dict[str, Any]) -> Any:
        if isinstance(data, dict) and schema.get("type") in (["object"], "object", ["object", "null"]):
            props = schema.get("properties", {}) or {}
            out = dict(data)
            for key, prop_schema in props.items():
                if key not in out or out[key] is None:
                    out[key] = _default_for(prop_schema)
                out[key] = _fill(out[key], prop_schema)
            return out
        if isinstance(data, list) and schema.get("type") in (["array"], "array", ["array", "null"]):
            item_schema = schema.get("items", {})
            return [_fill(x, item_schema) for x in data]
        return data

    filled = _fill(obj or {}, schema_root)
    # top-level required fill
    for req in schema_root.get("required", []):
        if req not in filled:
            schema_for = schema_root.get("properties", {}).get(req, {})
            filled[req] = _default_for(schema_for)
            filled[req] = _fill(filled[req], schema_for)
    return filled


def _openai_post_responses(payload: Dict[str, Any], *, timeout: int, log, mode: str, attempt: int, api_key: str) -> Tuple[requests.Response, float, Optional[str], Optional[str], str]:
    """
    Vykoná jeden HTTP POST na /v1/responses, zaloguje openai.request + případný openai.error.
    Vrací (response, latency_ms, response_body_hash, openai_request_id, openai_request_id_client).
    """
    req_id_client = str(uuid.uuid4())
    body_canon = _canonical_json(payload)
    body_hash = _sha256_bytes(body_canon.encode("utf-8"))
    text_format = payload.get("text", {}).get("format", {})
    fmt_type = text_format.get("type") if isinstance(text_format, dict) else None
    schema_hash = _JSON_SCHEMA_HASH if fmt_type == "json_schema" else None

    prompt_text = ""
    try:
        for part in payload.get("input", [])[0].get("content", []):
            if part.get("type") == "input_text":
                prompt_text = part.get("text", "")
                break
    except Exception:
        prompt_text = ""
    prompt_meta = {
        "hash": _sha256_bytes(prompt_text.encode("utf-8")) if prompt_text else None,
        "length": len(prompt_text or ""),
    }

    attachments_meta: List[Dict[str, Any]] = []
    try:
        for part in payload.get("input", [])[0].get("content", []):
            if part.get("type") in ("input_image", "input_file"):
                data_url = part.get("image_url") or part.get("file_data")
                meta: Dict[str, Any] = {
                    "type": part.get("type"),
                    "mime": None,
                    "size_bytes": None,
                    "content_hash": None,
                }
                if isinstance(data_url, str) and data_url.startswith("data:") and ";base64," in data_url:
                    mime = data_url.split("data:", 1)[1].split(";")[0]
                    meta["mime"] = mime
                    try:
                        b64 = data_url.split(";base64,", 1)[1]
                        b = base64.b64decode(b64, validate=False)
                        meta["size_bytes"] = len(b)
                        meta["content_hash"] = _sha256_bytes(b)
                    except Exception:
                        pass
                attachments_meta.append(meta)
    except Exception:
        pass

    with forensic_scope(openai_request_id_client=req_id_client, attempt=attempt, mode=mode):
        log_event(
            log,
            "openai.request",
            "OpenAI request",
            endpoint="/v1/responses",
            base_url="https://api.openai.com",
            timeout_sec=timeout,
            model=payload.get("model"),
            mode=mode,
            text_format=fmt_type,
            schema_hash=schema_hash,
            prompt_hash=prompt_meta["hash"],
            prompt_length=prompt_meta["length"],
            attachments=attachments_meta,
            request_body_hash=body_hash,
            openai_request_id_client=req_id_client,
        )

        start = time.perf_counter()
        try:
            r = requests.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
            latency_ms = (time.perf_counter() - start) * 1000.0
            raw_body = r.content or b""
            resp_hash = _sha256_bytes(raw_body) if raw_body else None
            openai_request_id = r.headers.get("x-request-id")
            if r.status_code >= 400:
                err_json = None
                err_msg = None
                try:
                    err_json = r.json()
                except Exception:
                    try:
                        err_msg = raw_body.decode("utf-8", errors="replace")
                    except Exception:
                        err_msg = str(raw_body)
                log_event(
                    log,
                    "openai.error",
                    "OpenAI HTTP error",
                    http_status=r.status_code,
                    latency_ms=int(latency_ms),
                    response_body_hash=resp_hash,
                    error=redact(err_json) if isinstance(err_json, dict) else err_msg,
                    retryable=bool(r.status_code in {408, 429, 500, 502, 503, 504}),
                    safe_excerpt=(err_msg or "")[:500] if err_msg else None,
                    openai_request_id=openai_request_id,
                    openai_request_id_client=req_id_client,
                )
            return r, latency_ms, resp_hash, openai_request_id, req_id_client
        except requests.RequestException as exc:
            latency_ms = (time.perf_counter() - start) * 1000.0
            resp = getattr(exc, "response", None)
            raw_body = resp.content if resp is not None else b""
            resp_hash = _sha256_bytes(raw_body) if raw_body else None
            status = getattr(resp, "status_code", None)
            err_msg = str(exc)
            log_event(
                log,
                "openai.error",
                "OpenAI request exception",
                http_status=status,
                latency_ms=int(latency_ms),
                response_body_hash=resp_hash,
                retryable=True,
                safe_excerpt=(err_msg or "")[:500],
                openai_request_id=getattr(resp, "headers", {}).get("x-request-id") if resp is not None else None,
                openai_request_id_client=req_id_client,
            )
            raise


def _is_retryable_http_status(status_code: int | None) -> bool:
    return bool(status_code in _RETRYABLE_HTTP_STATUSES)


def _openai_post_with_retry(
    payload: Dict[str, Any],
    *,
    timeout: int,
    log,
    mode: str,
    api_key: str,
    attempt_start: int = 1,
    max_retries: int = _MAX_HTTP_RETRIES,
) -> Tuple[requests.Response, float, Optional[str], Optional[str], str, int]:
    attempt = attempt_start
    while True:
        try:
            resp, latency_ms, resp_hash, openai_request_id, req_id_client = _openai_post_responses(
                payload,
                timeout=timeout,
                log=log,
                mode=mode,
                attempt=attempt,
                api_key=api_key,
            )
        except requests.RequestException:
            if attempt - attempt_start >= max_retries:
                raise
            backoff = _RETRY_BASE_DELAY_SEC * (2 ** (attempt - attempt_start))
            log_event(
                log,
                "openai.retry",
                "OpenAI retry after request exception",
                attempt_from=attempt,
                attempt_to=attempt + 1,
                reason="request_exception",
                backoff_ms=int(backoff * 1000),
            )
            time.sleep(backoff)
            attempt += 1
            continue

        if _is_retryable_http_status(resp.status_code) and (attempt - attempt_start) < max_retries:
            backoff = _RETRY_BASE_DELAY_SEC * (2 ** (attempt - attempt_start))
            log_event(
                log,
                "openai.retry",
                "OpenAI retry on retryable HTTP status",
                attempt_from=attempt,
                attempt_to=attempt + 1,
                reason=f"http_{resp.status_code}",
                backoff_ms=int(backoff * 1000),
            )
            time.sleep(backoff)
            attempt += 1
            continue
        return resp, latency_ms, resp_hash, openai_request_id, req_id_client, attempt


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

def _run_responses_flow(
    cfg: OpenAIConfig,
    ocr_text: str,
    images: Optional[Sequence[Tuple[str, bytes]]],
    pdf: Optional[Tuple[str, bytes]],
    timeout: int,
    mode: str,
) -> Tuple[Optional[Dict[str, Any]], str, str]:
    log = logging.getLogger(__name__)
    if cfg.use_json_schema:
        validate_schema_invariants_or_raise(_OPENAI_JSON_SCHEMA, log=log)
    model = _resolve_model(cfg.api_key, cfg.model if mode == "primary" else (cfg.fallback_model or cfg.model), _MODEL_PREFER_PRIMARY if mode == "primary" else _MODEL_PREFER_FALLBACK)
    prompt = _build_prompt(ocr_text, mode=mode)

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

    payload: Dict[str, Any] = {
        "model": model,
        "input": [{"role": "user", "content": content}],
        "text": {"format": _build_text_format(bool(cfg.use_json_schema))},
        "temperature": float(cfg.temperature or 0.0),
        "max_output_tokens": int(cfg.max_output_tokens or 2000),
    }

    attempt = 1
    resp, latency_ms, resp_hash, openai_request_id, req_id_client, attempt = _openai_post_with_retry(
        payload, timeout=timeout, log=log, mode=mode, api_key=cfg.api_key, attempt_start=attempt
    )
    if resp.status_code >= 400 and cfg.use_json_schema:
        err_body = {}
        try:
            err_body = resp.json()
        except Exception:
            err_body = {}
        err_code = None
        err_type = None
        if isinstance(err_body, dict):
            err_code = err_body.get("error", {}).get("code")
            err_type = err_body.get("error", {}).get("type")
        if err_code == "invalid_json_schema" or err_type == "invalid_json_schema":
            error_message = None
            if isinstance(err_body, dict):
                error_message = err_body.get("error", {}).get("message")
            schema_path_hint = _extract_schema_path_hint(error_message)
            log_event(
                log,
                "openai.error",
                "OpenAI invalid_json_schema – no fallback",
                http_status=resp.status_code,
                reason="invalid_json_schema",
                schema_hash=_JSON_SCHEMA_HASH,
                schema_path_hint=schema_path_hint,
                openai_request_id=openai_request_id,
                openai_request_id_client=req_id_client,
            )
            return None, resp.text, model
        else:
            log_event(
                log,
                "openai.retry",
                "OpenAI retry with json_object",
                attempt_from=attempt,
                attempt_to=attempt + 1,
                reason="schema_or_other_400",
                backoff_ms=0,
                mutated_params="format json_schema -> json_object",
            )
            payload["text"] = {"format": {"type": "json_object"}}
            attempt += 1
            resp, latency_ms, resp_hash, openai_request_id, req_id_client, attempt = _openai_post_with_retry(
                payload, timeout=timeout, log=log, mode=mode, api_key=cfg.api_key, attempt_start=attempt
            )
    resp.raise_for_status()

    data = resp.json()
    text = _extract_output_text(data)
    if not text:
        text = str(data)
    len_raw = len(text or "")

    start = text.find("{")
    end = text.rfind("}")
    obj = None
    parse_status = "fail"
    parse_error = None
    if start != -1 and end != -1 and end > start:
        fragment = text[start:end + 1]
        try:
            obj = json.loads(fragment)
            parse_status = "ok"
        except Exception as e:
            parse_error = str(e)
    else:
        parse_error = "no_braces"

    log_event(
        log,
        "structured_output.parse",
        "Structured output parse",
        status=parse_status,
        strategy="first_last_brace",
        start=start,
        end=end,
        length=len_raw,
        error=parse_error,
        json_extract_strategy="first_last_brace",
    )

    validation_errors: List[str] = []
    if isinstance(obj, dict) and cfg.use_json_schema:
        validation_errors = _validate_against_schema(obj, _OPENAI_JSON_SCHEMA)
        log_event(
            log,
            "structured_output.validate",
            "Structured output validate",
            status="pass" if not validation_errors else "fail",
            invalid_paths=validation_errors[:50],
        )

    if isinstance(obj, dict):
        obj = ensure_schema_defaults(obj)
    obj_norm = _normalize_extracted_payload(obj) if isinstance(obj, dict) else None
    log_event(
        log,
        "openai.response",
        "OpenAI response",
        http_status=resp.status_code,
        latency_ms=int(latency_ms),
        response_body_hash=resp_hash,
        openai_request_id=openai_request_id,
        openai_request_id_client=req_id_client,
        extracted_output_text_length=len(text or ""),
        len_raw=len_raw,
        usage=data.get("usage"),
        parse_status=parse_status,
    )
    return obj_norm, text, model


def extract_with_openai(
    cfg: OpenAIConfig,
    ocr_text: str,
    images: Optional[Sequence[Tuple[str, bytes]]] = None,
    pdf: Optional[Tuple[str, bytes]] = None,
    timeout: int = 40,
) -> Tuple[Optional[Dict[str, Any]], str, str]:
    return _run_responses_flow(cfg, ocr_text, images, pdf, timeout, mode="primary")


def extract_with_openai_fallback(
    cfg: OpenAIConfig,
    ocr_text: str,
    images: Optional[Sequence[Tuple[str, bytes]]] = None,
    pdf: Optional[Tuple[str, bytes]] = None,
    timeout: int = 40,
) -> Tuple[Optional[Dict[str, Any]], str, str]:
    return _run_responses_flow(cfg, ocr_text, images, pdf, timeout, mode="fallback")
