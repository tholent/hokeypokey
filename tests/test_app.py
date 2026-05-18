"""Integration tests for the application factory."""

from __future__ import annotations

import pytest

from hokeypokey.app import create_app
from hokeypokey.config import AppConfig, CacheConfig, ServerConfig


def minimal_config() -> AppConfig:
    return AppConfig(
        server=ServerConfig(host="127.0.0.1", port=11371),
        cache=CacheConfig(backend="memory", default_ttl=600),
        sources=[],
        resolvers=[],
    )


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def test_create_app_returns_quart_app():
    from quart import Quart
    app = create_app(minimal_config())
    assert isinstance(app, Quart)


def test_create_app_registers_orchestrator():
    app = create_app(minimal_config())
    assert "orchestrator" in app.extensions


def test_create_app_registers_hkp_blueprint():
    app = create_app(minimal_config())
    assert "hkp" in app.blueprints


# ---------------------------------------------------------------------------
# End-to-end via test client (no sources configured)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_key_no_sources_returns_404():
    app = create_app(minimal_config())
    fp = "A" * 40
    async with app.test_client() as client:
        resp = await client.get(f"/pks/lookup?op=get&search=0x{fp}&options=mr")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_index_no_sources_returns_404():
    app = create_app(minimal_config())
    async with app.test_client() as client:
        resp = await client.get("/pks/lookup?op=index&search=nobody@example.com&options=mr")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_post_pks_add_returns_403():
    app = create_app(minimal_config())
    async with app.test_client() as client:
        resp = await client.post("/pks/add", data={"keytext": "fake"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_unknown_op_returns_501():
    app = create_app(minimal_config())
    async with app.test_client() as client:
        resp = await client.get("/pks/lookup?op=frobnicate&search=x")
    assert resp.status_code == 501


@pytest.mark.asyncio
async def test_missing_params_returns_400():
    app = create_app(minimal_config())
    async with app.test_client() as client:
        resp = await client.get("/pks/lookup?op=get")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_cors_header_present():
    app = create_app(minimal_config())
    fp = "A" * 40
    async with app.test_client() as client:
        resp = await client.get(f"/pks/lookup?op=get&search=0x{fp}&options=mr")
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"
