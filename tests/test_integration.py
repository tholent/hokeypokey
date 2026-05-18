"""End-to-end integration tests with mock sources."""

from __future__ import annotations

import time

import pytest

from hokeypokey.config import ResolverConfig
from hokeypokey.models import SearchResult, SourceKey, SourceMetadata
from tests.conftest import make_app_with_sources, make_mock_source


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_alice_key(armor: str, fingerprint: str, source_name: str = "ldap",
                   priority: int = 10, **extra_meta) -> SourceKey:
    meta = {"email": "alice@example.com"}
    meta.update(extra_meta)
    return SourceKey(
        fingerprint=fingerprint,
        key_armor=armor,
        metadata=meta,
        freshness_token=f"uid=alice,ou=people,dc=example,dc=com|||20240101000000Z",
        source_name=source_name,
        source_priority=priority,
    )


def make_bob_key(armor: str, fingerprint: str, source_name: str = "github",
                 priority: int = 50) -> SourceKey:
    return SourceKey(
        fingerprint=fingerprint,
        key_armor=armor,
        metadata={"email": "bob@github.com", "github_username": "octocat"},
        freshness_token=f"octocat|||\"etag-abc\"",
        source_name=source_name,
        source_priority=priority,
    )


# ---------------------------------------------------------------------------
# Scenario 1: Cold cache, email search, single LDAP source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cold_cache_email_search_index(alice_armor, alice_fingerprint):
    alice_key = make_alice_key(alice_armor, alice_fingerprint)
    ldap = make_mock_source("ldap", 10, 300, ["email"], search_result=[alice_key])

    app, _, _ = make_app_with_sources({"ldap": ldap})
    async with app.test_client() as client:
        resp = await client.get("/pks/lookup?op=index&search=alice@example.com&options=mr")

    assert resp.status_code == 200
    body = await resp.get_data(as_text=True)
    assert "info:1:1" in body
    assert alice_fingerprint in body


@pytest.mark.asyncio
async def test_cold_cache_email_search_get(alice_armor, alice_fingerprint):
    alice_key = make_alice_key(alice_armor, alice_fingerprint)
    ldap = make_mock_source("ldap", 10, 300, ["email"], search_result=[alice_key])

    app, _, _ = make_app_with_sources({"ldap": ldap})
    async with app.test_client() as client:
        resp = await client.get("/pks/lookup?op=get&search=alice@example.com&options=mr")

    assert resp.status_code == 200
    body = await resp.get_data(as_text=True)
    assert "BEGIN PGP PUBLIC KEY BLOCK" in body


# ---------------------------------------------------------------------------
# Scenario 2: Cold cache, fingerprint lookup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cold_cache_fingerprint_lookup(alice_armor, alice_fingerprint):
    alice_key = make_alice_key(alice_armor, alice_fingerprint)
    ldap = make_mock_source("ldap", 10, 300, ["email"], fetch_result=alice_key)

    app, _, _ = make_app_with_sources({"ldap": ldap})
    async with app.test_client() as client:
        resp = await client.get(
            f"/pks/lookup?op=get&search=0x{alice_fingerprint}&options=mr"
        )

    assert resp.status_code == 200
    ldap.fetch_by_fingerprint.assert_called_once_with(alice_fingerprint)


# ---------------------------------------------------------------------------
# Scenario 3: Warm cache — second request served from cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warm_cache_no_source_call_on_second_request(alice_armor, alice_fingerprint):
    alice_key = make_alice_key(alice_armor, alice_fingerprint)
    ldap = make_mock_source("ldap", 10, 300, ["email"], search_result=[alice_key])

    app, _, _ = make_app_with_sources({"ldap": ldap})
    async with app.test_client() as client:
        # First request — populates cache
        await client.get("/pks/lookup?op=get&search=alice@example.com&options=mr")
        # Second request — should be served from cache
        resp = await client.get("/pks/lookup?op=get&search=alice@example.com&options=mr")

    assert resp.status_code == 200
    # search() called only once (first request)
    assert ldap.search.call_count == 1


# ---------------------------------------------------------------------------
# Scenario 4: Stale cache, freshness check passes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_cache_freshness_passes(alice_armor, alice_fingerprint):
    alice_key = make_alice_key(alice_armor, alice_fingerprint)
    ldap = make_mock_source("ldap", 10, 300, ["email"],
                            search_result=[alice_key], freshness_result=True)

    app, _, cache = make_app_with_sources({"ldap": ldap})
    async with app.test_client() as client:
        # Populate cache
        await client.get("/pks/lookup?op=get&search=alice@example.com&options=mr")

        # Wind back cached_at to make it stale
        entry = cache.get_by_fingerprint(alice_fingerprint)
        assert entry is not None
        entry.cached_at = time.time() - 400

        # Second request — freshness check should pass, no re-search
        resp = await client.get("/pks/lookup?op=get&search=alice@example.com&options=mr")

    assert resp.status_code == 200
    ldap.check_freshness.assert_called_once()
    assert ldap.search.call_count == 1  # not called again


