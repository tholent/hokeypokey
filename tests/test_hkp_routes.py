"""Tests for HKP endpoint routes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from quart import Quart

from hokeypokey.hkp.routes import hkp_bp
from hokeypokey.models import SourceKey

FP = "A" * 40


def make_source_key(fp: str = FP) -> SourceKey:
    return SourceKey(
        fingerprint=fp,
        key_armor=(
            "-----BEGIN PGP PUBLIC KEY BLOCK-----\n"
            "fake_armor_data\n"
            "-----END PGP PUBLIC KEY BLOCK-----"
        ),
        metadata={"email": "alice@example.com"},
        freshness_token="token",
        source_name="test",
        source_priority=10,
    )


def make_test_app(lookup_result: list[SourceKey] | None = None) -> Quart:
    """Create a minimal Quart app with the HKP blueprint and a mock orchestrator."""
    app = Quart(__name__)
    app.register_blueprint(hkp_bp)

    mock_orchestrator = MagicMock()
    mock_orchestrator.lookup = AsyncMock(return_value=lookup_result or [])
    app.extensions = {"orchestrator": mock_orchestrator}

    return app


# ---------------------------------------------------------------------------
# op=get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_key_returns_200_with_armor():
    app = make_test_app(lookup_result=[make_source_key()])
    async with app.test_client() as client:
        resp = await client.get(f"/pks/lookup?op=get&search=0x{FP}&options=mr")
    assert resp.status_code == 200
    assert resp.content_type == "application/pgp-keys"
    body = await resp.get_data(as_text=True)
    assert "BEGIN PGP PUBLIC KEY BLOCK" in body


@pytest.mark.asyncio
async def test_get_key_without_mr_still_returns_armor():
    """We always return armor regardless of options=mr."""
    app = make_test_app(lookup_result=[make_source_key()])
    async with app.test_client() as client:
        resp = await client.get(f"/pks/lookup?op=get&search=0x{FP}")
    assert resp.status_code == 200
    assert resp.content_type == "application/pgp-keys"


@pytest.mark.asyncio
async def test_get_key_not_found_returns_404():
    app = make_test_app(lookup_result=[])
    async with app.test_client() as client:
        resp = await client.get(f"/pks/lookup?op=get&search=0x{FP}&options=mr")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# op=index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_returns_200_machine_readable():
    app = make_test_app(lookup_result=[make_source_key()])
    async with app.test_client() as client:
        resp = await client.get("/pks/lookup?op=index&search=alice@example.com&options=mr")
    assert resp.status_code == 200
    assert "text/plain" in resp.content_type
    body = await resp.get_data(as_text=True)
    assert body.startswith("info:1:")


@pytest.mark.asyncio
async def test_index_not_found_returns_404():
    app = make_test_app(lookup_result=[])
    async with app.test_client() as client:
        resp = await client.get("/pks/lookup?op=index&search=nobody@example.com&options=mr")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# op=vindex (treated same as index)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vindex_returns_200():
    app = make_test_app(lookup_result=[make_source_key()])
    async with app.test_client() as client:
        resp = await client.get("/pks/lookup?op=vindex&search=alice@example.com&options=mr")
    assert resp.status_code == 200
    assert "text/plain" in resp.content_type


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_op_returns_400():
    app = make_test_app()
    async with app.test_client() as client:
        resp = await client.get("/pks/lookup?search=alice@example.com")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_missing_search_returns_400():
    app = make_test_app()
    async with app.test_client() as client:
        resp = await client.get("/pks/lookup?op=get")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_unknown_op_returns_501():
    app = make_test_app()
    async with app.test_client() as client:
        resp = await client.get("/pks/lookup?op=frobnicate&search=alice@example.com")
    assert resp.status_code == 501


@pytest.mark.asyncio
async def test_invalid_hex_search_returns_400():
    """A 0x-prefixed search with invalid hex should return 400."""
    app = make_test_app()
    async with app.test_client() as client:
        resp = await client.get("/pks/lookup?op=get&search=0xZZZZZZZZ")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /pks/add — always 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_pks_add_returns_403():
    app = make_test_app()
    async with app.test_client() as client:
        resp = await client.post("/pks/add", data={"keytext": "fake"})
    assert resp.status_code == 403
    body = await resp.get_data(as_text=True)
    assert "read-only" in body.lower()


# ---------------------------------------------------------------------------
# CORS headers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cors_header_on_200():
    app = make_test_app(lookup_result=[make_source_key()])
    async with app.test_client() as client:
        resp = await client.get(f"/pks/lookup?op=get&search=0x{FP}&options=mr")
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"


@pytest.mark.asyncio
async def test_cors_header_on_404():
    app = make_test_app(lookup_result=[])
    async with app.test_client() as client:
        resp = await client.get(f"/pks/lookup?op=get&search=0x{FP}&options=mr")
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"


@pytest.mark.asyncio
async def test_cors_header_on_400():
    app = make_test_app()
    async with app.test_client() as client:
        resp = await client.get("/pks/lookup?op=get")
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"


@pytest.mark.asyncio
async def test_cors_header_on_403():
    app = make_test_app()
    async with app.test_client() as client:
        resp = await client.post("/pks/add")
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"


@pytest.mark.asyncio
async def test_cors_header_on_501():
    app = make_test_app()
    async with app.test_client() as client:
        resp = await client.get("/pks/lookup?op=frobnicate&search=x")
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"
