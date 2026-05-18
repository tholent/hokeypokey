"""LDAP key source plugin."""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
from typing import Any

import pgpy
from ldap3 import BASE, Connection, Server, SUBTREE, Tls
from ldap3.utils.conv import escape_filter_chars

from hokeypokey.models import FieldDefinition, SearchResult, SourceKey, SourceMetadata
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
    ``tls_verify``      Validate the server's TLS certificate (default: ``true``).
                        Set to ``false`` only for self-signed certs in dev/test.
    ``tls_ca_file``     Path to a CA bundle file for custom certificate authorities
                        (optional; uses the system CA bundle by default).
    ``fields``          Mapping of logical field name → LDAP attribute name
    ==================  ============================================================

    Freshness token format: ``"<entry_dn>|||<modifyTimestamp>"``

    Thread safety
    -------------
    ``_ldap_search()`` is called via ``asyncio.to_thread()``, which means multiple
    concurrent requests will execute it in separate threads.  To avoid sharing a
    single ``ldap3.Connection`` across threads (which is not thread-safe), a fresh
    ``Connection`` is created for every ``_ldap_search()`` call and unbound in a
    ``finally`` block.  The ``ldap3.Server`` object is created once in ``__init__``
    and shared — it is stateless and safe to reuse across threads.
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

        # TLS configuration — applied when the URI scheme is ldaps://.
        # Plain ldap:// URIs do not use TLS and ignore these settings.
        tls_config: Tls | None = None
        if self._uri.lower().startswith("ldaps://"):
            tls_verify: bool = bool(config.get("tls_verify", True))
            tls_ca_file: str | None = config.get("tls_ca_file")
            validate = ssl.CERT_REQUIRED if tls_verify else ssl.CERT_NONE
            tls_config = Tls(validate=validate, ca_certs_file=tls_ca_file)

        # Shared, stateless Server object — safe to reuse across threads.
        # Connection objects are created per-call in _ldap_search().
        self._server = Server(self._uri, tls=tls_config)

    # ------------------------------------------------------------------
    # KeySource interface
    # ------------------------------------------------------------------

    def searchable_fields(self) -> list[FieldDefinition]:
        return [
            FieldDefinition(name=logical, source_attribute=ldap_attr, searchable=True)
            for logical, ldap_attr in self._fields.items()
        ]

    async def search(self, query: str, field: str = "email") -> list[SourceKey] | SearchResult:
        """Search LDAP for entries matching *query* in *field*.

        Constructs a compound LDAP filter combining the base filter with a
        field-specific equality assertion.  The query value is escaped to
        prevent LDAP filter injection.

        Returns a :class:`~hokeypokey.models.SearchResult` containing both
        keys (entries with PGP key data) and metadata-only entries (entries
        that matched the query but had no PGP key).  The metadata-only entries
        can still trigger cross-source resolvers.
        """
        ldap_attr = self._fields.get(field)
        if ldap_attr is None:
            logger.debug("LDAP source %r has no mapping for field %r", self.name, field)
            return SearchResult()

        escaped_query = escape_filter_chars(query)
        # Search for entries matching the field, regardless of whether they have a PGP key.
        # We drop the base_filter (which typically requires pgpKey=*) so that entries
        # WITHOUT a PGP key are still found — their metadata can trigger resolvers.
        search_filter = f"({ldap_attr}={escaped_query})"

        # Build the attribute list: mapped fields + modifyTimestamp are always
        # requested.  The key_attribute is requested separately so we can retry
        # without it if the LDAP server doesn't recognise it (e.g. the attribute
        # type isn't in the schema yet).
        metadata_attrs = list(self._fields.values()) + ["modifyTimestamp"]
        attrs_with_key = [self._key_attribute] + metadata_attrs

        try:
            entries = await asyncio.to_thread(
                self._ldap_search, self._base_dn, SUBTREE, search_filter, attrs_with_key
            )
        except Exception as exc:
            # If the search fails (e.g. "invalid attribute type" for the key
            # attribute), retry requesting only the metadata attributes.
            # This lets keyless LDAP entries still trigger resolvers even when
            # the PGP key attribute doesn't exist in the schema.
            exc_msg = str(exc).lower()
            if "invalid attribute" in exc_msg or "undefined attribute" in exc_msg:
                logger.info(
                    "LDAP source %r: key attribute %r not in schema, "
                    "retrying without it (metadata-only)",
                    self.name, self._key_attribute,
                )
                try:
                    entries = await asyncio.to_thread(
                        self._ldap_search, self._base_dn, SUBTREE, search_filter, metadata_attrs
                    )
                except Exception as exc2:
                    logger.warning("LDAP search retry failed for source %r: %s", self.name, exc2)
                    return SearchResult()
            else:
                logger.warning("LDAP search failed for source %r: %s", self.name, exc)
                return SearchResult()

        return self._entries_to_search_result(entries)

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

        search_result = self._entries_to_search_result(entries)
        return search_result.keys[0] if search_result.keys else None

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
        """No-op: connections are created per-call and unbound immediately after use."""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ldap_search(
        self,
        base: str,
        scope,
        search_filter: str,
        attributes: list[str],
    ) -> list[dict[str, Any]]:
        """Execute a synchronous LDAP search and return a list of attribute dicts.

        This method is intended to be called via ``asyncio.to_thread()``.  A new
        ``Connection`` is created for each call so that concurrent invocations in
        separate threads never share mutable connection state.
        """
        conn = Connection(
            self._server,
            user=self._bind_dn,
            password=self._bind_password,
            auto_bind=True,
            read_only=True,
        )
        try:
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
        finally:
            try:
                conn.unbind()
            except Exception:
                pass

    def _extract_metadata(self, entry: dict[str, Any]) -> dict[str, str]:
        """Extract metadata from an LDAP entry using the configured field mappings."""
        metadata: dict[str, str] = {}
        for logical_name, ldap_attr in self._fields.items():
            val = entry.get(ldap_attr)
            if isinstance(val, list):
                val = val[0] if val else None
            if val is not None:
                metadata[logical_name] = str(val)
        return metadata

    def _entries_to_search_result(self, entries: list[dict[str, Any]]) -> SearchResult:
        """Convert raw LDAP entry dicts to a :class:`SearchResult`.

        Entries with a PGP key become :class:`SourceKey` objects.
        Entries without a PGP key (but with metadata) become
        :class:`SourceMetadata` objects — these can still trigger resolvers.
        """
        result = SearchResult()

        for entry in entries:
            metadata = self._extract_metadata(entry)

            raw_key = entry.get(self._key_attribute)

            # Handle list values (multi-valued attributes)
            if isinstance(raw_key, list):
                raw_key = raw_key[0] if raw_key else None

            # Ensure it's a string if present
            if isinstance(raw_key, bytes):
                try:
                    raw_key = raw_key.decode("utf-8")
                except Exception:
                    raw_key = None

            if raw_key:
                # Entry has a PGP key — try to parse it
                try:
                    pgp_key, _ = pgpy.PGPKey.from_blob(raw_key)
                    fingerprint = str(pgp_key.fingerprint).replace(" ", "").upper()
                except Exception as exc:
                    logger.warning(
                        "Failed to parse PGP key from LDAP entry %r: %s",
                        entry.get("_dn"), exc,
                    )
                    # Key is unparseable — treat as metadata-only
                    if metadata:
                        result.metadata_only.append(SourceMetadata(
                            metadata=metadata,
                            source_name=self.name,
                            source_priority=self.priority,
                        ))
                    continue

                # Build freshness token
                dn = entry.get("_dn", "")
                modify_ts = entry.get("modifyTimestamp") or ""
                if isinstance(modify_ts, list):
                    modify_ts = modify_ts[0] if modify_ts else ""
                freshness_token = f"{dn}{_FRESHNESS_SEP}{modify_ts}"

                result.keys.append(SourceKey(
                    fingerprint=fingerprint,
                    key_armor=raw_key,
                    metadata=metadata,
                    freshness_token=freshness_token,
                    source_name=self.name,
                    source_priority=self.priority,
                ))
            elif metadata:
                # No PGP key, but has metadata — can still trigger resolvers
                logger.debug(
                    "LDAP entry %r has no PGP key but has metadata: %s",
                    entry.get("_dn"), list(metadata.keys()),
                )
                result.metadata_only.append(SourceMetadata(
                    metadata=metadata,
                    source_name=self.name,
                    source_priority=self.priority,
                ))

        return result
