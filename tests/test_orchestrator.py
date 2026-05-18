"""Tests for the search orchestrator."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from hokeypokey.cache import KeyCache
from hokeypokey.config import ResolverConfig
from hokeypokey.models import FieldDefinition, SearchResult, SourceKey
from hokeypokey.orchestrator import SearchOrchestrator
from hokeypokey.resolver import ConfigResolver
from hokeypokey.search import parse_search
from hokeypokey.sources.base import KeySource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FP_ALICE = "A" * 40
FP_BOB = "B" * 40
FP_CAROL = "C" * 40


def make_source_key(
    fp: str = FP_ALICE,
    source_name: str = "ldap",
    source_priority: int = 10,
    email: str = "alice@example.com",
    freshness_token: str = "token-1",
    **extra_metadata: str,
) -> SourceKey:
    metadata: dict[str, str] = {"email": email}
    metadata.update(extra_metadata)
    return SourceKey(
        fingerprint=fp,
        key_armor="-----BEGIN PGP PUBLIC KEY BLOCK-----\nfake\n-----END PGP PUBLIC KEY BLOCK-----",
        metadata=metadata,
        freshness_token=freshness_token,
        source_name=source_name,
        source_priority=source_priority,
    )


def make_mock_source(
    name: str = "ldap",
    priority: int = 10,
    ttl: int = 300,
    fields: list[str] | None = None,
    search_result: list[SourceKey] | None = None,
    fetch_result: SourceKey | None = None,
    freshness_result: bool = True,
) -> MagicMock:
    source = MagicMock(spec=KeySource)
    source.name = name
    source.priority = priority
    source.ttl = ttl
    source.searchable_fields.return_value = [
        FieldDefinition(name=f, source_attribute=f) for f in (fields or ["email"])
    ]
    source.search = AsyncMock(return_value=SearchResult(keys=search_result or []))
    source.fetch_by_fingerprint = AsyncMock(return_value=fetch_result)
    source.check_freshness = AsyncMock(return_value=freshness_result)
    return source


def make_orchestrator(
    sources: dict | None = None,
    cache: KeyCache | None = None,
    resolvers: list | None = None,
    max_depth: int = 2,
) -> SearchOrchestrator:
    return SearchOrchestrator(
        sources=sources or {},
        cache=cache or KeyCache(),
        resolvers=resolvers or [],
        max_depth=max_depth,
    )


# ---------------------------------------------------------------------------
# Cache hit — fresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hit_fresh_no_source_calls():
    """A fresh cache hit must not trigger any source calls."""
    cache = KeyCache()
    key = make_source_key(FP_ALICE)
    cache.put(key, ttl=300)

    source = make_mock_source()
    orch = make_orchestrator(sources={"ldap": source}, cache=cache)

    results = await orch.lookup(parse_search("alice@example.com"))

    assert len(results) == 1
    assert results[0].fingerprint == FP_ALICE
    source.search.assert_not_called()
    source.fetch_by_fingerprint.assert_not_called()
    source.check_freshness.assert_not_called()


# ---------------------------------------------------------------------------
# Cache hit — stale, freshness check passes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hit_stale_freshness_passes():
    """Stale cache hit + freshness check passes → serve from cache, no refetch."""
    cache = KeyCache()
    key = make_source_key(FP_ALICE)
    cache.put(key, ttl=300)

    # Wind back cached_at to make it stale
    entry = cache.get_by_fingerprint(FP_ALICE)
    entry.cached_at = time.time() - 400

    source = make_mock_source(freshness_result=True)
    orch = make_orchestrator(sources={"ldap": source}, cache=cache)

    results = await orch.lookup(parse_search("alice@example.com"))

    assert len(results) == 1
    source.check_freshness.assert_called_once()
    source.fetch_by_fingerprint.assert_not_called()
    source.search.assert_not_called()


# ---------------------------------------------------------------------------
# Cache hit — stale, freshness check fails → refetch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hit_stale_freshness_fails_refetch():
    """Stale cache hit + freshness check fails → source.fetch_by_fingerprint called."""
    cache = KeyCache()
    key = make_source_key(FP_ALICE)
    cache.put(key, ttl=300)

    entry = cache.get_by_fingerprint(FP_ALICE)
    entry.cached_at = time.time() - 400

    updated_key = make_source_key(FP_ALICE, freshness_token="token-2")
    source = make_mock_source(freshness_result=False, fetch_result=updated_key)
    orch = make_orchestrator(sources={"ldap": source}, cache=cache)

    results = await orch.lookup(parse_search("alice@example.com"))

    assert len(results) == 1
    source.check_freshness.assert_called_once()
    source.fetch_by_fingerprint.assert_called_once_with(FP_ALICE)

    # Cache should now have the updated token
    refreshed = cache.get_by_fingerprint(FP_ALICE)
    assert refreshed.freshness_token == "token-2"


# ---------------------------------------------------------------------------
# Cache miss — fan out to sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_miss_fans_out_to_all_sources():
    """On cache miss, all sources with matching fields are queried."""
    key = make_source_key(FP_ALICE)
    source1 = make_mock_source(name="ldap", priority=10, search_result=[key])
    source2 = make_mock_source(name="github", priority=50, search_result=[])

    orch = make_orchestrator(sources={"ldap": source1, "github": source2})

    results = await orch.lookup(parse_search("alice@example.com"))

    assert len(results) == 1
    source1.search.assert_called_once_with("alice@example.com", "email")
    source2.search.assert_called_once_with("alice@example.com", "email")


# ---------------------------------------------------------------------------
# Fingerprint lookup — fan out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_lookup_fans_out():
    """Fingerprint lookup fans out fetch_by_fingerprint to all sources."""
    key = make_source_key(FP_ALICE)
    source = make_mock_source(fetch_result=key)

    orch = make_orchestrator(sources={"ldap": source})

    results = await orch.lookup(parse_search("0x" + FP_ALICE))

    assert len(results) == 1
    source.fetch_by_fingerprint.assert_called_once_with(FP_ALICE)


# ---------------------------------------------------------------------------
# Key ID lookup — cache only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_key_id_lookup_cache_only():
    """Key ID lookups are served from cache only; no source calls."""
    cache = KeyCache()
    key = make_source_key(FP_ALICE)
    cache.put(key, ttl=300)

    source = make_mock_source()
    orch = make_orchestrator(sources={"ldap": source}, cache=cache)

    results = await orch.lookup(parse_search("0x" + FP_ALICE[-16:]))

    assert len(results) == 1
    source.search.assert_not_called()
    source.fetch_by_fingerprint.assert_not_called()


@pytest.mark.asyncio
async def test_key_id_lookup_cache_miss_returns_empty():
    """Key ID lookup with no cache entry returns empty list."""
    source = make_mock_source()
    orch = make_orchestrator(sources={"ldap": source})

    results = await orch.lookup(parse_search("0x" + FP_ALICE[-16:]))

    assert results == []
    source.search.assert_not_called()
    source.fetch_by_fingerprint.assert_not_called()


# ---------------------------------------------------------------------------
# Resolver chaining
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_chaining():
    """LDAP result with github_id triggers GitHub lookup via resolver."""
    ldap_key = make_source_key(
        FP_ALICE,
        source_name="ldap",
        source_priority=10,
        email="alice@example.com",
        github_id="octocat",
    )
    github_key = make_source_key(
        FP_BOB,
        source_name="github",
        source_priority=50,
        email="octocat@github.com",
    )

    ldap_source = make_mock_source(
        name="ldap",
        priority=10,
        fields=["email", "github_id"],
        search_result=[ldap_key],
    )
    github_source = make_mock_source(
        name="github",
        priority=50,
        fields=["github_username"],
        search_result=[github_key],
    )

    resolver = ConfigResolver(
        ResolverConfig(
            name="ldap-to-github",
            trigger_source="ldap",
            trigger_field="github_id",
            target_source="github",
            target_field="github_username",
        )
    )

    orch = make_orchestrator(
        sources={"ldap": ldap_source, "github": github_source},
        resolvers=[resolver],
    )

    results = await orch.lookup(parse_search("alice@example.com"))

    # Both keys should be returned
    fps = {r.fingerprint for r in results}
    assert FP_ALICE in fps
    assert FP_BOB in fps

    # GitHub source should have been queried via resolver
    github_source.search.assert_called_once_with("octocat", "github_username")


# ---------------------------------------------------------------------------
# Depth limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_depth_limit_stops_chaining():
    """Resolver chaining stops at max_depth=1 — second-level resolvers don't fire."""
    ldap_key = make_source_key(
        FP_ALICE,
        source_name="ldap",
        source_priority=10,
        email="alice@example.com",
        github_id="octocat",
    )
    github_key = make_source_key(
        FP_BOB,
        source_name="github",
        source_priority=50,
        email="octocat@github.com",
        another_id="deep",
    )

    ldap_source = make_mock_source(
        name="ldap",
        priority=10,
        fields=["email", "github_id"],
        search_result=[ldap_key],
    )
    github_source = make_mock_source(
        name="github",
        priority=50,
        fields=["github_username", "another_id"],
        search_result=[github_key],
    )
    deep_source = make_mock_source(
        name="deep",
        priority=90,
        fields=["deep_field"],
        search_result=[],
    )

    resolver1 = ConfigResolver(
        ResolverConfig(
            name="ldap-to-github",
            trigger_source="ldap",
            trigger_field="github_id",
            target_source="github",
            target_field="github_username",
        )
    )
    resolver2 = ConfigResolver(
        ResolverConfig(
            name="github-to-deep",
            trigger_source="github",
            trigger_field="another_id",
            target_source="deep",
            target_field="deep_field",
        )
    )

    orch = make_orchestrator(
        sources={"ldap": ldap_source, "github": github_source, "deep": deep_source},
        resolvers=[resolver1, resolver2],
        max_depth=1,  # only one level of chaining
    )

    await orch.lookup(parse_search("alice@example.com"))

    # GitHub should have been queried (depth=1 allows one resolver hop)
    github_source.search.assert_called()
    # Deep source should NOT have been queried (depth exhausted)
    deep_source.search.assert_not_called()


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cycle_detection():
    """A→B→A resolver cycle does not loop infinitely."""
    key_a = make_source_key(
        FP_ALICE,
        source_name="source-a",
        source_priority=10,
        email="alice@example.com",
        link_to_b="bob",
    )
    key_b = make_source_key(
        FP_BOB,
        source_name="source-b",
        source_priority=20,
        email="bob@example.com",
        link_to_a="alice@example.com",
    )

    source_a = make_mock_source(
        name="source-a",
        priority=10,
        fields=["email", "link_to_b"],
        search_result=[key_a],
    )
    source_b = make_mock_source(
        name="source-b",
        priority=20,
        fields=["username", "link_to_a"],
        search_result=[key_b],
    )

    resolver_a_to_b = ConfigResolver(
        ResolverConfig(
            name="a-to-b",
            trigger_source="source-a",
            trigger_field="link_to_b",
            target_source="source-b",
            target_field="username",
        )
    )
    resolver_b_to_a = ConfigResolver(
        ResolverConfig(
            name="b-to-a",
            trigger_source="source-b",
            trigger_field="link_to_a",
            target_source="source-a",
            target_field="email",
        )
    )

    orch = make_orchestrator(
        sources={"source-a": source_a, "source-b": source_b},
        resolvers=[resolver_a_to_b, resolver_b_to_a],
        max_depth=5,  # high depth to ensure cycle detection (not depth limit) stops it
    )

    # Should complete without infinite recursion
    results = await orch.lookup(parse_search("alice@example.com"))
    assert len(results) >= 1  # at least Alice's key


