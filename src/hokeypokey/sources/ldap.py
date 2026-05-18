"""LDAP key source plugin — stub (implemented in Task 5.1)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hokeypokey.sources.base import KeySource

if TYPE_CHECKING:
    from hokeypokey.models import FieldDefinition, SourceKey


class LDAPSource(KeySource):
    """Stub — full implementation in Task 5.1."""

    async def search(self, query: str, field: str = "email") -> list[SourceKey]:
        raise NotImplementedError

    async def fetch_by_fingerprint(self, fingerprint: str) -> SourceKey | None:
        raise NotImplementedError

    async def check_freshness(self, fingerprint: str, token: str) -> bool:
        raise NotImplementedError

    def searchable_fields(self) -> list[FieldDefinition]:
        raise NotImplementedError

    async def close(self) -> None:
        pass
