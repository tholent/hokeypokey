"""Tests for the GitHub key source plugin."""

from __future__ import annotations

import pgpy
import pgpy.constants
import pytest
from pytest_httpx import HTTPXMock

from hokeypokey.models import SearchResult
from hokeypokey.sources.github import _FRESHNESS_SEP, GitHubSource

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

    result = await source.search("octocat", "github_username")
    await source.close()

    assert isinstance(result, SearchResult)
    assert len(result.keys) == 1
    assert result.keys[0].fingerprint == test_fingerprint
    assert result.keys[0].metadata.get("github_username") == "octocat"
    assert result.keys[0].source_name == "test-github"
    assert result.keys[0].source_priority == 50

    # Freshness token should encode username and ETag
    token = result.keys[0].freshness_token
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

    result = await source.search("nobody", "github_username")
    await source.close()

    assert result.keys == []


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

    result = await source.search("alice@example.com", "email")
    await source.close()

    assert len(result.keys) == 1
    assert result.keys[0].fingerprint == test_fingerprint


@pytest.mark.asyncio
async def test_search_by_email_no_users_found(httpx_mock: HTTPXMock):
    source = make_github_source()

    httpx_mock.add_response(
        url="https://api.github.com/search/users?q=nobody%40example.com+in%3Aemail",
        json={"items": [], "total_count": 0},
    )

    result = await source.search("nobody@example.com", "email")
    await source.close()

    assert result.keys == []


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

    result = await source.search("octocat", "github_username")
    await source.close()

    assert result.keys == []


@pytest.mark.asyncio
async def test_search_rate_limited_403_returns_empty(httpx_mock: HTTPXMock):
    source = make_github_source()

    httpx_mock.add_response(
        url="https://api.github.com/users/octocat/gpg_keys",
        status_code=403,
        headers={"X-RateLimit-Remaining": "0"},
    )

    result = await source.search("octocat", "github_username")
    await source.close()

    assert result.keys == []


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
    result = await source.search("value", "nonexistent_field")
    await source.close()
    assert result.keys == []


# ---------------------------------------------------------------------------
# Username validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_invalid_username_returns_empty_no_request(httpx_mock: HTTPXMock):
    """An invalid username must be rejected before any HTTP request is made."""
    source = make_github_source()
    result = await source.search("../evil", "github_username")
    await source.close()
    assert result.keys == []
    # httpx_mock would raise if any request was made


@pytest.mark.asyncio
async def test_search_leading_hyphen_rejected(httpx_mock: HTTPXMock):
    source = make_github_source()
    result = await source.search("-badname", "github_username")
    await source.close()
    assert result.keys == []


@pytest.mark.asyncio
async def test_search_trailing_hyphen_rejected(httpx_mock: HTTPXMock):
    source = make_github_source()
    result = await source.search("badname-", "github_username")
    await source.close()
    assert result.keys == []


@pytest.mark.asyncio
async def test_search_too_long_username_rejected(httpx_mock: HTTPXMock):
    source = make_github_source()
    result = await source.search("a" * 40, "github_username")
    await source.close()
    assert result.keys == []


@pytest.mark.asyncio
async def test_search_single_char_username_valid(httpx_mock: HTTPXMock, test_armor, test_fingerprint):
    """A single-character username is valid per GitHub rules."""
    source = make_github_source()
    httpx_mock.add_response(
        url="https://api.github.com/users/a/gpg_keys",
        json=[make_gpg_key_response(test_armor, "a")],
        headers={"ETag": '"abc"'},
    )
    result = await source.search("a", "github_username")
    await source.close()
    assert len(result.keys) == 1


@pytest.mark.asyncio
async def test_check_freshness_invalid_username_in_token_returns_true():
    """Invalid username in freshness token → assume fresh, no HTTP request."""
    source = make_github_source()
    token = f"../evil{_FRESHNESS_SEP}\"etag\""
    result = await source.check_freshness("AABBCC", token)
    await source.close()
    assert result is True


# ---------------------------------------------------------------------------
# Token warning
# ---------------------------------------------------------------------------


def test_missing_token_logs_warning(caplog):
    """GitHubSource warns when token_env is set but the env var is absent."""
    import logging
    with caplog.at_level(logging.WARNING, logger="hokeypokey.sources.github"):
        make_github_source(extra_config={"token_env": "NONEXISTENT_GITHUB_TOKEN_XYZ"})
    assert any("NONEXISTENT_GITHUB_TOKEN_XYZ" in r.message for r in caplog.records)
    assert any("60/hour" in r.message for r in caplog.records)


def test_present_token_no_warning(caplog, monkeypatch):
    """GitHubSource does not warn when the token env var is set."""
    import logging
    monkeypatch.setenv("TEST_GITHUB_TOKEN_XYZ", "ghp_fake")
    with caplog.at_level(logging.WARNING, logger="hokeypokey.sources.github"):
        make_github_source(extra_config={"token_env": "TEST_GITHUB_TOKEN_XYZ"})
    assert not any("60/hour" in r.message for r in caplog.records)
