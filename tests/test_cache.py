"""Tests for the in-memory key cache."""

from __future__ import annotations

import time

import pytest

from hokeypokey.cache import KeyCache
from hokeypokey.models import SourceKey


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_key(
    fingerprint: str = "AABBCCDD" * 5,  # 40 hex chars
    source_name: str = "test-source",
    source_priority: int = 10,
    email: str = "alice@example.com",
    **extra_metadata: str,
) -> SourceKey:
    metadata: dict[str, str] = {"email": email}
    metadata.update(extra_metadata)
    return SourceKey(
        fingerprint=fingerprint,
        key_armor="-----BEGIN PGP PUBLIC KEY BLOCK-----\nfake\n-----END PGP PUBLIC KEY BLOCK-----",
        metadata=metadata,
        freshness_token="token-123",
        source_name=source_name,
        source_priority=source_priority,
    )


FP = "AABBCCDD" * 5  # canonical 40-char fingerprint used in most tests
FP2 = "11223344" * 5


# ---------------------------------------------------------------------------
# Basic put / get
# ---------------------------------------------------------------------------


def test_put_and_get_by_fingerprint():
    cache = KeyCache()
    key = make_key(FP)
    cache.put(key, ttl=300)
    result = cache.get_by_fingerprint(FP)
    assert result is not None
    assert result.source_key.fingerprint == FP


def test_get_by_fingerprint_case_insensitive():
    cache = KeyCache()
    key = make_key(FP)
    cache.put(key, ttl=300)
    assert cache.get_by_fingerprint(FP.lower()) is not None
    assert cache.get_by_fingerprint("0x" + FP) is not None


def test_get_by_fingerprint_missing():
    cache = KeyCache()
    assert cache.get_by_fingerprint(FP) is None


# ---------------------------------------------------------------------------
# Priority conflict resolution
# ---------------------------------------------------------------------------


def test_priority_high_authority_retained():
    """A key from priority=10 must not be overwritten by priority=50."""
    cache = KeyCache()
    high_auth = make_key(FP, source_name="ldap", source_priority=10)
    low_auth = make_key(FP, source_name="github", source_priority=50)

    cache.put(high_auth, ttl=300)
    cache.put(low_auth, ttl=300)

    result = cache.get_by_fingerprint(FP)
    assert result is not None
    assert result.source_key.source_name == "ldap"
    assert result.source_key.source_priority == 10


def test_priority_lower_authority_replaced():
    """A key from priority=50 must be replaced by priority=10."""
    cache = KeyCache()
    low_auth = make_key(FP, source_name="github", source_priority=50)
    high_auth = make_key(FP, source_name="ldap", source_priority=10)

    cache.put(low_auth, ttl=300)
    cache.put(high_auth, ttl=300)

    result = cache.get_by_fingerprint(FP)
    assert result is not None
    assert result.source_key.source_name == "ldap"
    assert result.source_key.source_priority == 10


def test_priority_equal_replaced():
    """Same priority: the newer put wins (last-write-wins within same priority)."""
    cache = KeyCache()
    first = make_key(FP, source_name="ldap-primary", source_priority=10)
    second = make_key(FP, source_name="ldap-secondary", source_priority=10)

    cache.put(first, ttl=300)
    cache.put(second, ttl=300)

    result = cache.get_by_fingerprint(FP)
    assert result is not None
    assert result.source_key.source_name == "ldap-secondary"


# ---------------------------------------------------------------------------
# Email search
# ---------------------------------------------------------------------------


def test_search_by_email():
    cache = KeyCache()
    key = make_key(FP, email="alice@example.com")
    cache.put(key, ttl=300)

    results = cache.search("alice@example.com", "email")
    assert len(results) == 1
    assert results[0].source_key.fingerprint == FP


def test_search_by_email_case_insensitive():
    cache = KeyCache()
    key = make_key(FP, email="Alice@Example.COM")
    cache.put(key, ttl=300)

    results = cache.search("alice@example.com", "email")
    assert len(results) == 1


def test_search_by_email_no_match():
    cache = KeyCache()
    key = make_key(FP, email="alice@example.com")
    cache.put(key, ttl=300)

    results = cache.search("bob@example.com", "email")
    assert results == []


