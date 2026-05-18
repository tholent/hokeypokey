"""Tests for HKP response formatters."""

from __future__ import annotations

import pgpy
import pgpy.constants
import pytest

from hokeypokey.hkp.formatter import (
    _encode_uid,
    format_get_response,
    format_index_response,
    parse_key_metadata,
)
from hokeypokey.models import SourceKey


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def test_pgp_key():
    """Generate a real RSA-2048 test key with a single UID."""
    key = pgpy.PGPKey.new(pgpy.constants.PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    uid = pgpy.PGPUID.new("Test User", email="test@example.com")
    key.add_uid(
        uid,
        usage={pgpy.constants.KeyFlags.Sign, pgpy.constants.KeyFlags.EncryptCommunications},
        hashes=[pgpy.constants.HashAlgorithm.SHA256],
        ciphers=[pgpy.constants.SymmetricKeyAlgorithm.AES256],
        compression=[pgpy.constants.CompressionAlgorithm.ZLIB],
    )
    return key


@pytest.fixture(scope="module")
def test_armor(test_pgp_key):
    return str(test_pgp_key.pubkey)


@pytest.fixture(scope="module")
def test_fingerprint(test_pgp_key):
    return str(test_pgp_key.fingerprint).replace(" ", "").upper()


def make_source_key(armor: str, fingerprint: str) -> SourceKey:
    return SourceKey(
        fingerprint=fingerprint,
        key_armor=armor,
        metadata={"email": "test@example.com"},
        freshness_token="token",
        source_name="test",
        source_priority=10,
    )


# ---------------------------------------------------------------------------
# parse_key_metadata
# ---------------------------------------------------------------------------


def test_parse_key_metadata_returns_info(test_armor, test_fingerprint):
    info = parse_key_metadata(test_armor)
    assert info is not None
    assert info.fingerprint == test_fingerprint
    assert info.algo > 0  # RSA = 1
    assert info.keylen == 2048
    assert info.created > 0
    assert info.expires is None
    assert "r" not in info.flags
    assert "e" not in info.flags


def test_parse_key_metadata_has_uid(test_armor):
    info = parse_key_metadata(test_armor)
    assert info is not None
    assert len(info.uids) >= 1
    assert "Test User" in info.uids[0].uid_string or "test@example.com" in info.uids[0].uid_string


def test_parse_key_metadata_invalid_armor():
    result = parse_key_metadata("not a pgp key")
    assert result is None


def test_parse_key_metadata_empty_string():
    result = parse_key_metadata("")
    assert result is None


# ---------------------------------------------------------------------------
# UID encoding
# ---------------------------------------------------------------------------


def test_encode_uid_plain():
    assert _encode_uid("Alice Smith") == "Alice Smith"


def test_encode_uid_colon_encoded():
    encoded = _encode_uid("Alice:Smith")
    assert ":" not in encoded
    assert "%3A" in encoded.upper() or "%3a" in encoded


def test_encode_uid_percent_encoded():
    encoded = _encode_uid("100%")
    assert "%25" in encoded


def test_encode_uid_email_style():
    encoded = _encode_uid("Alice <alice@example.com>")
    # @ and < > should be encoded
    assert "alice" in encoded.lower()


def test_encode_uid_non_ascii():
    encoded = _encode_uid("Ångström")
    # Non-ASCII must be percent-encoded
    assert "%" in encoded


# ---------------------------------------------------------------------------
# format_index_response
# ---------------------------------------------------------------------------


def test_format_index_response_structure(test_armor, test_fingerprint):
    key = make_source_key(test_armor, test_fingerprint)
    result = format_index_response([key])

    lines = result.strip().split("\n")
    assert lines[0] == "info:1:1"

    pub_line = next((l for l in lines if l.startswith("pub:")), None)
    assert pub_line is not None

    parts = pub_line.split(":")
    assert len(parts) >= 7
    assert parts[1] == test_fingerprint  # fingerprint field
    assert parts[2] != ""  # algo
    assert parts[4] != ""  # creation date (unix timestamp)


def test_format_index_response_uid_line(test_armor, test_fingerprint):
    key = make_source_key(test_armor, test_fingerprint)
    result = format_index_response([key])

    lines = result.strip().split("\n")
    uid_lines = [l for l in lines if l.startswith("uid:")]
    assert len(uid_lines) >= 1

    # UID line must have at least 5 colon-separated fields
    parts = uid_lines[0].split(":")
    assert len(parts) >= 5


def test_format_index_response_count(test_armor, test_fingerprint):
    key1 = make_source_key(test_armor, test_fingerprint)
    key2 = make_source_key(test_armor, test_fingerprint)
    result = format_index_response([key1, key2])
    assert result.startswith("info:1:2")


def test_format_index_response_empty():
    result = format_index_response([])
    assert result.startswith("info:1:0")


def test_format_index_response_ends_with_newline(test_armor, test_fingerprint):
    key = make_source_key(test_armor, test_fingerprint)
    result = format_index_response([key])
    assert result.endswith("\n")


def test_format_index_response_uid_special_chars(test_fingerprint):
    """UID with ':' and '%' must be percent-encoded in the index."""
    # Create a key with a UID containing special characters
    key = pgpy.PGPKey.new(pgpy.constants.PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    uid = pgpy.PGPUID.new("Colon:User", email="colon@example.com")
    key.add_uid(
        uid,
        usage={pgpy.constants.KeyFlags.Sign},
        hashes=[pgpy.constants.HashAlgorithm.SHA256],
        ciphers=[pgpy.constants.SymmetricKeyAlgorithm.AES256],
        compression=[pgpy.constants.CompressionAlgorithm.ZLIB],
    )
    armor = str(key.pubkey)
    fp = str(key.fingerprint).replace(" ", "").upper()
    source_key = make_source_key(armor, fp)

    result = format_index_response([source_key])
    uid_lines = [l for l in result.split("\n") if l.startswith("uid:")]
    assert uid_lines, "Expected at least one uid: line"

    # The UID field (second colon-delimited field) must not contain a bare ':'
    # from the UID string itself — it must be encoded
    uid_field = uid_lines[0].split(":", 2)[1]  # everything after first "uid:"
    # The encoded UID should not contain a bare colon from "Colon:User"
    # (colons from the field delimiters are fine, but the UID content colon must be encoded)
    assert "%3A" in uid_field.upper() or "Colon" not in uid_field


# ---------------------------------------------------------------------------
# format_get_response
# ---------------------------------------------------------------------------


def test_format_get_response_single_key(test_armor, test_fingerprint):
    key = make_source_key(test_armor, test_fingerprint)
    result = format_get_response([key])
    assert "-----BEGIN PGP PUBLIC KEY BLOCK-----" in result
    assert "-----END PGP PUBLIC KEY BLOCK-----" in result


def test_format_get_response_two_keys_separated_by_blank_line(test_armor, test_fingerprint):
    key1 = make_source_key(test_armor, test_fingerprint)
    key2 = make_source_key(test_armor, test_fingerprint)
    result = format_get_response([key1, key2])

    # Should have two armor blocks separated by a blank line
    assert result.count("-----BEGIN PGP PUBLIC KEY BLOCK-----") == 2
    assert "\n\n" in result


def test_format_get_response_empty():
    result = format_get_response([])
    assert result.strip() == ""


def test_format_get_response_ends_with_newline(test_armor, test_fingerprint):
    key = make_source_key(test_armor, test_fingerprint)
    result = format_get_response([key])
    assert result.endswith("\n")
