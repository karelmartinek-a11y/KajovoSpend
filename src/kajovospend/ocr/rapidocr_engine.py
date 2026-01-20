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
        if RapidOCR is None:
            raise RuntimeError("rapidocr-onnxruntime is not installed")
        kwargs = {}
        if models_dir and models_dir.exists():
            # RapidOCR expects model paths; keep it simple: default internal models if not pinned.
            # If pinned models exist, user can still rely on default behavior.
            kwargs["det_model_path"] = str(models_dir / "ch_ppocr_server_v2.0_det_infer.onnx") if (models_dir / "ch_ppocr_server_v2.0_det_infer.onnx").exists() else None
            kwargs["rec_model_path"] = str(models_dir / "ch_ppocr_server_v2.0_rec_infer.onnx") if (models_dir / "ch_ppocr_server_v2.0_rec_infer.onnx").exists() else None
            kwargs["cls_model_path"] = str(models_dir / "ch_ppocr_mobile_v2.0_cls_infer.onnx") if (models_dir / "ch_ppocr_mobile_v2.0_cls_infer.onnx").exists() else None
            kwargs = {k: v for k, v in kwargs.items() if v}
        self._engine = RapidOCR(**kwargs)

    def image_to_lines(self, image: Image.Image) -> List[OcrLine]:
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