# ---------------------------------------------------------------------------
# Scenario 5: Stale cache, freshness check fails → re-fetch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_cache_freshness_fails_refetch(alice_armor, alice_fingerprint):
    alice_key = make_alice_key(alice_armor, alice_fingerprint)
    updated_key = make_alice_key(alice_armor, alice_fingerprint,
                                 freshness_token="uid=alice|||20240201000000Z")
    ldap = make_mock_source("ldap", 10, 300, ["email"],
                            search_result=[alice_key],
                            fetch_result=updated_key,
                            freshness_result=False)

    app, _, cache = make_app_with_sources({"ldap": ldap})
    async with app.test_client() as client:
        # Populate cache
        await client.get("/pks/lookup?op=get&search=alice@example.com&options=mr")

        # Wind back cached_at
        entry = cache.get_by_fingerprint(alice_fingerprint)
        assert entry is not None
        entry.cached_at = time.time() - 400

        # Second request — freshness check fails, should refetch
        resp = await client.get("/pks/lookup?op=get&search=alice@example.com&options=mr")

    assert resp.status_code == 200
    ldap.check_freshness.assert_called_once()
    ldap.fetch_by_fingerprint.assert_called_once()


# ---------------------------------------------------------------------------
# Scenario 6: Cross-source resolver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_source_resolver(alice_armor, alice_fingerprint, bob_armor, bob_fingerprint):
    alice_key = make_alice_key(alice_armor, alice_fingerprint, github_id="octocat")
    bob_key = make_bob_key(bob_armor, bob_fingerprint)

    ldap = make_mock_source("ldap", 10, 300, ["email", "github_id"],
                            search_result=[alice_key])
    github = make_mock_source("github", 50, 900, ["github_username"],
                              search_result=[bob_key])

    resolver = ResolverConfig(
        name="ldap-to-github",
        trigger_source="ldap",
        trigger_field="github_id",
        target_source="github",
        target_field="github_username",
    )

    app, _, _ = make_app_with_sources(
        {"ldap": ldap, "github": github},
        resolvers=[resolver],
    )
    async with app.test_client() as client:
        resp = await client.get("/pks/lookup?op=index&search=alice@example.com&options=mr")

    assert resp.status_code == 200
    body = await resp.get_data(as_text=True)

    # Both keys should appear in the index
    assert alice_fingerprint in body
    assert bob_fingerprint in body

    # GitHub was queried via resolver
    github.search.assert_called_once_with("octocat", "github_username")


# ---------------------------------------------------------------------------
# Scenario 7: Priority deduplication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_priority_deduplication(alice_armor, alice_fingerprint):
    ldap_key = make_alice_key(alice_armor, alice_fingerprint, source_name="ldap", priority=10)
    github_key = make_alice_key(alice_armor, alice_fingerprint, source_name="github", priority=50)

    ldap = make_mock_source("ldap", 10, 300, ["email"], search_result=[ldap_key])
    github = make_mock_source("github", 50, 900, ["email"], search_result=[github_key])

    app, _, _ = make_app_with_sources({"ldap": ldap, "github": github})
    async with app.test_client() as client:
        resp = await client.get("/pks/lookup?op=index&search=alice@example.com&options=mr")

    assert resp.status_code == 200
    body = await resp.get_data(as_text=True)

    # Only one key in the index (deduplicated)
    assert body.count("pub:") == 1
    assert alice_fingerprint in body


