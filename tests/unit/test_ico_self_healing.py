from __future__ import annotations

import logging
import unittest
from unittest.mock import patch

from kajovospend.integrations.ares import AresError, AresRecord
from kajovospend.service.processor import Processor


class TestIcoSelfHealing(unittest.TestCase):
    def _proc(self) -> Processor:
        # Bypass __init__ (OCR init) – pro unit testy stačí log + cfg
        p = Processor.__new__(Processor)
        p.cfg = {}
        p.paths = None
        p.log = logging.getLogger("kajovospend_test_ico")
        return p

    def test_text_with_explicit_ico_label(self) -> None:
        p = self._proc()
        with patch("kajovospend.service.processor.fetch_by_ico") as fb:
            fb.return_value = AresRecord(ico="12345678", name="ACME")
            ico = p._guess_supplier_ico_from_text("Dodavatel ACME\nIČO: 12345678\nDIČ: CZ12345678")
            self.assertEqual(ico, "12345678")

    def test_text_without_ico_label_but_has_8_digits(self) -> None:
        p = self._proc()
        with patch("kajovospend.service.processor.fetch_by_ico") as fb:
            fb.return_value = AresRecord(ico="12345678", name="ACME")
            ico = p._guess_supplier_ico_from_text("ACME s.r.o.\nVodičkova 1\n12345678\nPraha\n")
            self.assertEqual(ico, "12345678")

    def test_multiple_candidates_choose_valid(self) -> None:
        p = self._proc()

        def _fake_fetch(ico: str, *args, **kwargs):
            if ico == "87654321":
                raise AresError("not found")
            if ico == "12345678":
                return AresRecord(ico="12345678", name="ACME")
            raise AresError("unexpected")

        with patch("kajovospend.service.processor.fetch_by_ico", side_effect=_fake_fetch):
            # 2 kandidáti, jen jeden validní v ARES
            txt = "Číslo dokladu: 87654321\nDodavatel: ACME s.r.o. 12345678\n"
            ico = p._guess_supplier_ico_from_text(txt)
            self.assertEqual(ico, "12345678")


if __name__ == "__main__":
    unittest.main()
