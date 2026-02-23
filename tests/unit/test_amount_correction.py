from __future__ import annotations

from kajovospend.utils.amount_correction import (
    normalize_ocr_amount_token,
    generate_decimal_candidates,
    parse_amount_candidates,
    validate_candidates_against_invariant,
    choose_best_candidate,
)


def test_normalize_ocr_amount_token_replaces_common_chars() -> None:
    out, changed = normalize_ocr_amount_token("1O,S0")
    assert changed is True
    assert out == "10,50"


def test_generate_decimal_candidates_from_digits() -> None:
    cands = generate_decimal_candidates("1O5O")
    assert "10,50" in cands
    assert "10.50" in cands


def test_parse_amount_candidates_returns_numeric_values() -> None:
    vals = parse_amount_candidates("12B0")
    assert 12.8 in vals


def test_validate_candidates_against_invariant_filters() -> None:
    vals = [99.0, 100.0, 101.0]
    valid = validate_candidates_against_invariant(vals, validator=lambda x: abs(x - 100.0) <= 0.01)
    assert valid == [100.0]


def test_choose_best_candidate_uses_original_guess() -> None:
    best = choose_best_candidate([100.0, 121.0, 99.0], original_guess=120.0)
    assert best == 121.0
