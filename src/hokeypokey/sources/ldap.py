"""LDAP key source plugin."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import pgpy
from ldap3 import ALL_ATTRIBUTES, BASE, Connection, Server, SUBTREE
from ldap3.utils.conv import escape_filter_chars

from hokeypokey.models import FieldDefinition, SourceKey
from hokeypokey.sources.base import KeySource

logger = logging.getLogger(__name__)

_FRESHNESS_SEP = "|||"


class LDAPSource(KeySource):
    """Key source that fetches GPG keys from an LDAP directory.

    Configuration keys (under ``[sources.config]``):

    ==================  ============================================================
    Key                 Description
    ==================  ============================================================
    ``uri``             LDAP URI, e.g. ``ldaps://ldap.corp.example.com``
    ``base_dn``         Search base DN
    ``bind_dn``         Bind DN (optional; omit for anonymous bind)
    ``bind_password_env`` Environment variable holding the bind password
    ``key_attribute``   LDAP attribute containing the PGP key (default: ``pgpKey``)
    ``search_filter``   Base LDAP filter applied to all searches
                        (default: ``(pgpKey=*)``)
    ``fingerprint_attribute`` LDAP attribute for fingerprint-based lookup
                        (optional; enables ``fetch_by_fingerprint``)
    ``fields``          Mapping of logical field name → LDAP attribute name
    ==================  ============================================================

    Freshness token format: ``"<entry_dn>|||<modifyTimestamp>"``
    """

    def __init__(self, name: str, priority: int, ttl: int, config: dict[str, Any]) -> None:
        super().__init__(name, priority, ttl, config)

        self._uri: str = config["uri"]
        self._base_dn: str = config["base_dn"]
        self._bind_dn: str | None = config.get("bind_dn")
        self._key_attribute: str = config.get("key_attribute", "pgpKey")
        self._base_filter: str = config.get("search_filter", f"({self._key_attribute}=*)")
        self._fingerprint_attribute: str | None = config.get("fingerprint_attribute")

        # Resolve bind password from environment
        self._bind_password: str | None = None
        pw_env = config.get("bind_password_env")
        if pw_env:
            self._bind_password = os.environ.get(pw_env)

        # Field mapping: logical name → LDAP attribute
        self._fields: dict[str, str] = dict(config.get("fields", {}))

        # Lazy connection — created on first use
        self._conn: Connection | None = None

    # ------------------------------------------------------------------
    # KeySource interface
    # ------------------------------------------------------------------

    def searchable_fields(self) -> list[FieldDefinition]:
        return [
            FieldDefinition(name=logical, source_attribute=ldap_attr, searchable=True)
            for logical, ldap_attr in self._fields.items()
        ]

    async def search(self, query: str, field: str = "email") -> list[SourceKey]:
        """Search LDAP for entries matching *query* in *field*.

        Constructs a compound LDAP filter combining the base filter with a
        field-specific equality assertion.  The query value is escaped to
        prevent LDAP filter injection.
        """
        ldap_attr = self._fields.get(field)
        if ldap_attr is None:
            logger.debug("LDAP source %r has no mapping for field %r", self.name, field)
            return []

        escaped_query = escape_filter_chars(query)
        search_filter = f"(&{self._base_filter}({ldap_attr}={escaped_query}))"

        attrs_to_fetch = (
            [self._key_attribute]
            + list(self._fields.values())
            + ["modifyTimestamp"]
        )

        try:
            entries = await asyncio.to_thread(
                self._ldap_search, self._base_dn, SUBTREE, search_filter, attrs_to_fetch
            )
        except Exception as exc:
            logger.warning("LDAP search failed for source %r: %s", self.name, exc)
            return []

        return self._entries_to_source_keys(entries)

    async def fetch_by_fingerprint(self, fingerprint: str) -> SourceKey | None:
        """Fetch a key by fingerprint, if the LDAP schema supports it."""
        if not self._fingerprint_attribute:
            return None

        escaped_fp = escape_filter_chars(fingerprint)
        search_filter = f"(&{self._base_filter}({self._fingerprint_attribute}={escaped_fp}))"

        attrs_to_fetch = (
            [self._key_attribute]
            + list(self._fields.values())
            + ["modifyTimestamp"]
        )

        try:
            entries = await asyncio.to_thread(
                self._ldap_search, self._base_dn, SUBTREE, search_filter, attrs_to_fetch
            )
        except Exception as exc:
            logger.warning("LDAP fingerprint fetch failed for source %r: %s", self.name, exc)
            return None

        keys = self._entries_to_source_keys(entries)
        return keys[0] if keys else None

    async def check_freshness(self, fingerprint: str, token: str) -> bool:
        """Check whether the LDAP entry's modifyTimestamp has changed.

        The token encodes both the entry DN and the last-seen modifyTimestamp
        as ``"<dn>|||<timestamp>"``.
        """
        if _FRESHNESS_SEP not in token:
            return False

        dn, old_timestamp = token.split(_FRESHNESS_SEP, 1)

        try:
            entries = await asyncio.to_thread(
                self._ldap_search, dn, BASE, "(objectClass=*)", ["modifyTimestamp"]
            )
        except Exception as exc:
            logger.warning("LDAP freshness check failed for source %r: %s", self.name, exc)
            # Assume fresh on error to avoid cascading failures
            return True

        if not entries:
            # Entry no longer exists — key was deleted
            return False

        current_timestamp = str(entries[0].get("modifyTimestamp", ""))
        return current_timestamp == old_timestamp

    async def close(self) -> None:
        """Unbind and close the LDAP connection."""
        if self._conn is not None:
            try:
                await asyncio.to_thread(self._conn.unbind)
            except Exception:
                pass
            self._conn = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_connection(self) -> Connection:
        """Return a live LDAP connection, reconnecting if necessary."""
        if self._conn is None or not self._conn.bound:
            server = Server(self._uri)
            self._conn = Connection(
                server,
                user=self._bind_dn,
                password=self._bind_password,
                auto_bind=True,
                read_only=True,
            )
        return self._conn

    def _ldap_search(
        self,
        base: str,
        scope,
        search_filter: str,
        attributes: list[str],
    ) -> list[dict[str, Any]]:
        """Execute a synchronous LDAP search and return a list of attribute dicts.

        This method is intended to be called via ``asyncio.to_thread()``.
        """
        conn = self._get_connection()
        conn.search(
            search_base=base,
            search_filter=search_filter,
            search_scope=scope,
            attributes=attributes,
        )
        results = []
        for entry in conn.entries:
            row: dict[str, Any] = {"_dn": entry.entry_dn}
            for attr in attributes:
                try:
                    val = getattr(entry, attr)
                    # ldap3 attribute values are wrapped objects; .value gives the raw value
                    row[attr] = val.value if val else None
                except Exception:
                    row[attr] = None
            results.append(row)
        return results

    def _entries_to_source_keys(self, entries: list[dict[str, Any]]) -> list[SourceKey]:
        """Convert raw LDAP entry dicts to :class:`SourceKey` objects."""
        keys: list[SourceKey] = []
        for entry in entries:
            raw_key = entry.get(self._key_attribute)
            if not raw_key:
                continue

            # Handle list values (multi-valued attributes)
            if isinstance(raw_key, list):
                raw_key = raw_key[0] if raw_key else None
            if not raw_key:
                continue

            # Ensure it's a string
            if isinstance(raw_key, bytes):
                try:
                    raw_key = raw_key.decode("utf-8")
                except Exception:
                    continue

            # Parse the key to get the fingerprint
            try:
                pgp_key, _ = pgpy.PGPKey.from_blob(raw_key)
                fingerprint = str(pgp_key.fingerprint).replace(" ", "").upper()
            except Exception as exc:
                logger.warning("Failed to parse PGP key from LDAP entry %r: %s", entry.get("_dn"), exc)
                continue

            # Build metadata from configured field mappings
            metadata: dict[str, str] = {}
            for logical_name, ldap_attr in self._fields.items():
                val = entry.get(ldap_attr)
                if isinstance(val, list):
                    val = val[0] if val else None
                if val is not None:
                    metadata[logical_name] = str(val)

            # Build freshness token: "<dn>|||<modifyTimestamp>"
            dn = entry.get("_dn", "")
            modify_ts = entry.get("modifyTimestamp") or ""
            if isinstance(modify_ts, list):
                modify_ts = modify_ts[0] if modify_ts else ""
            freshness_token = f"{dn}{_FRESHNESS_SEP}{modify_ts}"

            keys.append(SourceKey(
                fingerprint=fingerprint,
                key_armor=raw_key,
                metadata=metadata,
                freshness_token=freshness_token,
                source_name=self.name,
                source_priority=self.priority,
            ))

        return keys