# ---------------------------------------------------------------------------
# Scenario 8: POST /pks/add → 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_pks_add_returns_403():
    app, _, _ = make_app_with_sources({})
    async with app.test_client() as client:
        resp = await client.post("/pks/add", data={"keytext": "fake"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Scenario 9: Unknown op → 501
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_op_returns_501():
    app, _, _ = make_app_with_sources({})
    async with app.test_client() as client:
        resp = await client.get("/pks/lookup?op=frobnicate&search=x")
    assert resp.status_code == 501


# ---------------------------------------------------------------------------
# Scenario 10: Missing params → 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_op_returns_400():
    app, _, _ = make_app_with_sources({})
    async with app.test_client() as client:
        resp = await client.get("/pks/lookup?search=alice@example.com")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_missing_search_returns_400():
    app, _, _ = make_app_with_sources({})
    async with app.test_client() as client:
        resp = await client.get("/pks/lookup?op=get")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Scenario 11: Keyless LDAP entry triggers resolver to GitHub
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keyless_ldap_triggers_resolver_to_github(bob_armor, bob_fingerprint):
    """LDAP has no PGP key for the user, but has a github_id.
    The resolver should use that github_id to fetch keys from GitHub.
    """
    # LDAP returns metadata-only (no PGP key, but has github_id)
    ldap_metadata = SourceMetadata(
        metadata={"username": "wells", "github_id": "wellsiau", "email": "wells@example.com"},
        source_name="ldap",
        source_priority=10,
    )
    ldap_result = SearchResult(keys=[], metadata_only=[ldap_metadata])

    # GitHub returns a real key for the resolved github username
    bob_key = make_bob_key(bob_armor, bob_fingerprint)

    ldap = make_mock_source(
        "ldap", 10, 300, ["email", "username", "github_id"],
        search_result=ldap_result,
    )
    github = make_mock_source(
        "github", 50, 900, ["github_username"],
        search_result=[bob_key],
        text_searchable=False,
    )

    resolver = ResolverConfig(
        name="ldap-to-github",
        trigger_source="ldap",
        trigger_field="github_id",
        target_source="github",
        target_field="github_username",
    )

    app, _, _ = make_app_with_sources(
        {"ldap": ldap, "github": github},
        resolvers=[resolver],
    )
    async with app.test_client() as client:
        resp = await client.get("/pks/lookup?op=index&search=wells&options=mr")

    assert resp.status_code == 200
    body = await resp.get_data(as_text=True)
    assert bob_fingerprint in body

    # GitHub was queried via resolver with the LDAP github_id, NOT with "wells"
    github.search.assert_called_once_with("wellsiau", "github_username")


# ---------------------------------------------------------------------------
# Scenario 12: TEXT search does not fan out to non-text-searchable sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_search_does_not_fan_out_to_github_directly(bob_armor, bob_fingerprint):
    """A bare text search like 'wells' must NOT query GitHub directly.
    GitHub fields are not text-searchable; they should only be reached via resolvers.
    """
    # LDAP returns nothing (no match)
    ldap = make_mock_source("ldap", 10, 300, ["email", "username"], search_result=[])
    # GitHub should NOT be queried at all
    github = make_mock_source(
        "github", 50, 900, ["github_username"],
        search_result=[],
        text_searchable=False,
    )

    app, _, _ = make_app_with_sources({"ldap": ldap, "github": github})
    async with app.test_client() as client:
        resp = await client.get("/pks/lookup?op=index&search=wells&options=mr")

    assert resp.status_code == 404
    # LDAP was queried (it has text-searchable fields)
    assert ldap.search.call_count >= 1
    # GitHub was NOT queried (no text-searchable fields)
    github.search.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 13: Keyless LDAP + resolver + key from GitHub via email search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keyless_ldap_resolver_via_email_search(bob_armor, bob_fingerprint):
    """Search by email finds LDAP user with no key but with github_id.
    Resolver fires and finds the key on GitHub.
    """
    ldap_metadata = SourceMetadata(
        metadata={"email": "wells@example.com", "github_id": "wellsiau"},
        source_name="ldap",
        source_priority=10,
    )
    ldap_result = SearchResult(keys=[], metadata_only=[ldap_metadata])

    bob_key = make_bob_key(bob_armor, bob_fingerprint)

    ldap = make_mock_source(
        "ldap", 10, 300, ["email", "github_id"],
        search_result=ldap_result,
    )
    github = make_mock_source(
        "github", 50, 900, ["github_username"],
        search_result=[bob_key],
        text_searchable=False,
    )

    resolver = ResolverConfig(
        name="ldap-to-github",
        trigger_source="ldap",
        trigger_field="github_id",
        target_source="github",
        target_field="github_username",
    )

    app, _, _ = make_app_with_sources(
        {"ldap": ldap, "github": github},
        resolvers=[resolver],
    )
    async with app.test_client() as client:
        resp = await client.get(
            "/pks/lookup?op=get&search=wells@example.com&options=mr"
        )

    assert resp.status_code == 200
    body = await resp.get_data(as_text=True)
    assert "BEGIN PGP PUBLIC KEY BLOCK" in body
    github.search.assert_called_once_with("wellsiau", "github_username")
