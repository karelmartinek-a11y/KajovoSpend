from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Tuple, Optional, Sequence

import numpy as np
from PIL import Image, ImageOps, ImageFilter

from kajovospend.ocr.geometry import deskew_pil

try:
    from rapidocr_onnxruntime import RapidOCR
except Exception:  # pragma: no cover
    RapidOCR = None  # type: ignore


@dataclass
class OcrLine:
    text: str
    confidence: float


@dataclass
class OcrItem:
    # 4-point polygon [[x,y],...]
    box: List[List[float]]
    text: str
    confidence: float


class RapidOcrEngine:
    def __init__(self, models_dir: Path | None = None):
        # Do NOT hard-fail the whole app if OCR runtime isn't available.
        # Service will quarantine docs instead.
        self._engine = None
        if RapidOCR is None:
            return

        kwargs: dict[str, Any] = {}
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

            # 0.5) Deskew (narovnání náklonu) – pokud je dostupné OpenCV
            try:
                variants.append(deskew_pil(base))
            except Exception:
                pass

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

            # 1b) Pokud jsou na jedné stránce 2 doklady vedle sebe (např. 2 účtenky), zkus split vlevo/vpravo.
            try:
                cg = ImageOps.grayscale(cropped)
                cg = ImageOps.autocontrast(cg)
                carr = np.array(cg)
                # non-white mask
                m2 = carr < 245
                if m2.any() and carr.shape[1] >= 1200:
                    proj = m2.sum(axis=0).astype(np.float64)
                    mx = float(proj.max() or 0.0)
                    if mx > 0:
                        # hledej minimum v centrálním pásmu
                        w = carr.shape[1]
                        a = int(0.35 * w)
                        b = int(0.65 * w)
                        center = proj[a:b]
                        if center.size > 0:
                            min_idx = int(center.argmin()) + a
                            # valley threshold: skoro prázdný sloupec
                            if proj[min_idx] <= 0.02 * mx:
                                # ověř, že okolo minima je souvislá "bílá mezera"
                                span = int(0.03 * w) + 10
                                left = max(0, min_idx - span)
                                right = min(w - 1, min_idx + span)
                                if float(proj[left:right].max() or 0.0) <= 0.06 * mx:
                                    # vytvoř dvě varianty cropu
                                    if min_idx > 300 and (w - min_idx) > 300:
                                        left_img = cropped.crop((0, 0, min_idx, cropped.size[1]))
                                        right_img = cropped.crop((min_idx, 0, cropped.size[0], cropped.size[1]))
                                        variants.append(left_img)
                                        variants.append(right_img)
            except Exception:
                pass

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
                sum_b, w_b, var_max, thr_otsu = 0.0, 0.0, 0.0, 0
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

    def image_to_items(self, image: Image.Image) -> List[OcrItem]:
        """Vrátí OCR výsledky včetně bboxů (pro layout-aware rekonstrukci řádků)."""
        if self._engine is None:
            return []
        arr = np.array(image.convert("RGB"))
        result, _ = self._engine(arr)
        items: List[OcrItem] = []
        if not result:
            return items
        for it in result:
            try:
                box = it[0]
                text = str(it[1]).strip()
                score = float(it[2])
            except Exception:
                continue
            if not text:
                continue
            # normalize box to list[list[float]]
            try:
                box_ll = [[float(p[0]), float(p[1])] for p in box]
            except Exception:
                box_ll = []
            items.append(OcrItem(box=box_ll, text=text, confidence=score))
        return items

    def image_to_lines(self, image: Image.Image) -> List[OcrLine]:
        items = self.image_to_items(image)
        return [OcrLine(text=i.text, confidence=float(i.confidence)) for i in items]

    @staticmethod
    def _items_to_reconstructed_text(items: List[OcrItem]) -> str:
        """Rekonstrukce čtecího pořadí: shlukování podle Y, řazení podle X.
        Výrazně zlepšuje kvalitu textu u tabulek a účtenek, kde OCR vrací segmenty mimo pořadí.
        """
        if not items:
            return ""
        # compute centers and heights
        rows: List[Tuple[float, float, float, float, str]] = []
        for it in items:
            if not it.box or len(it.box) < 4:
                continue
            xs = [p[0] for p in it.box]
            ys = [p[1] for p in it.box]
            x0, x1 = float(min(xs)), float(max(xs))
            y0, y1 = float(min(ys)), float(max(ys))
            yc = (y0 + y1) / 2.0
            h = max(1.0, y1 - y0)
            rows.append((yc, x0, x1, h, it.text))
        if not rows:
            return "\n".join(it.text for it in items)

        median_h = float(np.median([r[3] for r in rows]) or 12.0)
        # group into line buckets by y center
        rows.sort(key=lambda r: r[0])
        buckets: List[List[Tuple[float, float, float, float, str]]] = []
        tol = max(6.0, 0.55 * median_h)
        for r in rows:
            if not buckets:
                buckets.append([r])
                continue
            if abs(r[0] - buckets[-1][0][0]) <= tol:
                buckets[-1].append(r)
            else:
                buckets.append([r])

        out_lines: List[str] = []
        for b in buckets:
            b.sort(key=lambda r: r[1])
            parts: List[str] = []
            last_x1: Optional[float] = None
            for yc, x0, x1, h, text in b:
                if last_x1 is None:
                    parts.append(text)
                    last_x1 = x1
                    continue
                # insert space if there's a gap
                gap = x0 - last_x1
                if gap > 0.9 * median_h:
                    parts.append(text)
                else:
                    # glue with space (avoid crushing tokens)
                    parts.append(text)
                last_x1 = max(last_x1, x1)
            out_lines.append(" ".join(parts).strip())
        return "\n".join([ln for ln in out_lines if ln])

    def image_to_text_candidates(
        self,
        image: Image.Image,
        *,
        rotations: Sequence[int] = (0, 90, 180, 270),
        include_reconstructed: bool = True,
        max_candidates: int = 6,
    ) -> List[Tuple[str, float, dict]]:
        """Vrací více kandidátů textu (různé předzpracování + rotace + rekonstrukce řádků)."""
        if self._engine is None:
            return []
        candidates: List[Tuple[str, float, dict]] = []

        for rot in rotations:
            img = image
            if rot:
                img = image.rotate(rot, expand=True)
            for v_idx, variant in enumerate(self._preprocess_variants(img)):
                items = self.image_to_items(variant)
                if not items:
                    continue
                avg = float(sum(i.confidence for i in items) / max(1, len(items)))
                text_plain = "\n".join(i.text for i in items).strip()
                if text_plain:
                    candidates.append((text_plain, avg, {"rotation": rot, "variant": v_idx, "mode": "plain"}))
                if include_reconstructed:
                    text_rec = self._items_to_reconstructed_text(items).strip()
                    if text_rec and text_rec != text_plain:
                        candidates.append((text_rec, avg, {"rotation": rot, "variant": v_idx, "mode": "reconstructed"}))

        # rank candidates
        def _rank(c):
            text, conf, meta = c
            return self._score_text(text, conf)

        candidates.sort(key=_rank, reverse=True)
        # de-dup by normalized text prefix (avoid wasting downstream)
        out: List[Tuple[str, float, dict]] = []
        seen = set()
        for t, c, m in candidates:
            key = (t.strip()[:200], int(m.get("rotation", 0)), m.get("mode"))
            if key in seen:
                continue
            seen.add(key)
            out.append((t, c, m))
            if len(out) >= int(max_candidates):
                break
        return out

    def image_to_text(self, image: Image.Image) -> Tuple[str, float]:
        """Backward-compatible: vrací nejlepší kandidát (plain) podle interního skóre."""
        if self._engine is None:
            return "", 0.0
        best_text = ""
        best_conf = 0.0
        best_score = -1.0
        for text, conf, _meta in self.image_to_text_candidates(image, include_reconstructed=False, max_candidates=8):
            score = self._score_text(text, float(conf))
            if score > best_score:
                best_score = score
                best_text = text
                best_conf = float(conf)
        if not best_text:
            return "", 0.0
        return best_text, float(best_conf)
