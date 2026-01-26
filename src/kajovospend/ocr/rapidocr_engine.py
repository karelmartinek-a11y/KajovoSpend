from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple
import numpy as np
from PIL import Image, ImageOps, ImageFilter
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
    def _preprocess_variants(self, image: Image.Image) -> List[Image.Image]:
        """Vytvoří několik variant obrázku pro robustnější OCR (slabý kontrast, malý doklad uprostřed stránky)."""
        variants: List[Image.Image] = []
        try:
            base = image.convert("RGB")
            variants.append(base)
            # 1) Autocrop "obsahu" (odstraní velké bílé okraje u skenů)
            g = ImageOps.grayscale(base)
            g2 = ImageOps.autocontrast(g)
            arr = np.array(g2)
            # mask: všechno, co není téměř bílé
            thr = int(np.percentile(arr, 98))
            mask = arr < max(200, thr - 5)
            if mask.any():
                ys, xs = np.where(mask)
                y0, y1 = int(ys.min()), int(ys.max())
                x0, x1 = int(xs.min()), int(xs.max())
                pad = int(0.03 * max(arr.shape[0], arr.shape[1])) + 10
                y0 = max(0, y0 - pad)
                x0 = max(0, x0 - pad)
                y1 = min(arr.shape[0] - 1, y1 + pad)
                x1 = min(arr.shape[1] - 1, x1 + pad)
                cropped = base.crop((x0, y0, x1 + 1, y1 + 1))
            else:
                cropped = base
            variants.append(cropped)
            # 2) Kontrast + doostření + upscaling
            for src in [cropped]:
                gg = ImageOps.grayscale(src)
                gg = ImageOps.autocontrast(gg)
                # upscale (malé účtenky)
                scale = 2
                if max(gg.size) < 1400:
                    scale = 3
                up = gg.resize((gg.size[0] * scale, gg.size[1] * scale), Image.Resampling.LANCZOS)
                up = up.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
                # 3) Otsu binarizace (pomáhá u vybledlých skenů)
                a = np.array(up)
                # histogram 256
                hist = np.bincount(a.flatten(), minlength=256).astype(np.float64)
                total = a.size
                sum_total = np.dot(np.arange(256), hist)
                sum_b, w_b, w_f, var_max, thr_otsu = 0.0, 0.0, 0.0, 0.0, 0
                for i in range(256):
                    w_b += hist[i]
                    if w_b == 0:
                        continue
                    w_f = total - w_b
                    if w_f == 0:
                        break
                    sum_b += i * hist[i]
                    m_b = sum_b / w_b
                    m_f = (sum_total - sum_b) / w_f
                    var_between = w_b * w_f * (m_b - m_f) ** 2
                    if var_between > var_max:
                        var_max = var_between
                        thr_otsu = i
                bw = (a > thr_otsu).astype(np.uint8) * 255
                bw_img = Image.fromarray(bw, mode="L")
                variants.append(bw_img.convert("RGB"))
        except Exception:
            # fallback: at least base
            return [image.convert("RGB")]
        # dedupe by size/mode quickly
        out: List[Image.Image] = []
        seen = set()
        for v in variants:
            key = (v.size, v.mode)
            if key in seen:
                continue
            seen.add(key)
            out.append(v)
        return out
    def _score_text(self, text: str, conf: float) -> float:
        # prefer higher confidence and more content (but not too aggressively)
        ln = len((text or "").strip())
        return float(conf or 0.0) * (1.0 + min(2.0, np.log1p(max(0, ln)) / 3.0))
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
        if self._engine is None:
            return "", 0.0
        best_text = ""
        best_conf = 0.0
        best_score = -1.0
        for variant in self._preprocess_variants(image):
            lines = self.image_to_lines(variant)
            if not lines:
                continue
            avg = sum(l.confidence for l in lines) / len(lines)
            text = "\n".join(l.text for l in lines)
            score = self._score_text(text, float(avg))
            if score > best_score:
                best_score = score
                best_text = text
                best_conf = float(avg)
        if not best_text:
            return "", 0.0
        return best_text, float(best_conf)