def test_search_by_custom_field():
    cache = KeyCache()
    key = make_key(FP, github_username="octocat")
    cache.put(key, ttl=300)

    results = cache.search("octocat", "github_username")
    assert len(results) == 1
    assert results[0].source_key.fingerprint == FP


# ---------------------------------------------------------------------------
# Key ID lookups
# ---------------------------------------------------------------------------


def test_get_by_long_key_id():
    cache = KeyCache()
    key = make_key(FP)
    cache.put(key, ttl=300)

    long_id = FP[-16:]
    results = cache.get_by_key_id(long_id)
    assert len(results) == 1
    assert results[0].source_key.fingerprint == FP


def test_get_by_short_key_id():
    cache = KeyCache()
    key = make_key(FP)
    cache.put(key, ttl=300)

    short_id = FP[-8:]
    results = cache.get_by_key_id(short_id)
    assert len(results) == 1
    assert results[0].source_key.fingerprint == FP


def test_get_by_key_id_with_0x_prefix():
    cache = KeyCache()
    key = make_key(FP)
    cache.put(key, ttl=300)

    results = cache.get_by_key_id("0x" + FP[-16:])
    assert len(results) == 1


def test_get_by_key_id_invalid_length():
    cache = KeyCache()
    key = make_key(FP)
    cache.put(key, ttl=300)

    # 9 chars — neither short nor long key ID
    results = cache.get_by_key_id("ABCDEF123")
    assert results == []


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


def test_is_fresh_within_ttl():
    cache = KeyCache()
    key = make_key(FP)
    cache.put(key, ttl=300)
    assert cache.is_fresh(FP) is True


def test_is_fresh_expired():
    cache = KeyCache()
    key = make_key(FP)
    cache.put(key, ttl=300)

    # Wind back cached_at so the entry appears old
    entry = cache.get_by_fingerprint(FP)
    assert entry is not None
    entry.cached_at = time.time() - 400
    assert cache.is_fresh(FP) is False


def test_is_fresh_missing():
    cache = KeyCache()
    assert cache.is_fresh(FP) is False


def test_is_fresh_expired_via_cached_at(monkeypatch):
    """Simulate expiry by manipulating cached_at directly."""
    cache = KeyCache()
    key = make_key(FP)
    cache.put(key, ttl=10)

    entry = cache.get_by_fingerprint(FP)
    assert entry is not None
    # Wind back cached_at so the entry appears old
    entry.cached_at = time.time() - 20
    assert cache.is_fresh(FP) is False


# ---------------------------------------------------------------------------
# Remove
# ---------------------------------------------------------------------------


def test_remove_clears_store():
    cache = KeyCache()
    key = make_key(FP)
    cache.put(key, ttl=300)
    cache.remove(FP)
    assert cache.get_by_fingerprint(FP) is None


def test_remove_clears_email_index():
    cache = KeyCache()
    key = make_key(FP, email="alice@example.com")
    cache.put(key, ttl=300)
    cache.remove(FP)
    assert cache.search("alice@example.com", "email") == []


def test_remove_clears_key_id_indexes():
    cache = KeyCache()
    key = make_key(FP)
    cache.put(key, ttl=300)
    cache.remove(FP)
    assert cache.get_by_key_id(FP[-16:]) == []
    assert cache.get_by_key_id(FP[-8:]) == []


def test_remove_nonexistent_is_noop():
    cache = KeyCache()
    cache.remove(FP)  # should not raise


def test_remove_by_source():
    cache = KeyCache()
    key1 = make_key(FP, source_name="ldap", source_priority=10)
    key2 = make_key(FP2, source_name="github", source_priority=50)
    cache.put(key1, ttl=300)
    cache.put(key2, ttl=300)

    cache.remove_by_source("ldap")

    assert cache.get_by_fingerprint(FP) is None
    assert cache.get_by_fingerprint(FP2) is not None


# ---------------------------------------------------------------------------
# Len
# ---------------------------------------------------------------------------


def test_len():
    cache = KeyCache()
    assert len(cache) == 0
    cache.put(make_key(FP), ttl=300)
    assert len(cache) == 1
    cache.put(make_key(FP2), ttl=300)
    assert len(cache) == 2
    cache.remove(FP)
    assert len(cache) == 1