# ---------------------------------------------------------------------------
# Priority deduplication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_priority_deduplication():
    """Same fingerprint from two sources → only the lower-priority-number version returned."""
    ldap_key = make_source_key(FP_ALICE, source_name="ldap", source_priority=10)
    github_key = make_source_key(FP_ALICE, source_name="github", source_priority=50)

    ldap_source = make_mock_source(name="ldap", priority=10, search_result=[ldap_key])
    github_source = make_mock_source(name="github", priority=50, search_result=[github_key])

    orch = make_orchestrator(sources={"ldap": ldap_source, "github": github_source})

    results = await orch.lookup(parse_search("alice@example.com"))

    # Only one result (deduplicated by fingerprint)
    assert len(results) == 1
    assert results[0].source_name == "ldap"
    assert results[0].source_priority == 10


# ---------------------------------------------------------------------------
# get_key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_key_returns_highest_priority():
    """get_key returns the single highest-priority result."""
    ldap_key = make_source_key(FP_ALICE, source_name="ldap", source_priority=10)
    source = make_mock_source(name="ldap", priority=10, search_result=[ldap_key])
    orch = make_orchestrator(sources={"ldap": source})

    result = await orch.get_key(parse_search("alice@example.com"))
    assert result is not None
    assert result.fingerprint == FP_ALICE


@pytest.mark.asyncio
async def test_get_key_returns_none_when_not_found():
    source = make_mock_source(name="ldap", search_result=[])
    orch = make_orchestrator(sources={"ldap": source})

    result = await orch.get_key(parse_search("nobody@example.com"))
    assert result is None
