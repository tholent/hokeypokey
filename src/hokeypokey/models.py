"""Core data models shared across all hokeypokey components."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class SearchType(Enum):
    """Classification of an HKP search term."""

    FINGERPRINT = "fingerprint"   # 40-hex V4 fingerprint (0x prefix stripped)
    LONG_KEY_ID = "long_key_id"   # 16-hex long key ID
    SHORT_KEY_ID = "short_key_id" # 8-hex short key ID (accepted but discouraged)
    EMAIL = "email"               # email address (contains @)
    TEXT = "text"                 # free-text UID search


@dataclass
class ParsedSearch:
    """A parsed and normalised HKP search term."""

    search_type: SearchType
    raw: str        # original value as received from the client
    normalized: str # canonical form: uppercase hex for IDs/fingerprints, lowercase for email


@dataclass
class FieldDefinition:
    """Declares a searchable field that a source exposes.

    ``name`` is the logical field name used throughout hokeypokey (e.g. ``"email"``).
    ``source_attribute`` is the source-specific attribute name (e.g. ``"mail"`` in LDAP).
    ``searchable`` controls whether the field is included in the search index.
    ``text_searchable`` controls whether the field participates in unqualified
    TEXT searches (e.g. searching for ``"wells"`` without specifying a field).
    Fields like ``email`` and ``username`` are text-searchable because a bare
    text query could plausibly match them.  Fields like ``github_username`` are
    NOT text-searchable because they should only be reached via resolvers or
    explicit field-qualified searches.
    """

    name: str
    source_attribute: str
    searchable: bool = True
    text_searchable: bool = True


@dataclass
class SourceKey:
    """A GPG public key as returned by a source plugin.

    Attributes:
        fingerprint:      Uppercase hex fingerprint, no ``0x`` prefix.
        key_armor:        ASCII-armored public key block.
        metadata:         Arbitrary key/value metadata from the source
                          (e.g. ``{"email": "alice@example.com", "github_id": "octocat"}``).
        freshness_token:  Source-specific opaque string used to check staleness later
                          (e.g. LDAP ``modifyTimestamp``, GitHub ``ETag``).
        source_name:      Name of the source that produced this key.
        source_priority:  Numeric priority of the source (lower = more authoritative).
    """

    fingerprint: str
    key_armor: str
    metadata: dict[str, str]
    freshness_token: str
    source_name: str
    source_priority: int


@dataclass
class CachedKey:
    """A ``SourceKey`` stored in the key cache with TTL bookkeeping.

    Attributes:
        source_key:      The underlying key and its metadata.
        cached_at:       Unix timestamp of when the key was last fetched/validated.
        ttl:             Seconds before the cached entry should be revalidated.
        freshness_token: Copied from ``source_key.freshness_token`` for convenience;
                         updated in-place when a freshness check confirms the key is
                         still current without a full refetch.
    """

    source_key: SourceKey
    cached_at: float
    ttl: float
    freshness_token: str = field(init=False)

    def __post_init__(self) -> None:
        self.freshness_token = self.source_key.freshness_token

    @property
    def is_fresh(self) -> bool:
        """Return True if the entry is still within its TTL window."""
        return (self.cached_at + self.ttl) > time.time()

    def touch(self) -> None:
        """Reset the TTL clock without changing the key data (used after a successful freshness check)."""
        self.cached_at = time.time()


@dataclass
class SourceMetadata:
    """Metadata from a source entry that has no PGP key but may trigger resolvers.

    This represents an LDAP entry (or similar) that was found by the search query
    and contains useful metadata (e.g. a ``github_id``), but does not contain a
    PGP key itself.  The orchestrator uses this to fire cross-source resolvers.

    Attributes:
        metadata:        Arbitrary key/value metadata from the source
                         (e.g. ``{"github_id": "octocat", "email": "alice@example.com"}``).
        source_name:     Name of the source that produced this metadata.
        source_priority: Numeric priority of the source.
    """

    metadata: dict[str, str]
    source_name: str
    source_priority: int


@dataclass
class SearchResult:
    """Combined result from a source search: keys found + metadata-only entries.

    A source may find LDAP entries that match the query but have no PGP key.
    Those entries still carry metadata (e.g. ``github_id``) that can trigger
    cross-source resolvers.  This dataclass bundles both kinds of results.
    """

    keys: list[SourceKey] = field(default_factory=list)
    metadata_only: list[SourceMetadata] = field(default_factory=list)


@dataclass
class ResolvedQuery:
    """A cross-source query produced by a ``SearchResolver``.

    Instructs the orchestrator to search ``target_source`` for ``search_value``
    in the field named ``search_field``.
    """

    target_source: str
    search_field: str
    search_value: str
