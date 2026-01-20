from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

import numpy as np
from PIL import Image

try:
    from rapidocr_onnxruntime import RapidOCR
except Exception:  # pragma: no cover
    RapidOCR = None  # type: ignore


@dataclass
class OcrLine:
    text: str
    confidence: float


class RapidOcrEngine:
    def __init__(self, models_dir: Path | None = None):
        # Do NOT hard-fail the whole app if OCR runtime isn't available.
        # Service will quarantine docs instead.
        self._engine = None
        if RapidOCR is None:
            return
        kwargs = {}
        if models_dir and models_dir.exists():
            # Deterministic offline pinning (if present).
            det = models_dir / "ch_ppocr_server_v2.0_det_infer.onnx"
            rec = models_dir / "ch_ppocr_server_v2.0_rec_infer.onnx"
            cls = models_dir / "ch_ppocr_mobile_v2.0_cls_infer.onnx"
            keys = models_dir / "ppocr_keys_v1.txt"
            kwargs["det_model_path"] = str(det) if det.exists() else None
            kwargs["rec_model_path"] = str(rec) if rec.exists() else None
            kwargs["cls_model_path"] = str(cls) if cls.exists() else None
            # Some RapidOCR builds accept rec_char_dict_path; safe to pass only if exists.
            kwargs["rec_char_dict_path"] = str(keys) if keys.exists() else None
            kwargs = {k: v for k, v in kwargs.items() if v}
        self._engine = RapidOCR(**kwargs)

    def is_available(self) -> bool:
        return self._engine is not None

    def image_to_lines(self, image: Image.Image) -> List[OcrLine]:
        if self._engine is None:
            return []
        arr = np.array(image.convert("RGB"))
        result, _ = self._engine(arr)
        lines: List[OcrLine] = []
        if not result:
            return lines
        for item in result:
            # item: [box, text, score]
            try:
                text = str(item[1]).strip()
                score = float(item[2])
            except Exception:
                continue
            if text:
                lines.append(OcrLine(text=text, confidence=score))
        return lines

    def image_to_text(self, image: Image.Image) -> Tuple[str, float]:
        lines = self.image_to_lines(image)
        if not lines:
            return "", 0.0
        avg = sum(l.confidence for l in lines) / len(lines)
        return "\n".join(l.text for l in lines), float(avg)
