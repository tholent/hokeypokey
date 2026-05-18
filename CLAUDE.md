# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**hokeypokey** is a read-only HKP/HKPS-compliant GPG keyserver that federates GPG public keys from multiple upstream sources (LDAP, GitHub). It serves as a bridge exposing keys through a standard HKP interface compatible with `gpg --keyserver`. Keys are never submitted to hokeypokey — they only come from upstream sources.

## Commands

```bash
# Install dependencies (including dev tools)
uv sync --extra dev

# Run the server
uv run hokeypokey --config hokeypokey.example.toml

# Run all tests
uv run pytest

# Run a single test file
uv run pytest tests/test_cache.py

# Run a single test by name
uv run pytest tests/test_orchestrator.py::test_name

# Lint
uv run ruff check src/

# Format
uv run ruff format src/
```

Docker:
```bash
docker compose up
```

## Architecture

```
GPG Client → HKP HTTP Server (Quart) → Orchestrator → Cache + Sources (LDAP, GitHub)
                                                     ↓
                                              Search Resolvers (cross-source chaining)
```

**Request flow for a cache miss:**
1. `hkp/routes.py` receives `GET /pks/lookup`, parses query, calls orchestrator
2. `orchestrator.py` checks cache → revalidates stale entries (lightweight) or fans out to all sources concurrently
3. Search resolvers fire if source metadata triggers cross-source lookups (e.g., LDAP result has `github_username` → fetch GitHub keys)
4. Results deduplicated by fingerprint (lower priority number = more authoritative wins), cached, returned

**Key modules:**

| File | Responsibility |
|------|----------------|
| `app.py` | App factory — wires all components |
| `orchestrator.py` | Main business logic: cache → revalidate → fan-out → resolve |
| `cache.py` | In-memory multi-field index; priority-aware; optional LRU eviction |
| `resolver.py` | Declarative cross-source chains evaluated at query time |
| `sources/base.py` | Abstract `KeySource` plugin interface |
| `sources/ldap.py` | LDAP source; freshness via `modifyTimestamp` |
| `sources/github.py` | GitHub source; freshness via `ETag` |
| `hkp/routes.py` | Quart blueprints for HKP endpoints |
| `hkp/formatter.py` | ASCII-armored and machine-readable index formatting |
| `config.py` | TOML loader with validation; duration string parsing (`"5m"`, `"1h30m"`) |
| `search.py` | Parses HKP search terms into typed `SearchType` variants |
| `models.py` | Core data classes: `SearchType`, `SourceKey`, `CachedKey`, etc. |

## Plugin System

New sources implement the `KeySource` abstract base class (`sources/base.py`) and are registered in `sources/__init__.py`. Required methods: `search()`, `fetch_by_fingerprint()`, `check_freshness()`, `searchable_fields()`, `close()`.

## Resolvers

Resolvers are declared in TOML and fire when a cached result contains specified metadata fields. They chain queries across sources declaratively without hardcoding source-to-source dependencies. Depth-limited (default: 2) and cycle-aware.

## Configuration & Credentials

- Config: `hokeypokey.toml` (TOML format; see `hokeypokey.example.toml`)
- Credentials **never** go in TOML — use `.env` file or environment variables
- Key env vars: `LDAP_BIND_PASSWORD`, `GITHUB_TOKEN`

## Testing Notes

- `asyncio_mode = "auto"` — all test coroutines run automatically without `@pytest.mark.asyncio`
- `pytest-httpx` is used to mock GitHub HTTP calls in `test_source_github.py`
- Root `conftest.py` injects a mock `imghdr` module to work around a `pgpy` incompatibility with Python 3.13
