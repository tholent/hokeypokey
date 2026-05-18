"""Abstract base class for hokeypokey key source plugins."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hokeypokey.models import FieldDefinition, SearchResult, SourceKey


class KeySource(ABC):
    """Abstract interface that every key source plugin must implement.

    A source is responsible for:
    - Searching for keys by a query against a named field (e.g. email, username)
    - Fetching a specific key by its fingerprint (when the source supports it)
    - Checking whether a previously-cached key is still fresh

    Sources are instantiated once at startup from configuration and reused for
    the lifetime of the server.  They run inside a single asyncio event loop,
    so there is no need for thread-safety within a source.
    """

    def __init__(
        self,
        name: str,
        priority: int,
        ttl: int,
        config: dict[str, Any],
    ) -> None:
        self._name = name
        self._priority = priority
        self._ttl = ttl
        self._config = config

    # ------------------------------------------------------------------
    # Properties (concrete — subclasses must not override these)
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Human-readable name of this source instance (from config)."""
        return self._name

    @property
    def priority(self) -> int:
        """Numeric priority — lower number means more authoritative."""
        return self._priority

    @property
    def ttl(self) -> int:
        """Cache TTL in seconds for keys fetched from this source."""
        return self._ttl

    # ------------------------------------------------------------------
    # Abstract interface — subclasses must implement all of these
    # ------------------------------------------------------------------

    @abstractmethod
    async def search(self, query: str, field: str = "email") -> SearchResult:
        """Search for keys matching *query* against the named *field*.

        Args:
            query: The search term (e.g. an email address, username, employee ID).
            field: The logical field name to search against.  Must be one of the
                   names declared by :meth:`searchable_fields`.

        Returns:
            A (possibly empty) list of matching :class:`~hokeypokey.models.SourceKey`
            objects, each carrying the key data, metadata, and a freshness token.
        """
        ...

    @abstractmethod
    async def fetch_by_fingerprint(self, fingerprint: str) -> SourceKey | None:
        """Fetch a specific key by its full fingerprint.

        Args:
            fingerprint: Uppercase hex fingerprint without ``0x`` prefix.

        Returns:
            The matching :class:`~hokeypokey.models.SourceKey`, or ``None`` if
            the source does not have a key with that fingerprint (or does not
            support fingerprint-based lookup).
        """
        ...

    @abstractmethod
    async def check_freshness(self, fingerprint: str, token: str) -> bool:
        """Check whether a previously-cached key is still current.

        This is a lightweight check — it should avoid fetching the full key
        data if possible (e.g. query only a timestamp attribute, or send a
        conditional HTTP request).

        Args:
            fingerprint: Uppercase hex fingerprint of the cached key.
            token:       The opaque freshness token stored when the key was
                         last fetched (e.g. LDAP ``modifyTimestamp``, HTTP ``ETag``).

        Returns:
            ``True`` if the cached version is still current (no refetch needed).
            ``False`` if the key has changed or been removed (caller should refetch).
        """
        ...

    @abstractmethod
    def searchable_fields(self) -> list[FieldDefinition]:
        """Declare the fields this source can search against.

        Returns:
            A list of :class:`~hokeypokey.models.FieldDefinition` objects
            describing each searchable field.
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """Release any resources held by this source (connections, clients, etc.)."""
        ...
