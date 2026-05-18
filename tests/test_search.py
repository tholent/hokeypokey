"""Tests for the HKP search query parser."""

from __future__ import annotations

import pytest

from hokeypokey.models import SearchType
from hokeypokey.search import parse_search

FP_40 = "A" * 40  # valid 40-char fingerprint hex


# ---------------------------------------------------------------------------
# Hex / key ID searches
# ---------------------------------------------------------------------------


def test_short_key_id():
    result = parse_search("0xABCD1234")
    assert result.search_type == SearchType.SHORT_KEY_ID
    assert result.normalized == "ABCD1234"
    assert result.raw == "0xABCD1234"


def test_short_key_id_uppercase_prefix():
    result = parse_search("0XABCD1234")
    assert result.search_type == SearchType.SHORT_KEY_ID
    assert result.normalized == "ABCD1234"


def test_long_key_id():
    result = parse_search("0xDEADBEEFDECAFBAD")
    assert result.search_type == SearchType.LONG_KEY_ID
    assert result.normalized == "DEADBEEFDECAFBAD"


def test_fingerprint_40_chars():
    result = parse_search("0x" + FP_40)
    assert result.search_type == SearchType.FINGERPRINT
    assert result.normalized == FP_40.upper()


def test_hex_normalized_to_uppercase():
    result = parse_search("0xdeadbeefdeadbeef")
    assert result.search_type == SearchType.LONG_KEY_ID
    assert result.normalized == "DEADBEEFDEADBEEF"


def test_invalid_hex_chars():
    with pytest.raises(ValueError, match="non-hexadecimal"):
        parse_search("0xZZZZ1234")


def test_invalid_hex_length_9():
    with pytest.raises(ValueError, match="9 hex characters"):
        parse_search("0x" + "A" * 9)


def test_invalid_hex_length_7():
    with pytest.raises(ValueError, match="7 hex characters"):
        parse_search("0x" + "A" * 7)


def test_invalid_hex_length_32():
    with pytest.raises(ValueError, match="32 hex characters"):
        parse_search("0x" + "A" * 32)


def test_empty_after_0x_prefix():
    with pytest.raises(ValueError, match="empty after"):
        parse_search("0x")


# ---------------------------------------------------------------------------
# Email searches
# ---------------------------------------------------------------------------


def test_email_search():
    result = parse_search("user@example.com")
    assert result.search_type == SearchType.EMAIL
    assert result.normalized == "user@example.com"


def test_email_normalized_to_lowercase():
    result = parse_search("User@EXAMPLE.COM")
    assert result.search_type == SearchType.EMAIL
    assert result.normalized == "user@example.com"


def test_email_with_plus():
    result = parse_search("user+tag@example.com")
    assert result.search_type == SearchType.EMAIL
    assert result.normalized == "user+tag@example.com"


# ---------------------------------------------------------------------------
# Text searches
# ---------------------------------------------------------------------------


def test_text_search():
    result = parse_search("John Doe")
    assert result.search_type == SearchType.TEXT
    assert result.normalized == "John Doe"
    assert result.raw == "John Doe"


def test_text_search_preserved_case():
    result = parse_search("Alice Smith")
    assert result.normalized == "Alice Smith"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_string_raises():
    with pytest.raises(ValueError):
        parse_search("")
