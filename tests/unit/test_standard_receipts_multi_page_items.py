from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from kajovospend.extract.standard_receipts import extract_using_template


class _FakeOcr:
    def image_to_text(self, image):
        page_marker = image.getpixel((0, 0))[0]
        mapping = {
            1: "ITEMS_PAGE_1",
            2: "ITEMS_PAGE_2",
        }
        return mapping.get(page_marker, ""), 0.9


class TestStandardReceiptItemsAcrossPages(unittest.TestCase):
    def test_items_region_can_span_two_pages(self) -> None:
        schema = {
            "version": 1,
            "fields": {
                "supplier_ico": {"page": 1, "box": [0.0, 0.0, 1.0, 1.0]},
                "doc_number": {"page": 1, "box": [0.0, 0.0, 1.0, 1.0]},
                "issue_date": {"page": 1, "box": [0.0, 0.0, 1.0, 1.0]},
                "total_with_vat": {"page": 1, "box": [0.0, 0.0, 1.0, 1.0]},
                "items_region": {"page": 1, "box": [0.0, 0.0, 1.0, 1.0]},
                "items_region_page2": {"page": 2, "box": [0.0, 0.0, 1.0, 1.0]},
            },
        }
        template = SimpleNamespace(schema_json=json.dumps(schema))
        captured_items_text = {}

        def fake_render(_pdf_path, dpi, start_page, max_pages):
            page_no = int(start_page) + 1
            return [Image.new("RGB", (32, 32), (page_no, 0, 0))]

        def fake_extract_from_text(text):
            captured_items_text["text"] = text
            return SimpleNamespace(items=[{"name": "ok"}], review_reasons=[])

        def fake_postprocess_items_for_db(items, total_with_vat, reasons):
            return True, reasons, None, None, None

        with patch("kajovospend.ocr.pdf_render.render_pdf_to_images", new=fake_render), patch(
            "kajovospend.extract.parser.extract_from_text", new=fake_extract_from_text
        ), patch("kajovospend.extract.parser.postprocess_items_for_db", new=fake_postprocess_items_for_db):
            extracted = extract_using_template(
                pdf_path=Path("dummy.pdf"),
                template=template,
                ocr_engine=_FakeOcr(),
                cfg={},
                full_text="",
            )

        self.assertEqual(captured_items_text.get("text"), "ITEMS_PAGE_1\nITEMS_PAGE_2")
        self.assertEqual(extracted.items, [{"name": "ok"}])


if __name__ == "__main__":
    unittest.main()
