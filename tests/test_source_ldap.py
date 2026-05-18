"""Tests for the LDAP key source plugin."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pgpy
import pgpy.constants
import pytest

from hokeypokey.sources.ldap import LDAPSource, _FRESHNESS_SEP


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def test_pgp_key():
    key = pgpy.PGPKey.new(pgpy.constants.PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    uid = pgpy.PGPUID.new("LDAP User", email="ldap@example.com")
    key.add_uid(
        uid,
        usage={pgpy.constants.KeyFlags.Sign},
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


def make_ldap_source(extra_config: dict | None = None) -> LDAPSource:
    config = {
        "uri": "ldap://localhost",
        "base_dn": "ou=people,dc=example,dc=com",
        "key_attribute": "pgpKey",
        "fields": {
            "email": "mail",
            "username": "uid",
            "github_id": "githubUsername",
        },
    }
    if extra_config:
        config.update(extra_config)
    return LDAPSource(name="test-ldap", priority=10, ttl=300, config=config)


def make_ldap_entry(armor: str, dn: str = "uid=alice,ou=people,dc=example,dc=com",
                    mail: str = "alice@example.com", uid: str = "alice",
                    github_id: str = "octocat",
                    modify_ts: str = "20240101000000Z") -> dict:
    return {
        "_dn": dn,
        "pgpKey": armor,
        "mail": mail,
        "uid": uid,
        "githubUsername": github_id,
        "modifyTimestamp": modify_ts,
    }


# ---------------------------------------------------------------------------
# searchable_fields
# ---------------------------------------------------------------------------


def test_searchable_fields():
    source = make_ldap_source()
    fields = source.searchable_fields()
    names = {f.name for f in fields}
    assert "email" in names
    assert "username" in names
    assert "github_id" in names


# ---------------------------------------------------------------------------
# search — filter construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_constructs_correct_filter(test_armor, test_fingerprint):
    source = make_ldap_source()
    entry = make_ldap_entry(test_armor)

    with patch.object(source, "_ldap_search", return_value=[entry]) as mock_search:
        results = await source.search("alice@example.com", "email")

    # Verify the filter was constructed correctly
    call_args = mock_search.call_args
    search_filter = call_args[0][2]  # positional arg: base, scope, filter, attrs
    assert "mail=alice@example.com" in search_filter
    assert search_filter.startswith("(&")

    assert len(results) == 1
    assert results[0].fingerprint == test_fingerprint
    assert results[0].metadata["email"] == "alice@example.com"
    assert results[0].metadata["username"] == "alice"
    assert results[0].metadata["github_id"] == "octocat"
    assert results[0].source_name == "test-ldap"
    assert results[0].source_priority == 10


@pytest.mark.asyncio
async def test_search_unknown_field_returns_empty():
    source = make_ldap_source()
    results = await source.search("alice@example.com", "nonexistent_field")
    assert results == []


@pytest.mark.asyncio
async def test_search_ldap_error_returns_empty():
    source = make_ldap_source()
    with patch.object(source, "_ldap_search", side_effect=Exception("connection refused")):
        results = await source.search("alice@example.com", "email")
    assert results == []


# ---------------------------------------------------------------------------
# LDAP filter injection prevention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_escapes_filter_injection(test_armor):
    """Malicious query must not inject into the LDAP filter."""
    source = make_ldap_source()

    with patch.object(source, "_ldap_search", return_value=[]) as mock_search:
        await source.search("user@example.com)(objectClass=*", "email")

    call_args = mock_search.call_args
    search_filter = call_args[0][2]
    # The injected ')' must be escaped — the filter must remain syntactically valid
    # and must not contain the raw injection string
    assert ")(objectClass=*)" not in search_filter


# ---------------------------------------------------------------------------
# freshness token format
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_freshness_token_format(test_armor, test_fingerprint):
    source = make_ldap_source()
    dn = "uid=alice,ou=people,dc=example,dc=com"
    ts = "20240101000000Z"
    entry = make_ldap_entry(test_armor, dn=dn, modify_ts=ts)

    with patch.object(source, "_ldap_search", return_value=[entry]):
        results = await source.search("alice@example.com", "email")

    assert len(results) == 1
    token = results[0].freshness_token
    assert _FRESHNESS_SEP in token
    token_dn, token_ts = token.split(_FRESHNESS_SEP, 1)
    assert token_dn == dn
    assert token_ts == ts


# ---------------------------------------------------------------------------
# check_freshness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_freshness_matching_timestamp_returns_true():
    source = make_ldap_source()
    dn = "uid=alice,ou=people,dc=example,dc=com"
    ts = "20240101000000Z"
    token = f"{dn}{_FRESHNESS_SEP}{ts}"

    entry = {"_dn": dn, "modifyTimestamp": ts}
    with patch.object(source, "_ldap_search", return_value=[entry]):
        result = await source.check_freshness("AABBCC", token)

    assert result is True


@pytest.mark.asyncio
async def test_check_freshness_different_timestamp_returns_false():
    source = make_ldap_source()
    dn = "uid=alice,ou=people,dc=example,dc=com"
    old_ts = "20240101000000Z"
    new_ts = "20240201000000Z"
    token = f"{dn}{_FRESHNESS_SEP}{old_ts}"

    entry = {"_dn": dn, "modifyTimestamp": new_ts}
    with patch.object(source, "_ldap_search", return_value=[entry]):
        result = await source.check_freshness("AABBCC", token)

    assert result is False


@pytest.mark.asyncio
async def test_check_freshness_missing_entry_returns_false():
    source = make_ldap_source()
    dn = "uid=alice,ou=people,dc=example,dc=com"
    token = f"{dn}{_FRESHNESS_SEP}20240101000000Z"

    with patch.object(source, "_ldap_search", return_value=[]):
        result = await source.check_freshness("AABBCC", token)

    assert result is False


@pytest.mark.asyncio
async def test_check_freshness_invalid_token_returns_false():
    source = make_ldap_source()
    result = await source.check_freshness("AABBCC", "no-separator-here")
    assert result is False


@pytest.mark.asyncio
async def test_check_freshness_ldap_error_returns_true():
    """On LDAP error, assume fresh to avoid cascading failures."""
    source = make_ldap_source()
    dn = "uid=alice,ou=people,dc=example,dc=com"
    token = f"{dn}{_FRESHNESS_SEP}20240101000000Z"

    with patch.object(source, "_ldap_search", side_effect=Exception("timeout")):
        result = await source.check_freshness("AABBCC", token)

    assert result is True


# ---------------------------------------------------------------------------
# fetch_by_fingerprint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_by_fingerprint_returns_none_without_config():
    """Without fingerprint_attribute configured, returns None."""
    source = make_ldap_source()  # no fingerprint_attribute
    result = await source.fetch_by_fingerprint("A" * 40)
    assert result is None


@pytest.mark.asyncio
async def test_fetch_by_fingerprint_with_config(test_armor, test_fingerprint):
    source = make_ldap_source({"fingerprint_attribute": "pgpCertID"})
    entry = make_ldap_entry(test_armor)

    with patch.object(source, "_ldap_search", return_value=[entry]):
        result = await source.fetch_by_fingerprint(test_fingerprint)

    assert result is not None
    assert result.fingerprint == test_fingerprint
