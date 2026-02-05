from __future__ import annotations

from typing import Optional, Tuple

from PIL import Image

def deskew_pil(image: Image.Image) -> Image.Image:
    """Best-effort deskew using OpenCV if installed. If OpenCV is missing or deskew fails, returns input."""
    try:
        import cv2
        import numpy as np
    except Exception:
        return image

    try:
        # convert to grayscale
        rgb = image.convert("RGB")
        arr = np.array(rgb)
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

        # use Otsu to find text pixels
        _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        coords = cv2.findNonZero(bw)
        if coords is None:
            return image
        rect = cv2.minAreaRect(coords)
        angle = rect[-1]
        # minAreaRect angle is in [-90, 0); adjust
        if angle < -45:
            angle = 90 + angle
        # ignore tiny angles
        if abs(angle) < 0.4:
            return image

        (h, w) = gray.shape[:2]
        center = (w // 2, h // 2)
        M = cv2.getRotationMatrix2D(center, angle, 1.0)
        rotated = cv2.warpAffine(arr, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
        return Image.fromarray(rotated)
    except Exception:
        return image
