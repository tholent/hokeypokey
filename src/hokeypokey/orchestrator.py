"""Search orchestrator — coordinates cache, sources, and resolvers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hokeypokey.models import ParsedSearch, SearchType, SourceKey

if TYPE_CHECKING:
    from hokeypokey.cache import KeyCache
    from hokeypokey.resolver import ConfigResolver
    from hokeypokey.sources.base import KeySource

logger = logging.getLogger(__name__)


@dataclass
class _ResolverQuery:
    """A cross-source query produced by a resolver, ready to execute."""

    target_source: str
    search_field: str
    search_value: str


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

        await self._query(parsed, depth=self._max_depth, visited=visited, collected_fps=collected_fps)

        # Gather results from cache, sorted by priority
        results: list[SourceKey] = []
        for fp in collected_fps:
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
            revalidation_tasks = [
                self._revalidate(entry) for entry in stale_entries
            ]
            revalidated = await asyncio.gather(*revalidation_tasks, return_exceptions=True)
            for entry, result in zip(stale_entries, revalidated):
                fp = entry.source_key.fingerprint
                if isinstance(result, Exception):
                    logger.warning("Freshness check failed for %s: %s", fp, result)
                    # Serve stale rather than nothing
                    collected_fps.add(fp)
                elif result:
                    # Still fresh — touch the TTL clock
                    entry.touch()
                    collected_fps.add(fp)
                else:
                    # Stale — refetch
                    source = self._sources.get(entry.source_key.source_name)
                    if source is not None:
                        try:
                            new_key = await source.fetch_by_fingerprint(fp)
                            if new_key is not None:
                                self._cache.put(new_key, ttl=source.ttl)
                                collected_fps.add(fp)
                            else:
                                # Key no longer exists in source — remove from cache
                                self._cache.remove(fp)
                        except Exception as exc:
                            logger.warning("Refetch failed for %s: %s", fp, exc)
                            collected_fps.add(fp)  # serve stale

        # ---- 3. Cache miss — fan out to sources ----
        # Only fan out if we didn't get anything from cache for this query
        if not fresh_fps and not stale_entries:
            new_keys = await self._fan_out(parsed, visited)
            for key in new_keys:
                source = self._sources.get(key.source_name)
                ttl = source.ttl if source is not None else 600
                self._cache.put(key, ttl=ttl)
                collected_fps.add(key.fingerprint)
        else:
            new_keys = []

        # ---- 4. Resolver pass (only if we have depth remaining) ----
        if depth <= 0:
            return

        # Collect all metadata from newly fetched keys + fresh cache hits
        all_keys_for_resolvers: list[SourceKey] = list(new_keys)
        for fp in fresh_fps:
            entry = self._cache.get_by_fingerprint(fp)
            if entry is not None:
                all_keys_for_resolvers.append(entry.source_key)

        resolver_queries = self._collect_resolver_queries(all_keys_for_resolvers, visited)

        if resolver_queries:
            resolver_tasks = [
                self._run_resolver_query(rq, depth=depth, visited=visited, collected_fps=collected_fps)
                for rq in resolver_queries
            ]
            await asyncio.gather(*resolver_tasks, return_exceptions=True)

    async def _run_resolver_query(
        self,
        rq: _ResolverQuery,
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
            keys = await source.search(rq.search_value, rq.search_field)
        except Exception as exc:
            logger.warning("Resolver source query failed for %s/%s: %s", rq.target_source, rq.search_field, exc)
            return

        for key in keys:
            self._cache.put(key, ttl=source.ttl)
            collected_fps.add(key.fingerprint)

        # Run further resolvers on the results (depth - 1)
        further_queries = self._collect_resolver_queries(keys, visited)
        if further_queries:
            tasks = [
                self._run_resolver_query(fq, depth=depth - 1, visited=visited, collected_fps=collected_fps)
                for fq in further_queries
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

    def _cache_lookup(self, parsed: ParsedSearch):
        """Return cached entries matching *parsed* (may be fresh or stale)."""
        from hokeypokey.models import CachedKey

        if parsed.search_type == SearchType.FINGERPRINT:
            entry = self._cache.get_by_fingerprint(parsed.normalized)
            return [entry] if entry is not None else []

        if parsed.search_type in (SearchType.LONG_KEY_ID, SearchType.SHORT_KEY_ID):
            return self._cache.get_by_key_id(parsed.normalized)

        if parsed.search_type == SearchType.EMAIL:
            return self._cache.search(parsed.normalized, "email")

        if parsed.search_type == SearchType.TEXT:
            # Text search: check all custom field indexes
            results: list[CachedKey] = []
            seen: set[str] = set()
            for source in self._sources.values():
                for field_def in source.searchable_fields():
                    if field_def.name == "email":
                        continue
                    for entry in self._cache.search(parsed.normalized, field_def.name):
                        if entry.source_key.fingerprint not in seen:
                            results.append(entry)
                            seen.add(entry.source_key.fingerprint)
            return results

        return []

    async def _revalidate(self, entry) -> bool:
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
    ) -> list[SourceKey]:
        """Query all relevant sources concurrently for *parsed*."""
        if parsed.search_type in (SearchType.LONG_KEY_ID, SearchType.SHORT_KEY_ID):
            # Key ID lookups are cache-only; sources don't index by key ID
            return []

        tasks = []

        for source in self._sources.values():
            if parsed.search_type == SearchType.FINGERPRINT:
                visit_key = (source.name, "__fingerprint__", parsed.normalized)
                if visit_key in visited:
                    continue
                visited.add(visit_key)
                tasks.append(source.fetch_by_fingerprint(parsed.normalized))

            elif parsed.search_type == SearchType.EMAIL:
                # Only query sources that declare an "email" field
                field_names = {f.name for f in source.searchable_fields()}
                if "email" not in field_names:
                    continue
                visit_key = (source.name, "email", parsed.normalized)
                if visit_key in visited:
                    continue
                visited.add(visit_key)
                tasks.append(source.search(parsed.normalized, "email"))

            elif parsed.search_type == SearchType.TEXT:
                # Query each source against each of its searchable fields
                for field_def in source.searchable_fields():
                    visit_key = (source.name, field_def.name, parsed.normalized)
                    if visit_key in visited:
                        continue
                    visited.add(visit_key)
                    tasks.append(source.search(parsed.normalized, field_def.name))

        if not tasks:
            return []

        results = await asyncio.gather(*tasks, return_exceptions=True)

        keys: list[SourceKey] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning("Source query failed: %s", result)
                continue
            if result is None:
                continue
            if isinstance(result, list):
                keys.extend(result)
            else:
                keys.append(result)

        return keys

    def _collect_resolver_queries(
        self,
        keys: list[SourceKey],
        visited: set[tuple[str, str, str]],
    ) -> list[_ResolverQuery]:
        """Evaluate all resolvers against *keys* and return resolver queries to execute."""
        new_queries: list[_ResolverQuery] = []

        for key in keys:
            for resolver in self._resolvers:
                if not resolver.can_resolve(key.metadata, key.source_name):
                    continue
                for resolved_query in resolver.resolve(key.metadata):
                    visit_key = (
                        resolved_query.target_source,
                        resolved_query.search_field,
                        resolved_query.search_value,
                    )
                    if visit_key in visited:
                        continue
                    visited.add(visit_key)
                    new_queries.append(
                        _ResolverQuery(
                            target_source=resolved_query.target_source,
                            search_field=resolved_query.search_field,
                            search_value=resolved_query.search_value,
                        )
                    )

        return new_queries
