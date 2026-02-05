from __future__ import annotations

import re

_IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")

def normalize_iban(s: str) -> str:
    """Normalize IBAN-like string: remove spaces, upper-case."""
    return re.sub(r"\s+", "", (s or "")).upper()

def is_valid_iban(iban: str) -> bool:
    """Offline IBAN checksum validation (ISO 13616 / mod-97)."""
    iban = normalize_iban(iban)
    if not iban or not _IBAN_RE.match(iban):
        return False
    # Move first 4 chars to the end
    rearranged = iban[4:] + iban[:4]
    # Convert letters to numbers: A=10..Z=35
    digits = []
    for ch in rearranged:
        if ch.isdigit():
            digits.append(ch)
        elif "A" <= ch <= "Z":
            digits.append(str(ord(ch) - 55))
        else:
            return False
    num = "".join(digits)
    # mod 97 in chunks to avoid huge ints
    rem = 0
    for i in range(0, len(num), 9):
        rem = int(str(rem) + num[i:i+9]) % 97
    return rem == 1
