from __future__ import annotations

import json
import base64
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Sequence

import requests


@dataclass
class OpenAIConfig:
    api_key: str
    model: str


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


_SCHEMA = {
    "supplier_ico": "string",
    "doc_number": "string",
    "bank_account": "string",
    "issue_date": "YYYY-MM-DD",
    "total_with_vat": "number",
    "currency": "string",
    "items": [
        {"name": "string", "quantity": "number", "vat_rate": "number", "line_total": "number"}
    ],
}


def _b64_data_url(mime: str, data: bytes) -> str:
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def extract_with_openai(
    cfg: OpenAIConfig,
    ocr_text: str,
    images: Optional[Sequence[Tuple[str, bytes]]] = None,
    timeout: int = 40,
) -> Tuple[Optional[Dict[str, Any]], str]:
    # Responses API supports multimodal input items (input_text + input_image).
    # image_url může být i base64 data URL.
    # Zapneme JSON mode přes text.format json_object.
    prompt = (
        "Jsi extrakční systém pro české doklady (faktury, účtenky). "
        "Vrať POUZE validní JSON objekt (bez markdownu, bez komentářů). "
        "Schéma JSON je: "
        + json.dumps(_SCHEMA, ensure_ascii=False)
        + "\n\nOCR text dokladu:\n"
        + (ocr_text or "")
    )

    content: List[Dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    if images:
        for mime, data in images:
            if not data:
                continue
            mm = mime if mime in ("image/png", "image/jpeg", "image/webp") else "image/png"
            content.append({"type": "input_image", "image_url": _b64_data_url(mm, data)})

    payload = {
        "model": cfg.model,
        "input": [{"role": "user", "content": content}],
        "text": {"format": {"type": "json_object"}},
    }

    r = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
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
                return obj, text
    except Exception:
        return None, text
    return None, text
