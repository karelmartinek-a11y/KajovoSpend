from __future__ import annotations

from typing import Tuple

try:
    import pytesseract
except Exception:  # pragma: no cover
    pytesseract = None  # type: ignore


class TesseractHandwritingEngine:
    """Minimální offline handwriting OCR backend (pytesseract)."""

    def __init__(self, *, lang: str = "ces", psm: int = 6, oem: int = 1):
        self.lang = str(lang or "ces")
        self.psm = int(psm)
        self.oem = int(oem)

    def is_available(self) -> bool:
        if pytesseract is None:
            return False
        try:
            _ = pytesseract.get_tesseract_version()
            return True
        except Exception:
            return False

    def image_to_text(self, image) -> Tuple[str, float]:
        if not self.is_available():
            return "", 0.0
        cfg = f"--oem {self.oem} --psm {self.psm}"
        txt = pytesseract.image_to_string(image, lang=self.lang, config=cfg) or ""
        # pytesseract typicky nedává stabilní conf pro celý text; použijeme konzervativní pseudo-conf.
        conf = 0.55 if txt.strip() else 0.0
        return txt, float(conf)
