from __future__ import annotations

from kajovospend.ocr.handwriting_tesseract import TesseractHandwritingEngine


def test_tesseract_engine_is_available_returns_bool() -> None:
    eng = TesseractHandwritingEngine(lang="ces")
    assert isinstance(eng.is_available(), bool)
