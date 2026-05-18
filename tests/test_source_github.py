"""Tests for the GitHub key source plugin."""

from __future__ import annotations

import json

import pgpy
import pgpy.constants
import pytest
from pytest_httpx import HTTPXMock

from hokeypokey.sources.github import GitHubSource, _FRESHNESS_SEP


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def test_pgp_key():
    key = pgpy.PGPKey.new(pgpy.constants.PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    uid = pgpy.PGPUID.new("GitHub User", email="gh@example.com")
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


def make_github_source(extra_config: dict | None = None) -> GitHubSource:
    config = {
        "fields": {
            "github_username": "login",
            "email": "email",
        },
    }
    if extra_config:
        config.update(extra_config)
    return GitHubSource(name="test-github", priority=50, ttl=900, config=config)


def make_gpg_key_response(armor: str, username: str = "octocat") -> dict:
    return {
        "id": 1,
        "key_id": "DEADBEEF",
        "raw_key": armor,
        "emails": [{"email": f"{username}@github.com", "verified": True}],
    }


# ---------------------------------------------------------------------------
# searchable_fields
# ---------------------------------------------------------------------------


def test_searchable_fields():
    source = make_github_source()
    fields = source.searchable_fields()
    names = {f.name for f in fields}
    assert "github_username" in names
    assert "email" in names


# ---------------------------------------------------------------------------
# search by username
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_by_username(httpx_mock: HTTPXMock, test_armor, test_fingerprint):
    source = make_github_source()

    httpx_mock.add_response(
        url="https://api.github.com/users/octocat/gpg_keys",
        json=[make_gpg_key_response(test_armor, "octocat")],
        headers={"ETag": '"abc123"'},
    )

    results = await source.search("octocat", "github_username")
    await source.close()

    assert len(results) == 1
    assert results[0].fingerprint == test_fingerprint
    assert results[0].metadata.get("github_username") == "octocat"
    assert results[0].source_name == "test-github"
    assert results[0].source_priority == 50

    # Freshness token should encode username and ETag
    token = results[0].freshness_token
    assert _FRESHNESS_SEP in token
    username_part, etag_part = token.split(_FRESHNESS_SEP, 1)
    assert username_part == "octocat"
    assert "abc123" in etag_part


@pytest.mark.asyncio
async def test_search_by_username_not_found(httpx_mock: HTTPXMock):
    source = make_github_source()

    httpx_mock.add_response(
        url="https://api.github.com/users/nobody/gpg_keys",
        status_code=404,
    )

    results = await source.search("nobody", "github_username")
    await source.close()

    assert results == []


# ---------------------------------------------------------------------------
# search by email
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_by_email(httpx_mock: HTTPXMock, test_armor, test_fingerprint):
    source = make_github_source()

    # User search response
    httpx_mock.add_response(
        url="https://api.github.com/search/users?q=alice%40example.com+in%3Aemail",
        json={"items": [{"login": "alice"}], "total_count": 1},
    )
    # GPG keys for alice
    httpx_mock.add_response(
        url="https://api.github.com/users/alice/gpg_keys",
        json=[make_gpg_key_response(test_armor, "alice")],
        headers={"ETag": '"xyz789"'},
    )

    results = await source.search("alice@example.com", "email")
    await source.close()

    assert len(results) == 1
    assert results[0].fingerprint == test_fingerprint


@pytest.mark.asyncio
async def test_search_by_email_no_users_found(httpx_mock: HTTPXMock):
    source = make_github_source()

    httpx_mock.add_response(
        url="https://api.github.com/search/users?q=nobody%40example.com+in%3Aemail",
        json={"items": [], "total_count": 0},
    )

    results = await source.search("nobody@example.com", "email")
    await source.close()

    assert results == []


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_rate_limited_429_returns_empty(httpx_mock: HTTPXMock):
    source = make_github_source()

    httpx_mock.add_response(
        url="https://api.github.com/users/octocat/gpg_keys",
        status_code=429,
        headers={"Retry-After": "60"},
    )

    results = await source.search("octocat", "github_username")
    await source.close()

    assert results == []


@pytest.mark.asyncio
async def test_search_rate_limited_403_returns_empty(httpx_mock: HTTPXMock):
    source = make_github_source()

    httpx_mock.add_response(
        url="https://api.github.com/users/octocat/gpg_keys",
        status_code=403,
        headers={"X-RateLimit-Remaining": "0"},
    )

    results = await source.search("octocat", "github_username")
    await source.close()

    assert results == []


# ---------------------------------------------------------------------------
# check_freshness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_freshness_304_returns_true(httpx_mock: HTTPXMock):
    source = make_github_source()
    token = f"octocat{_FRESHNESS_SEP}\"abc123\""

    httpx_mock.add_response(
        url="https://api.github.com/users/octocat/gpg_keys",
        status_code=304,
    )

    result = await source.check_freshness("AABBCC", token)
    await source.close()

    assert result is True


@pytest.mark.asyncio
async def test_check_freshness_200_returns_false(httpx_mock: HTTPXMock, test_armor):
    source = make_github_source()
    token = f"octocat{_FRESHNESS_SEP}\"abc123\""

    httpx_mock.add_response(
        url="https://api.github.com/users/octocat/gpg_keys",
        status_code=200,
        json=[make_gpg_key_response(test_armor)],
        headers={"ETag": '"newetag"'},
    )

    result = await source.check_freshness("AABBCC", token)
    await source.close()

    assert result is False


@pytest.mark.asyncio
async def test_check_freshness_invalid_token_returns_false():
    source = make_github_source()
    result = await source.check_freshness("AABBCC", "no-separator")
    await source.close()
    assert result is False


# ---------------------------------------------------------------------------
# fetch_by_fingerprint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_by_fingerprint_returns_none():
    source = make_github_source()
    result = await source.fetch_by_fingerprint("A" * 40)
    await source.close()
    assert result is None


# ---------------------------------------------------------------------------
# Unknown field
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_unknown_field_returns_empty():
    source = make_github_source()
    results = await source.search("value", "nonexistent_field")
    await source.close()
    assert results == []
