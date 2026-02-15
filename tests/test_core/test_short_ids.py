"""Tests for short ID validation and parsing in core/ids.py."""

from __future__ import annotations

import pytest

from lattice.core.ids import is_short_id, parse_short_id, validate_short_id


class TestValidateShortId:
    def test_valid_simple(self) -> None:
        assert validate_short_id("LAT-1") is True

    def test_valid_multi_digit(self) -> None:
        assert validate_short_id("LAT-42") is True

    def test_valid_large_number(self) -> None:
        assert validate_short_id("LAT-99999") is True

    def test_valid_single_letter_prefix(self) -> None:
        assert validate_short_id("X-1") is True

    def test_valid_five_letter_prefix(self) -> None:
        assert validate_short_id("ABCDE-1") is True

    def test_invalid_lowercase(self) -> None:
        assert validate_short_id("lat-1") is False

    def test_invalid_no_number(self) -> None:
        assert validate_short_id("LAT-") is False

    def test_invalid_no_dash(self) -> None:
        assert validate_short_id("LAT1") is False

    def test_invalid_six_letter_prefix(self) -> None:
        assert validate_short_id("ABCDEF-1") is False

    def test_invalid_ulid(self) -> None:
        assert validate_short_id("task_01ABC") is False

    def test_invalid_empty(self) -> None:
        assert validate_short_id("") is False

    def test_invalid_digits_in_prefix(self) -> None:
        assert validate_short_id("L1T-1") is False


class TestParseShortId:
    def test_parse_simple(self) -> None:
        prefix, num = parse_short_id("LAT-42")
        assert prefix == "LAT"
        assert num == 42

    def test_parse_case_insensitive(self) -> None:
        prefix, num = parse_short_id("lat-7")
        assert prefix == "LAT"
        assert num == 7

    def test_parse_single_letter(self) -> None:
        prefix, num = parse_short_id("X-1")
        assert prefix == "X"
        assert num == 1

    def test_parse_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid short ID"):
            parse_short_id("not-a-short-id")


class TestIsShortId:
    def test_valid(self) -> None:
        assert is_short_id("LAT-42") is True

    def test_valid_lowercase(self) -> None:
        assert is_short_id("lat-42") is True

    def test_ulid_not_short(self) -> None:
        assert is_short_id("task_01ABC") is False

    def test_empty_not_short(self) -> None:
        assert is_short_id("") is False
