"""Search orchestrator — coordinates cache, sources, and resolvers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hokeypokey.models import (
    CachedKey,
    ParsedSearch,
    ResolvedQuery,
    SearchResult,
    SearchType,
    SourceKey,
    SourceMetadata,
)

if TYPE_CHECKING:
    from hokeypokey.cache import KeyCache
    from hokeypokey.resolver import ConfigResolver
    from hokeypokey.sources.base import KeySource

logger = logging.getLogger(__name__)


@dataclass
class _FanOutResult:
    """Keys and metadata collected from a fan-out across sources."""

    keys: list[SourceKey]
    metadata: list[SourceMetadata]


class SearchOrchestrator:
    """Coordinates lazy key lookups across multiple sources with caching.

    Responsibilities
    ----------------
    1. Check the cache first; serve fresh entries immediately.
    2. Revalidate stale entries via lightweight source-specific freshness checks.
    3. Fan out cache-miss queries to all relevant sources concurrently.
    4. Run configured resolvers to chain cross-source lookups.
    5. Deduplicate results by fingerprint (priority-aware — handled by the cache).
    6. Return results sorted by source priority (lowest number = most authoritative).

    Cycle prevention
    ----------------
    The orchestrator tracks ``(source_name, field, value)`` tuples that have
    already been queried within a single :meth:`lookup` call.  If a resolver
    would produce a query for a tuple already in the visited set, it is skipped.
    Additionally, resolver chaining is limited to *max_depth* levels.
    """

    def __init__(
        self,
        sources: dict[str, KeySource],
        cache: KeyCache,
        resolvers: list[ConfigResolver],
        max_depth: int = 2,
    ) -> None:
        self._sources = sources
        self._cache = cache
        self._resolvers = resolvers
        self._max_depth = max_depth

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def lookup(self, parsed: ParsedSearch) -> list[SourceKey]:
        """Return all keys matching *parsed*, using cache + sources + resolvers.

        Args:
            parsed: A classified and normalised search term.

        Returns:
            Deduplicated list of :class:`~hokeypokey.models.SourceKey` objects
            sorted by source priority (lowest number first).
        """
        visited: set[tuple[str, str, str]] = set()
        collected_fps: set[str] = set()

        await self._query(
            parsed, depth=self._max_depth, visited=visited, collected_fps=collected_fps
        )

        # Gather results from cache, sorted by priority.
        # collected_fps is mutated in-place by _query / _run_resolver_query
        # (SonarQube S2583 false positive: it doesn't track cross-method set mutations).
        # The `entry is not None` guard is also real: LRU eviction can drop an entry
        # between it being added to collected_fps and this read-back.
        results: list[SourceKey] = []
        for fp in collected_fps:  # NOSONAR
            entry = self._cache.get_by_fingerprint(fp)
            if entry is not None:
                results.append(entry.source_key)

        results.sort(key=lambda k: k.source_priority)
        return results

    async def get_key(self, parsed: ParsedSearch) -> SourceKey | None:
        """Return the single highest-priority key matching *parsed*, or ``None``.

        Args:
            parsed: A classified and normalised search term.
        """
        results = await self.lookup(parsed)
        return results[0] if results else None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _query(
        self,
        parsed: ParsedSearch,
        depth: int,
        visited: set[tuple[str, str, str]],
        collected_fps: set[str],
    ) -> None:
        """Core recursive query method.

        Checks cache, fans out to sources on miss, then runs resolvers.
        """
        # ---- 1. Cache check ----
        cached_entries = self._cache_lookup(parsed)

        fresh_fps: set[str] = set()
        stale_entries = []

        for entry in cached_entries:
            fp = entry.source_key.fingerprint
            if entry.is_fresh:
                fresh_fps.add(fp)
                collected_fps.add(fp)
            else:
                stale_entries.append(entry)

        # ---- 2. Revalidate stale entries ----
        if stale_entries:
            await self._revalidate_stale_entries(stale_entries, collected_fps)

        # ---- 3. Cache miss — fan out to sources ----
        if not fresh_fps and not stale_entries:
            new_keys, new_metadata = await self._cache_miss_fan_out(parsed, visited, collected_fps)
        else:
            new_keys, new_metadata = [], []

        # ---- 4. Resolver pass (only if we have depth remaining) ----
        if depth <= 0:
            return

        # Collect keys from: newly fetched, fresh cache hits, and metadata-only results.
        all_keys_for_resolvers: list[SourceKey] = list(new_keys)
        for fp in fresh_fps:
            cached = self._cache.get_by_fingerprint(fp)
            if cached is not None:
                all_keys_for_resolvers.append(cached.source_key)

        resolver_queries = self._collect_resolver_queries(
            all_keys_for_resolvers,
            visited,
            extra_metadata=new_metadata,
        )

        if resolver_queries:
            resolver_tasks = [
                self._run_resolver_query(
                    rq, depth=depth, visited=visited, collected_fps=collected_fps
                )
                for rq in resolver_queries
            ]
            await asyncio.gather(*resolver_tasks, return_exceptions=True)

    async def _revalidate_stale_entries(
        self,
        stale_entries: list[CachedKey],
        collected_fps: set[str],
    ) -> None:
        """Revalidate stale cache entries, refetching or evicting as needed."""
        revalidation_tasks = [self._revalidate(entry) for entry in stale_entries]
        revalidated = await asyncio.gather(*revalidation_tasks, return_exceptions=True)
        for entry, result in zip(stale_entries, revalidated, strict=True):
            fp = entry.source_key.fingerprint
            if isinstance(result, Exception):
                logger.warning("Freshness check failed for %s: %s", fp, result)
                collected_fps.add(fp)  # serve stale rather than nothing
            elif result:
                entry.touch()  # still fresh — touch the TTL clock
                collected_fps.add(fp)
            else:
                await self._refetch_stale(fp, entry.source_key.source_name, collected_fps)

    async def _refetch_stale(
        self,
        fp: str,
        source_name: str,
        collected_fps: set[str],
    ) -> None:
        """Attempt to refetch a key that failed its freshness check."""
        source = self._sources.get(source_name)
        if source is None:
            return
        try:
            new_key = await source.fetch_by_fingerprint(fp)
            if new_key is not None:
                self._cache.put(new_key, ttl=source.ttl)
                collected_fps.add(fp)
            else:
                self._cache.remove(fp)  # key no longer exists in source
        except Exception as exc:
            logger.warning("Refetch failed for %s: %s", fp, exc)
            collected_fps.add(fp)  # serve stale

    async def _cache_miss_fan_out(
        self,
        parsed: ParsedSearch,
        visited: set[tuple[str, str, str]],
        collected_fps: set[str],
    ) -> tuple[list[SourceKey], list[SourceMetadata]]:
        """Fan out to all sources on a complete cache miss and cache the results."""
        fan_out_result = await self._fan_out(parsed, visited)
        for key in fan_out_result.keys:
            source = self._sources.get(key.source_name)
            ttl = source.ttl if source is not None else 600
            self._cache.put(key, ttl=ttl)
            collected_fps.add(key.fingerprint)
        return fan_out_result.keys, fan_out_result.metadata

    async def _run_resolver_query(
        self,
        rq: ResolvedQuery,
        depth: int,
        visited: set[tuple[str, str, str]],
        collected_fps: set[str],
    ) -> None:
        """Execute a single resolver-generated cross-source query.

        *depth* is the number of additional resolver hops still allowed after
        this one.  depth=1 means: execute this query, but do not chain further.
        depth=0 means: do not execute at all.
        """
        if depth <= 0:
            return

        source = self._sources.get(rq.target_source)
        if source is None:
            logger.warning("Resolver target source %r not found", rq.target_source)
            return

        try:
            result = await source.search(rq.search_value, rq.search_field)
        except Exception as exc:
            logger.warning(
                "Resolver source query failed for %s/%s: %s",
                rq.target_source,
                rq.search_field,
                exc,
            )
            return

        if isinstance(result, SearchResult):
            keys = result.keys
            extra_metadata = result.metadata_only
        else:
            keys = []
            extra_metadata = []

        for key in keys:
            self._cache.put(key, ttl=source.ttl)
            collected_fps.add(key.fingerprint)

        # Run further resolvers on the results (depth - 1)
        further_queries = self._collect_resolver_queries(
            keys, visited, extra_metadata=extra_metadata
        )
        if further_queries:
            tasks = [
                self._run_resolver_query(
                    fq, depth=depth - 1, visited=visited, collected_fps=collected_fps
                )
                for fq in further_queries
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

    def _cache_lookup(self, parsed: ParsedSearch) -> list[CachedKey]:
        """Return cached entries matching *parsed* (may be fresh or stale)."""
        if parsed.search_type == SearchType.FINGERPRINT:
            entry = self._cache.get_by_fingerprint(parsed.normalized)
            return [entry] if entry is not None else []

        if parsed.search_type in (SearchType.LONG_KEY_ID, SearchType.SHORT_KEY_ID):
            return self._cache.get_by_key_id(parsed.normalized)

        if parsed.search_type == SearchType.EMAIL:
            return self._cache.search(parsed.normalized, "email")

        if parsed.search_type == SearchType.TEXT:
            return self._text_search_cache(parsed.normalized)

        return []

    def _text_search_cache(self, normalized: str) -> list[CachedKey]:
        """Search cache indexes for all text-searchable fields (excluding email)."""
        results: list[CachedKey] = []
        seen: set[str] = set()
        for source in self._sources.values():
            for field_def in source.searchable_fields():
                if field_def.name == "email" or not field_def.text_searchable:
                    continue
                for entry in self._cache.search(normalized, field_def.name):
                    if entry.source_key.fingerprint not in seen:
                        results.append(entry)
                        seen.add(entry.source_key.fingerprint)
        return results

    async def _revalidate(self, entry: CachedKey) -> bool:
        """Ask the originating source whether *entry* is still fresh."""
        source = self._sources.get(entry.source_key.source_name)
        if source is None:
            return False
        return await source.check_freshness(
            entry.source_key.fingerprint,
            entry.freshness_token,
        )

    async def _fan_out(
        self,
        parsed: ParsedSearch,
        visited: set[tuple[str, str, str]],
    ) -> _FanOutResult:
        """Query all relevant sources concurrently for *parsed*.

        Returns both keys (with PGP data) and metadata-only entries
        (e.g. LDAP entries that match but have no PGP key).
        """
        if parsed.search_type in (SearchType.LONG_KEY_ID, SearchType.SHORT_KEY_ID):
            # Key ID lookups are cache-only; sources don't index by key ID
            return _FanOutResult(keys=[], metadata=[])

        tasks = self._build_source_tasks(parsed, visited)
        if not tasks:
            return _FanOutResult(keys=[], metadata=[])

        raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        return self._collect_fan_out_results(raw_results)

    def _build_source_tasks(
        self,
        parsed: ParsedSearch,
        visited: set[tuple[str, str, str]],
    ) -> list[Coroutine[Any, Any, SourceKey | None | SearchResult]]:
        """Build the list of source coroutines to execute for *parsed*."""
        tasks: list[Coroutine[Any, Any, SourceKey | None | SearchResult]] = []
        for source in self._sources.values():
            if parsed.search_type == SearchType.FINGERPRINT:
                visit_key = (source.name, "__fingerprint__", parsed.normalized)
                if visit_key not in visited:
                    visited.add(visit_key)
                    tasks.append(source.fetch_by_fingerprint(parsed.normalized))
            elif parsed.search_type == SearchType.EMAIL:
                # Only query sources that declare an "email" field
                field_names = {f.name for f in source.searchable_fields()}
                if "email" not in field_names:
                    continue
                visit_key = (source.name, "email", parsed.normalized)
                if visit_key not in visited:
                    visited.add(visit_key)
                    tasks.append(source.search(parsed.normalized, "email"))
            elif parsed.search_type == SearchType.TEXT:
                # Fields with text_searchable=False (e.g. github_username) are only
                # reachable via resolvers or explicit field-qualified queries.
                for field_def in source.searchable_fields():
                    if not field_def.text_searchable:
                        continue
                    visit_key = (source.name, field_def.name, parsed.normalized)
                    if visit_key not in visited:
                        visited.add(visit_key)
                        tasks.append(source.search(parsed.normalized, field_def.name))
        return tasks

    def _collect_fan_out_results(
        self,
        results: list[Any],
    ) -> _FanOutResult:
        """Flatten asyncio.gather results into a _FanOutResult."""
        keys: list[SourceKey] = []
        metadata: list[SourceMetadata] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning("Source query failed: %s", result)
            elif isinstance(result, SearchResult):
                keys.extend(result.keys)
                metadata.extend(result.metadata_only)
            elif isinstance(result, SourceKey):
                keys.append(result)
        return _FanOutResult(keys=keys, metadata=metadata)

    def _collect_resolver_queries(
        self,
        keys: list[SourceKey],
        visited: set[tuple[str, str, str]],
        extra_metadata: list[SourceMetadata] | None = None,
    ) -> list[ResolvedQuery]:
        """Evaluate all resolvers against *keys* and *extra_metadata*.

        Resolvers fire on metadata from two sources:
        1. Keys with PGP data (``keys``) — their ``.metadata`` dict is checked.
        2. Metadata-only entries (``extra_metadata``) — e.g. LDAP entries that
           matched the query but had no PGP key.  These can still trigger
           resolvers (the whole point of cross-source resolution).
        """
        new_queries: list[ResolvedQuery] = []

        # Build a unified list of (metadata_dict, source_name) tuples
        metadata_items: list[tuple[dict[str, str], str]] = [
            (key.metadata, key.source_name) for key in keys
        ]
        for meta in extra_metadata or []:
            metadata_items.append((meta.metadata, meta.source_name))

        for metadata, source_name in metadata_items:
            for resolver in self._resolvers:
                if not resolver.can_resolve(metadata, source_name):
                    continue
                for resolved_query in resolver.resolve(metadata):
                    visit_key = (
                        resolved_query.target_source,
                        resolved_query.search_field,
                        resolved_query.search_value,
                    )
                    if visit_key in visited:
                        continue
                    visited.add(visit_key)
                    new_queries.append(
                        ResolvedQuery(
                            target_source=resolved_query.target_source,
                            search_field=resolved_query.search_field,
                            search_value=resolved_query.search_value,
                        )
                    )

        return new_queries
