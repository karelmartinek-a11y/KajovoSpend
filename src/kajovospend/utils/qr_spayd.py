from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Dict

# SPAYD format: 'SPD*1.0*ACC:CZ...*AM:123.45*CC:CZK*X-VS:123...'
# https://qr-platba.cz/pro-vyvojare/ (format is commonly used in CZ)
_SPD_PREFIX = "SPD*"

@dataclass
class SpaydPayment:
    account: Optional[str] = None  # IBAN
    amount: Optional[float] = None
    currency: Optional[str] = None
    vs: Optional[str] = None
    ss: Optional[str] = None
    ks: Optional[str] = None
    message: Optional[str] = None
    date: Optional[str] = None  # YYYYMMDD sometimes

def parse_spayd(payload: str) -> Optional[SpaydPayment]:
    if not payload:
        return None
    payload = payload.strip()
    if not payload.startswith(_SPD_PREFIX):
        return None
    parts = payload.split("*")
    # parts[0] == SPD, parts[1] version
    kv: Dict[str, str] = {}
    for p in parts[2:]:
        if ":" not in p:
            continue
        k, v = p.split(":", 1)
        kv[k.strip().upper()] = v.strip()

    sp = SpaydPayment()
    # Account can be 'ACC:CZ...'
    acc = kv.get("ACC")
    if acc:
        # Sometimes multiple accounts separated by comma; take first
        sp.account = acc.split(",")[0].strip()
    am = kv.get("AM")
    if am:
        try:
            sp.amount = float(am.replace(",", "."))
        except Exception:
            pass
    cc = kv.get("CC")
    if cc:
        sp.currency = cc.strip().upper()
    sp.vs = kv.get("X-VS")
    sp.ss = kv.get("X-SS")
    sp.ks = kv.get("X-KS")
    sp.message = kv.get("MSG") or kv.get("RN")  # RN = recipient name (rare), MSG common
    sp.date = kv.get("DT")
    return sp

def decode_qr_from_pil(image) -> Optional[str]:
    """Best-effort QR decode from PIL.Image using zxing-cpp if available."""
    try:
        import numpy as np
        import zxingcpp
    except Exception:
        return None

    try:
        import PIL.Image
        if isinstance(image, PIL.Image.Image):
            img = image
        else:
            return None
        # zxing expects numpy array
        arr = np.array(img.convert("RGB"))
        results = zxingcpp.read_barcodes(arr)
        for r in results:
            if getattr(r, "text", None):
                return r.text
    except Exception:
        return None
    return None
