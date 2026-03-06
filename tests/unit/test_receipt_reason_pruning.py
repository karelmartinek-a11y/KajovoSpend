from __future__ import annotations

from kajovospend.service.processor import Processor


def test_prune_receipt_reasons_handles_diacritics() -> None:
    p = Processor.__new__(Processor)
    out = p._prune_receipt_reasons(
        [
            "nekompletní vytěžení",
            "Nekompletni vytezeni",
            "chybí položky",
        ]
    )
    assert out == ["chybí položky"]
