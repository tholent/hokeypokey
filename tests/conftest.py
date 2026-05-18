"""Shared test fixtures for hokeypokey integration tests."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pgpy
import pgpy.constants
import pytest

from hokeypokey.app import create_app
from hokeypokey.config import AppConfig, CacheConfig, ResolverConfig, ServerConfig, SourceConfig
from hokeypokey.models import FieldDefinition, SearchResult, SourceKey, SourceMetadata
from hokeypokey.sources.base import KeySource


# ---------------------------------------------------------------------------
# PGP key fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def alice_pgp_key():
    """RSA-2048 key for Alice <alice@example.com>."""
    key = pgpy.PGPKey.new(pgpy.constants.PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    uid = pgpy.PGPUID.new("Alice", email="alice@example.com")
    key.add_uid(
        uid,
        usage={pgpy.constants.KeyFlags.Sign, pgpy.constants.KeyFlags.EncryptCommunications},
        hashes=[pgpy.constants.HashAlgorithm.SHA256],
        ciphers=[pgpy.constants.SymmetricKeyAlgorithm.AES256],
        compression=[pgpy.constants.CompressionAlgorithm.ZLIB],
    )
    return key


@pytest.fixture(scope="session")
def alice_armor(alice_pgp_key):
    return str(alice_pgp_key.pubkey)


@pytest.fixture(scope="session")
def alice_fingerprint(alice_pgp_key):
    return str(alice_pgp_key.fingerprint).replace(" ", "").upper()


@pytest.fixture(scope="session")
def bob_pgp_key():
    """RSA-2048 key for Bob <bob@github.com> (simulates a GitHub key)."""
    key = pgpy.PGPKey.new(pgpy.constants.PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    uid = pgpy.PGPUID.new("Bob", email="bob@github.com")
    key.add_uid(
        uid,
        usage={pgpy.constants.KeyFlags.Sign},
        hashes=[pgpy.constants.HashAlgorithm.SHA256],
        ciphers=[pgpy.constants.SymmetricKeyAlgorithm.AES256],
        compression=[pgpy.constants.CompressionAlgorithm.ZLIB],
    )
    return key


@pytest.fixture(scope="session")
def bob_armor(bob_pgp_key):
    return str(bob_pgp_key.pubkey)


@pytest.fixture(scope="session")
def bob_fingerprint(bob_pgp_key):
    return str(bob_pgp_key.fingerprint).replace(" ", "").upper()


# ---------------------------------------------------------------------------
# Mock source factory
# ---------------------------------------------------------------------------


def make_mock_source(
    name: str,
    priority: int,
    ttl: int,
    fields: list[str],
    search_result: list[SourceKey] | SearchResult | None = None,
    fetch_result: SourceKey | None = None,
    freshness_result: bool = True,
    text_searchable: bool = True,
) -> MagicMock:
    source = MagicMock(spec=KeySource)
    source.name = name
    source.priority = priority
    source.ttl = ttl
    source.searchable_fields.return_value = [
        FieldDefinition(name=f, source_attribute=f, text_searchable=text_searchable)
        for f in fields
    ]
    source.search = AsyncMock(return_value=search_result if search_result is not None else [])
    source.fetch_by_fingerprint = AsyncMock(return_value=fetch_result)
    source.check_freshness = AsyncMock(return_value=freshness_result)
    source.close = AsyncMock()
    return source


# ---------------------------------------------------------------------------
# App factory helpers
# ---------------------------------------------------------------------------


def make_app_with_sources(sources_dict: dict, resolvers: list[ResolverConfig] | None = None):
    """Create a Quart app with pre-built source objects injected directly."""
    from hokeypokey.cache import KeyCache
    from hokeypokey.orchestrator import SearchOrchestrator
    from hokeypokey.resolver import ConfigResolver
    from quart import Quart
    from hokeypokey.hkp.routes import hkp_bp

    app = Quart(__name__)
    cache = KeyCache()
    resolver_objs = [ConfigResolver(r) for r in (resolvers or [])]
    orchestrator = SearchOrchestrator(
        sources=sources_dict,
        cache=cache,
        resolvers=resolver_objs,
    )
    app.extensions = {"orchestrator": orchestrator}
    app.register_blueprint(hkp_bp)
    return app, orchestrator, cache
