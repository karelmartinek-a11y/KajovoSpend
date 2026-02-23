from __future__ import annotations

from typing import Any, Protocol, Tuple


class TextOcrEngine(Protocol):
    """Společné rozhraní OCR backendů (MVP)."""

    def is_available(self) -> bool:
        ...

    def image_to_text(self, image: Any) -> Tuple[str, float]:
        ...
