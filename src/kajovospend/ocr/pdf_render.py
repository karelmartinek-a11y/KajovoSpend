from __future__ import annotations

from pathlib import Path
from typing import List

import numpy as np
from PIL import Image
import pypdfium2 as pdfium


def render_pdf_to_images(
    pdf_path: Path,
    dpi: int = 200,
    max_pages: int | None = None,
    *,
    start_page: int = 0,
) -> List[Image.Image]:
    pdf = pdfium.PdfDocument(str(pdf_path))
    n = len(pdf)
    start = max(0, int(start_page or 0))
    if start >= n:
        return []
    if max_pages is not None:
        n = min(n, start + int(max_pages))
    scale = dpi / 72.0
    images: List[Image.Image] = []
    for i in range(start, n):
        page = pdf[i]
        bitmap = page.render(scale=scale)
        arr = bitmap.to_numpy()  # BGRA
        if arr.shape[2] == 4:
            arr = arr[:, :, :3]
        img = Image.fromarray(arr.astype(np.uint8), mode="RGB")
        images.append(img)
    return images
