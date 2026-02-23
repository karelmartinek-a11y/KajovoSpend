from __future__ import annotations

from kajovospend.extract.layout_items import LayoutOcrItem, extract_items_from_ocr_layout


def _mk(x0: float, y0: float, x1: float, y1: float, text: str) -> LayoutOcrItem:
    return LayoutOcrItem(box=[[x0, y0], [x1, y0], [x1, y1], [x0, y1]], text=text, confidence=0.9)


def test_extract_single_row_with_explicit_vat_column() -> None:
    items = [
        _mk(10, 10, 120, 30, "Mléko"),
        _mk(140, 10, 170, 30, "2"),
        _mk(200, 10, 260, 30, "21"),
        _mk(300, 10, 380, 30, "24,20"),
    ]
    out = extract_items_from_ocr_layout(items)
    assert len(out) == 1
    assert out[0]["name"] == "Mléko"
    assert out[0]["quantity"] == 2.0
    assert out[0]["vat_rate"] == 21.0
    assert out[0]["line_total_gross"] == 24.2


def test_extract_two_rows_with_y_clustering_and_x_order() -> None:
    items = [
        _mk(300, 10, 380, 30, "12,10"),
        _mk(10, 10, 120, 30, "Pečivo"),
        _mk(140, 10, 170, 30, "1"),
        _mk(300, 50, 380, 70, "50,00"),
        _mk(10, 50, 120, 70, "Káva"),
        _mk(140, 50, 170, 70, "2"),
    ]
    out = extract_items_from_ocr_layout(items, document_text="DPH 21%")
    assert len(out) == 2
    assert out[0]["name"] == "Pečivo"
    assert out[1]["name"] == "Káva"


def test_fallback_vat_from_document_text_when_column_missing() -> None:
    items = [
        _mk(10, 10, 150, 30, "Služba"),
        _mk(180, 10, 230, 30, "1"),
        _mk(300, 10, 380, 30, "121,00"),
    ]
    out = extract_items_from_ocr_layout(items, document_text="Základ daně 100,00 DPH 21%")
    assert len(out) == 1
    assert out[0]["vat_rate"] == 21.0
    assert out[0]["line_total_net"] == 100.0


def test_filters_summary_rows() -> None:
    items = [
        _mk(10, 10, 180, 30, "Celkem"),
        _mk(300, 10, 380, 30, "121,00"),
        _mk(10, 50, 140, 70, "Položka"),
        _mk(300, 50, 380, 70, "121,00"),
    ]
    out = extract_items_from_ocr_layout(items)
    assert len(out) == 1
    assert out[0]["name"] == "Položka"


def test_uses_second_rightmost_amount_as_unit_gross() -> None:
    items = [
        _mk(10, 10, 140, 30, "Čaj"),
        _mk(160, 10, 190, 30, "2"),
        _mk(240, 10, 300, 30, "10,00"),
        _mk(320, 10, 390, 30, "20,00"),
    ]
    out = extract_items_from_ocr_layout(items)
    assert len(out) == 1
    assert out[0]["unit_price_gross"] == 10.0
    assert out[0]["line_total_gross"] == 20.0
