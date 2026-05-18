# Hokeypokey — Implementation Plan

> **Source of truth:** `.state/OVERVIEW.md`
> **Status legend:** `[ ]` pending · `[~]` in-process · `[x]` complete

---

## Wave 1 — Project Scaffolding & Core Data Types

No interdependencies within this wave. All tasks can run in parallel.

### Task 1.1 — Project skeleton and packaging
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to create:**
  - `pyproject.toml` — package metadata, dependencies, entry point
  - `src/hokeypokey/__init__.py` — package root, `__version__`
  - `src/hokeypokey/__main__.py` — `python -m hokeypokey` entry point (stub: parse `--config` arg, call `run()`)
  - `src/hokeypokey/cli.py` — CLI argument parsing with `argparse`: `--config PATH` (default `hokeypokey.toml`), `--host`, `--port` overrides
- **Details:**
  - `pyproject.toml` must use `[project]` table (PEP 621), `[build-system]` with `hatchling`
  - `requires-python = ">=3.12"`
  - Dependencies: `quart`, `hypercorn`, `ldap3`, `httpx`, `pgpy`
  - Dev dependencies group: `pytest`, `pytest-asyncio`, `ruff`
  - Entry point: `[project.scripts] hokeypokey = "hokeypokey.cli:main"`
  - Source layout: `src/hokeypokey/`
- **Verification:** `uv run hokeypokey --help` prints usage and exits 0

### Task 1.2 — Core data models
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to create:**
  - `src/hokeypokey/models.py`
- **Define these dataclasses/types:**
  - `SourceKey` — `fingerprint: str`, `key_armor: str`, `metadata: dict[str, str]`, `freshness_token: str`, `source_name: str`, `source_priority: int`
  - `FieldDefinition` — `name: str` (logical name, e.g. `"email"`), `source_attribute: str` (source-specific attribute, e.g. `"mail"`), `searchable: bool`
  - `ResolvedQuery` — `target_source: str`, `search_field: str`, `search_value: str`
  - `CachedKey` — `source_key: SourceKey`, `cached_at: float` (unix timestamp), `ttl: float` (seconds), `freshness_token: str`
  - `SearchType` — enum: `FINGERPRINT`, `LONG_KEY_ID`, `SHORT_KEY_ID`, `TEXT`, `EMAIL`
  - `ParsedSearch` — `search_type: SearchType`, `raw: str`, `normalized: str` (e.g., lowercase hex, lowercase email)
- **Verification:** `uv run python -c "from hokeypokey.models import SourceKey, CachedKey, SearchType"` succeeds

### Task 1.3 — Configuration loading and validation
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to create:**
  - `src/hokeypokey/config.py`
- **Define these dataclasses:**
  - `ServerConfig` — `host: str`, `port: int`, `tls_cert: str | None`, `tls_key: str | None`
  - `CacheConfig` — `backend: Literal["memory"]`, `default_ttl: int` (seconds, parsed from duration string like `"10m"`)
  - `SourceConfig` — `name: str`, `type: str`, `priority: int`, `ttl: int | None` (seconds, overrides cache default), `config: dict[str, Any]`
  - `ResolverConfig` — `name: str`, `trigger_source: str`, `trigger_field: str`, `target_source: str`, `target_field: str`
  - `AppConfig` — `server: ServerConfig`, `cache: CacheConfig`, `sources: list[SourceConfig]`, `resolvers: list[ResolverConfig]`
- **Implement:**
  - `load_config(path: Path) -> AppConfig` — reads TOML file using `tomllib`, maps to dataclasses
  - `parse_duration(s: str) -> int` — converts `"5m"`, `"1h"`, `"30s"`, `"2h30m"` to seconds
  - Validation: source names must be unique, resolver `trigger_source` and `target_source` must reference declared source names, field names across all sources must be globally unique, priority must be positive integer
  - Raise `ConfigError(message: str)` (custom exception) on any validation failure
- **Verification:** Unit tests in `tests/test_config.py`:
  - Test loading a valid TOML string → correct `AppConfig`
  - Test `parse_duration` with `"5m"`, `"1h"`, `"30s"`, `"2h30m"`
  - Test duplicate source names → `ConfigError`
  - Test resolver referencing nonexistent source → `ConfigError`
  - Test duplicate field names across sources → `ConfigError`

---

## Wave 2 — Abstract Interfaces & Cache Layer

Depends on Wave 1 (models, config). Tasks 2.1 and 2.2 can run in parallel.

### Task 2.1 — Source plugin interface (ABC)
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to create:**
  - `src/hokeypokey/sources/__init__.py` — re-exports `KeySource`
  - `src/hokeypokey/sources/base.py` — abstract base class
- **Define `KeySource(ABC)`:**
  ```python
  class KeySource(ABC):
      def __init__(self, name: str, priority: int, ttl: int, config: dict[str, Any]): ...

      @abstractmethod
      async def search(self, query: str, field: str = "email") -> list[SourceKey]: ...

      @abstractmethod
      async def fetch_by_fingerprint(self, fingerprint: str) -> SourceKey | None: ...

      @abstractmethod
      async def check_freshness(self, fingerprint: str, token: str) -> bool: ...

      @abstractmethod
      def searchable_fields(self) -> list[FieldDefinition]: ...

      @property
      def name(self) -> str: ...

      @property
      def priority(self) -> int: ...

      @property
      def ttl(self) -> int: ...

      @abstractmethod
      async def close(self) -> None: ...
  ```
- **Also define** the source registry function:
  - `get_source_class(type_name: str) -> type[KeySource]` — maps `"ldap"` → `LDAPSource`, `"github"` → `GitHubSource`. Raises `ConfigError` for unknown types. Uses a simple dict registry, not dynamic discovery.
- **Verification:** Importing the module succeeds; attempting to instantiate `KeySource` directly raises `TypeError`

### Task 2.2 — In-memory key cache
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to create:**
  - `src/hokeypokey/cache.py`
- **Implement `KeyCache` class:**
  - **Internal storage:** `dict[str, CachedKey]` keyed by fingerprint (uppercase hex, no `0x`)
  - **Index maps** (all values are `set[str]` of fingerprints):
    - `_by_long_id: dict[str, set[str]]` — last 16 hex chars → fingerprints
    - `_by_short_id: dict[str, set[str]]` — last 8 hex chars → fingerprints
    - `_by_email: dict[str, set[str]]` — lowercase email → fingerprints
    - `_by_field: dict[str, dict[str, set[str]]]` — `field_name → {value → fingerprints}`
  - **Methods:**
    - `put(key: SourceKey, ttl: float) -> None` — stores key, updates all indexes. If fingerprint already cached from a higher-priority source (lower number), skip. If same or lower priority, overwrite.
    - `get_by_fingerprint(fp: str) -> CachedKey | None` — exact match on normalized fingerprint
    - `get_by_key_id(key_id: str) -> list[CachedKey]` — match against long or short ID index depending on length
    - `search(query: str, field: str) -> list[CachedKey]` — look up in the appropriate index
    - `is_fresh(fp: str) -> bool` — returns `True` if cached and `cached_at + ttl > now`
    - `remove(fp: str) -> None` — removes from storage and all indexes
    - `remove_by_source(source_name: str) -> None` — removes all keys from a given source
  - **Thread safety:** Not required (single async event loop), but document this assumption
  - **Index maintenance:** `put()` and `remove()` must keep all indexes consistent. Extract email addresses from `metadata["email"]` if present. Index all metadata fields that have corresponding `FieldDefinition.searchable = True`.
- **Verification:** Unit tests in `tests/test_cache.py`:
  - Test `put` + `get_by_fingerprint` round-trip
  - Test priority conflict: put key from priority 10, then same fingerprint from priority 50 → priority 10 version retained
  - Test priority conflict: put key from priority 50, then same fingerprint from priority 10 → priority 10 version replaces
  - Test `search` by email returns correct keys
  - Test `is_fresh` returns `True` within TTL, `False` after
  - Test `remove` cleans up all indexes
  - Test `get_by_key_id` with long (16-char) and short (8-char) IDs

---

## Wave 3 — Search Orchestrator & Resolver

Depends on Wave 2 (cache, source interface).

### Task 3.1 — Search query parser
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to create:**
  - `src/hokeypokey/search.py`
- **Implement `parse_search(raw: str) -> ParsedSearch`:**
  - If `raw` starts with `0x` (case-insensitive):
    - Strip `0x` prefix, uppercase the hex
    - 8 hex chars → `SearchType.SHORT_KEY_ID`
    - 16 hex chars → `SearchType.LONG_KEY_ID`
    - 40 hex chars → `SearchType.FINGERPRINT`
    - Any other length → raise `ValueError("Invalid key ID length")`
  - If `raw` contains `@` → `SearchType.EMAIL`, normalize to lowercase
  - Otherwise → `SearchType.TEXT`, keep as-is
  - `normalized` field: uppercase hex for IDs/fingerprints, lowercase for email, original for text
- **Verification:** Unit tests in `tests/test_search.py`:
  - `0xABCD1234` → `SHORT_KEY_ID`, normalized `"ABCD1234"`
  - `0xDEADBEEFDECAFBAD` → `LONG_KEY_ID`, normalized `"DEADBEEFDECAFBAD"`
  - `0x` + 40 hex chars → `FINGERPRINT`
  - `user@example.com` → `EMAIL`, normalized lowercase
  - `John Doe` → `TEXT`
  - `0xZZZZ` → `ValueError`
  - `0x` + 9 hex chars → `ValueError`

### Task 3.2 — Search resolver (declarative, config-driven)
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to create:**
  - `src/hokeypokey/resolver.py`
- **Implement `ConfigResolver` class:**
  - Constructor takes a `ResolverConfig`
  - `can_resolve(metadata: dict[str, str], source_name: str) -> bool` — returns `True` if `source_name == self.trigger_source` and `self.trigger_field in metadata` and `metadata[self.trigger_field]` is non-empty
  - `resolve(metadata: dict[str, str]) -> list[ResolvedQuery]` — returns `[ResolvedQuery(target_source=self.target_source, search_field=self.target_field, search_value=metadata[self.trigger_field])]`
- **Verification:** Unit tests in `tests/test_resolver.py`:
  - Resolver with `trigger_source="ldap", trigger_field="github_id", target_source="github", target_field="github_username"`
  - `can_resolve({"github_id": "octocat"}, "ldap")` → `True`
  - `can_resolve({"github_id": "octocat"}, "other-source")` → `False`
  - `can_resolve({"email": "x@y.com"}, "ldap")` → `False` (missing trigger field)
  - `can_resolve({"github_id": ""}, "ldap")` → `False` (empty value)
  - `resolve({"github_id": "octocat"})` → `[ResolvedQuery("github", "github_username", "octocat")]`

### Task 3.3 — Search orchestrator
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to create:**
  - `src/hokeypokey/orchestrator.py`
- **Implement `SearchOrchestrator` class:**
  - Constructor: `__init__(self, sources: dict[str, KeySource], cache: KeyCache, resolvers: list[ConfigResolver], max_depth: int = 2)`
  - **`async def lookup(self, parsed: ParsedSearch) -> list[SourceKey]`:**
    1. **Cache check first:** query cache by fingerprint/key_id/email/text depending on `parsed.search_type`
    2. For any cache hits that are fresh (`cache.is_fresh(fp)`), collect them
    3. For cache hits that are stale, call `source.check_freshness(fp, token)` concurrently for each. If fresh, update `cached_at` in cache. If stale, refetch via `source.fetch_by_fingerprint(fp)` and update cache.
    4. **Cache miss path:** fan out to all sources concurrently:
       - For `FINGERPRINT`: call `source.fetch_by_fingerprint(normalized)` on all sources
       - For `EMAIL`/`TEXT`: call `source.search(query, field)` on all sources that have a matching searchable field
       - For `LONG_KEY_ID`/`SHORT_KEY_ID`: call `source.fetch_by_fingerprint()` is not viable (sources don't index by key ID); return only cache results for these. If cache miss, return empty (key IDs only work for previously-seen keys).
    5. Cache all newly fetched results via `cache.put()`
    6. **Resolver pass:** for each result with metadata, evaluate all resolvers. For any that fire, execute the resolved queries (respecting `max_depth` — decrement depth, stop at 0). Track visited `(source, field, value)` tuples to prevent cycles.
    7. Cache resolver results via `cache.put()`
    8. Deduplicate by fingerprint — cache handles priority, so just return unique fingerprints from cache
    9. Return results sorted by source priority (lowest number first)
  - **`async def get_key(self, parsed: ParsedSearch) -> SourceKey | None`:**
    - Calls `lookup()`, returns the highest-priority result (first in sorted list), or `None`
- **Verification:** Unit tests in `tests/test_orchestrator.py` using mock sources:
  - Test cache hit (fresh) → no source calls made
  - Test cache hit (stale) + freshness check passes → source `check_freshness` called, no `fetch_by_fingerprint`
  - Test cache hit (stale) + freshness check fails → source `fetch_by_fingerprint` called, cache updated
  - Test cache miss → all sources queried concurrently
  - Test resolver chaining: source A returns metadata that triggers resolver → source B queried
  - Test depth limit: resolver chain stops at `max_depth`
  - Test cycle detection: A→B→A does not loop
  - Test priority deduplication: same fingerprint from two sources → lower priority number wins

---

## Wave 4 — HKP Protocol Layer

Depends on Wave 3 (orchestrator, search parser).

### Task 4.1 — HKP response formatting
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to create:**
  - `src/hokeypokey/hkp/formatter.py`
  - `src/hokeypokey/hkp/__init__.py`
- **Implement:**
  - `format_index_response(keys: list[SourceKey]) -> str` — machine-readable index format:
    - First line: `info:1:<count>\n`
    - For each key, parse the ASCII armor with `pgpy` to extract:
      - Fingerprint (uppercase hex, no `0x`)
      - Algorithm number (map `pgpy` algo enum to RFC 9580 integer)
      - Key length in bits
      - Creation date (unix timestamp)
      - Expiration date (unix timestamp or empty)
      - Flags: `r` if revoked, `e` if expired, `d` if disabled
    - Write `pub:<fingerprint>:<algo>:<keylen>:<created>:<expires>:<flags>\n`
    - For each UID on the key:
      - URL-encode the UID string (percent-encode non-ASCII, `:`, `%`)
      - Write `uid:<encoded_uid>:<created>:<expires>:<flags>\n`
  - `format_get_response(keys: list[SourceKey]) -> str` — concatenate ASCII-armored key blocks, separated by blank line
  - Helper: `parse_key_metadata(armor: str) -> dict` — uses `pgpy.PGPKey.from_blob()` to extract fingerprint, algo, keylen, created, expires, revoked, uids. Handle parse errors gracefully (log warning, skip malformed keys).
- **Verification:** Unit tests in `tests/test_hkp_formatter.py`:
  - Generate a test PGP key (use `pgpy` to create one in test fixture), format as index → verify `info:1:1`, `pub:...`, `uid:...` lines parse correctly
  - Test UID with special characters (`:`, `%`, non-ASCII) is properly percent-encoded
  - Test expired key gets `e` flag
  - Test `format_get_response` with two keys → two armor blocks separated by blank line

### Task 4.2 — HKP endpoint routes
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to create:**
  - `src/hokeypokey/hkp/routes.py`
- **Implement Quart blueprint `hkp_bp`:**
  - `GET /pks/lookup`:
    - Parse query params: `op` (required), `search` (required), `options` (optional), `exact` (optional), `fingerprint` (optional)
    - Missing `op` or `search` → return `400 Bad Request`
    - Unknown `op` → return `501 Not Implemented`
    - `op=get`:
      - Parse search term via `parse_search()`
      - Call `orchestrator.lookup(parsed)`
      - If no results → `404 Not Found`
      - If `options` contains `mr`: `Content-Type: application/pgp-keys`, body = `format_get_response(keys)`
      - If `options` does not contain `mr`: same body (we are a machine-oriented server; always return armor), but `Content-Type: application/pgp-keys`
      - Set `Access-Control-Allow-Origin: *`
    - `op=index` / `op=vindex`:
      - Parse search term via `parse_search()`
      - Call `orchestrator.lookup(parsed)`
      - If no results → `404 Not Found`
      - If `options` contains `mr`: `Content-Type: text/plain; charset=utf-8`, body = `format_index_response(keys)`
      - If `options` does not contain `mr`: same machine-readable format (we don't serve HTML)
      - Set `Access-Control-Allow-Origin: *`
  - `POST /pks/add`:
    - Return `403 Forbidden` with body `Keyserver is read-only`
  - Error handling: catch `ValueError` from `parse_search()` → `400 Bad Request`
- **Verification:** Unit tests in `tests/test_hkp_routes.py` using Quart test client:
  - `GET /pks/lookup?op=get&search=0x<fingerprint>&options=mr` with a mock orchestrator returning a key → 200, correct content-type, armor body
  - `GET /pks/lookup?op=index&search=user@example.com&options=mr` → 200, `text/plain`, machine-readable index
  - `GET /pks/lookup?op=get&search=0xNONEXISTENT` → 404
  - `GET /pks/lookup` (missing params) → 400
  - `GET /pks/lookup?op=frobnicate&search=x` → 501
  - `POST /pks/add` → 403
  - Verify `Access-Control-Allow-Origin: *` header present on all responses

---

## Wave 5 — Source Plugins

Depends on Wave 2 (source interface, models). Can run in parallel with Waves 3-4 since plugins implement the interface defined in Wave 2.

### Task 5.1 — LDAP source plugin
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to create:**
  - `src/hokeypokey/sources/ldap.py`
- **Implement `LDAPSource(KeySource)`:**
  - **Constructor:** parse config dict for `uri`, `base_dn`, `bind_dn`, `bind_password_env`, `key_attribute` (default `"pgpKey"`), `search_filter` (default `"(pgpKey=*)"`), `fields` mapping. Read bind password from environment variable named in `bind_password_env`. Create `ldap3.Connection` (lazy — connect on first use).
  - **`searchable_fields()`:** return `FieldDefinition` list built from `config["fields"]` mapping
  - **`async def search(query, field)`:**
    - Look up the LDAP attribute name for `field` from the `fields` config mapping
    - Construct LDAP filter: `(&(<base_search_filter>)(<ldap_attr>=<query>))` — escape `query` for LDAP filter injection using `ldap3.utils.conv.escape_filter_chars()`
    - Execute search against `base_dn` with attributes: `[key_attribute] + [all mapped LDAP attributes] + ["modifyTimestamp"]`
    - For each result entry:
      - Extract the key data from `key_attribute`
      - Parse with `pgpy` to get fingerprint
      - Build metadata dict from configured field mappings
      - Use `modifyTimestamp` as freshness token
      - Construct `SourceKey`
    - Return list of `SourceKey`
    - Handle connection errors: log, return empty list
  - **`async def fetch_by_fingerprint(fingerprint)`:**
    - If LDAP schema has `pgpCertID` or similar fingerprint-indexed attribute (configurable via `fingerprint_attribute` in config, default `None`):
      - Search with filter `(<fingerprint_attribute>=<fingerprint>)`
    - Otherwise: return `None` (fingerprint lookup not supported without prior cache; this is documented in OVERVIEW.md risk #7)
  - **`async def check_freshness(fingerprint, token)`:**
    - The `token` is a stored `modifyTimestamp` value and the DN of the entry (store as `"<dn>|||<modifyTimestamp>"` in the freshness token)
    - Parse DN and old timestamp from token
    - Query LDAP: search at DN scope `BASE`, attrs `["modifyTimestamp"]`
    - If entry not found → return `False` (key was deleted)
    - If `modifyTimestamp` matches stored value → return `True` (fresh)
    - Otherwise → return `False` (stale)
  - **`async def close()`:** unbind LDAP connection
  - **LDAP connection management:** use `ldap3` with `SAFE_SYNC` strategy wrapped in `asyncio.to_thread()` for async compatibility (ldap3 is synchronous). Reconnect on connection failure.
- **Verification:** Unit tests in `tests/test_source_ldap.py`:
  - Mock `ldap3.Connection` to avoid real LDAP server
  - Test `search("user@example.com", "email")` constructs correct LDAP filter and returns `SourceKey` with correct metadata
  - Test LDAP filter escaping: search for `user@example.com)(objectClass=*` does not inject
  - Test `check_freshness` with matching timestamp → `True`
  - Test `check_freshness` with different timestamp → `False`
  - Test `check_freshness` with missing entry → `False`
  - Test `fetch_by_fingerprint` returns `None` when `fingerprint_attribute` is not configured

### Task 5.2 — GitHub source plugin
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to create:**
  - `src/hokeypokey/sources/github.py`
- **Implement `GitHubSource(KeySource)`:**
  - **Constructor:** parse config dict for `token_env`, `fields` mapping, `api_base` (default `"https://api.github.com"`). Read token from environment variable named in `token_env`. Create `httpx.AsyncClient` with auth header `Authorization: token <token>` and `Accept: application/vnd.github+json`.
  - **`searchable_fields()`:** return `FieldDefinition` list from `config["fields"]` — typically `github_username` mapped to `"login"`, `email` mapped to `"email"`
  - **`async def search(query, field)`:**
    - If `field` maps to `"login"` (github username): fetch `GET /users/{query}/gpg_keys`
    - If `field` maps to `"email"`: fetch `GET /search/users?q={query}+in:email` to resolve email → username, then fetch GPG keys for each matched user
    - Parse response JSON: each GPG key object has `raw_key` (ASCII armor), `key_id`, `emails` list
    - For each key: parse with `pgpy` to get fingerprint, build metadata dict (username, emails), use response `ETag` header as freshness token
    - Construct and return `SourceKey` list
    - Handle rate limiting: if response status is `403` or `429`, check `X-RateLimit-Remaining` and `Retry-After` headers, log warning, return empty list
  - **`async def fetch_by_fingerprint(fingerprint)`:**
    - Cannot fetch by fingerprint without knowing the username
    - Return `None` (relies on cache or resolver to have previously associated a username)
  - **`async def check_freshness(fingerprint, token)`:**
    - Token format: `"<username>|||<etag>"`
    - Parse username and ETag from token
    - Send `GET /users/{username}/gpg_keys` with `If-None-Match: <etag>` header
    - `304 Not Modified` → return `True`
    - `200 OK` → return `False` (data changed; caller will refetch)
    - Error/rate-limit → return `True` (assume fresh on error to avoid cascading failures)
  - **`async def close()`:** close `httpx.AsyncClient`
- **Verification:** Unit tests in `tests/test_source_github.py`:
  - Mock `httpx.AsyncClient` responses using `pytest-httpx` or manual mocking
  - Test `search("octocat", "github_username")` → correct API call, returns `SourceKey` with parsed fingerprint and metadata
  - Test `search("user@example.com", "email")` → calls search API first, then GPG keys API
  - Test `check_freshness` with `304` response → `True`
  - Test `check_freshness` with `200` response → `False`
  - Test rate limit handling: `429` response → returns empty list, logs warning
  - Test `fetch_by_fingerprint` → returns `None`

---

## Wave 6 — Application Assembly & Server Startup

Depends on Waves 3, 4, 5 (all components exist).

### Task 6.1 — Application factory and wiring
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to create:**
  - `src/hokeypokey/app.py`
- **Implement:**
  - `create_app(config: AppConfig) -> Quart` — the application factory:
    1. Create `Quart(__name__)` instance
    2. Instantiate `KeyCache()`
    3. For each `SourceConfig` in `config.sources`: call `get_source_class(type)` and instantiate with config
    4. For each `ResolverConfig` in `config.resolvers`: instantiate `ConfigResolver`
    5. Instantiate `SearchOrchestrator(sources, cache, resolvers)`
    6. Store orchestrator on `app` (e.g., `app.extensions["orchestrator"] = orchestrator`)
    7. Register `hkp_bp` blueprint
    8. Register `@app.after_serving` hook to call `source.close()` on all sources
    9. Return `app`
  - Update `cli.py` to:
    1. Parse args
    2. Call `load_config(path)`
    3. Call `create_app(config)`
    4. Run with Hypercorn: `hypercorn.asyncio.serve(app, hypercorn_config)` where `hypercorn_config` sets host, port, and optionally TLS cert/key from `ServerConfig`
- **Verification:** Integration test in `tests/test_app.py`:
  - Create a minimal `AppConfig` with no sources and no resolvers
  - Call `create_app(config)` → returns a Quart app
  - Use Quart test client: `GET /pks/lookup?op=get&search=0x1234567890ABCDEF1234567890ABCDEF12345678&options=mr` → 404 (no sources, nothing cached)
  - `POST /pks/add` → 403

### Task 6.2 — Update HKP routes to use orchestrator from app context
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to modify:**
  - `src/hokeypokey/hkp/routes.py`
- **Details:**
  - The routes in Task 4.2 need access to the `SearchOrchestrator`. Update them to retrieve it from `current_app.extensions["orchestrator"]` (Quart's `current_app` proxy).
  - This is a wiring task — the route logic is already defined in Task 4.2, this just connects it to the real orchestrator instead of a placeholder.
- **Verification:** Same integration test as Task 6.1 confirms end-to-end wiring works.

---

## Wave 7 — Docker & Deployment

Depends on Wave 6 (working application).

### Task 7.1 — Dockerfile and docker-compose
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to create:**
  - `Dockerfile`
  - `docker-compose.yml`
  - `hokeypokey.example.toml` — example configuration file with comments
- **Dockerfile details:**
  - Base: `python:3.13-slim`
  - Install `uv` via `pip install uv`
  - Copy project, install with `uv pip install .`
  - Expose port `11371`
  - `ENTRYPOINT ["hokeypokey"]`, `CMD ["--config", "/etc/hokeypokey/hokeypokey.toml"]`
- **docker-compose.yml:**
  - Single service `hokeypokey`
  - Build from `.`
  - Port mapping `11371:11371`
  - Volume mount for config file
  - Environment variables for `LDAP_BIND_PASSWORD`, `GITHUB_TOKEN`
- **hokeypokey.example.toml:**
  - Full example matching the config schema from OVERVIEW.md
  - Extensive comments explaining each field
  - Both LDAP and GitHub source examples
  - Resolver example
- **Verification:** `docker build -t hokeypokey .` succeeds; `docker run --rm hokeypokey --help` prints usage

---

## Wave 8 — End-to-End Integration Tests

Depends on Wave 6 (fully assembled application).

### Task 8.1 — Integration tests with mock sources
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to create:**
  - `tests/conftest.py` — shared fixtures (test PGP keys, mock sources, test app)
  - `tests/test_integration.py`
- **Test scenarios:**
  1. **Cold cache, email search, single LDAP source:**
     - Configure mock LDAP source with one key for `alice@example.com`
     - `GET /pks/lookup?op=index&search=alice@example.com&options=mr` → 200, index contains Alice's key
     - `GET /pks/lookup?op=get&search=alice@example.com&options=mr` → 200, returns Alice's armor
  2. **Cold cache, fingerprint lookup:**
     - `GET /pks/lookup?op=get&search=0x<alice_fingerprint>&options=mr` → 200 (if LDAP source supports fingerprint lookup) or 404 (if not)
  3. **Warm cache, repeated lookup:**
     - First request populates cache
     - Second request: mock source's `search()` is NOT called again (served from cache)
     - Verify via mock call count
  4. **Stale cache, freshness check passes:**
     - Populate cache, advance time past TTL
     - Mock `check_freshness` returns `True`
     - Request served from cache, `search()` not called
  5. **Stale cache, freshness check fails:**
     - Populate cache, advance time past TTL
     - Mock `check_freshness` returns `False`
     - Source `search()` called again, cache updated
  6. **Cross-source resolver:**
     - LDAP source returns key with `github_id: "octocat"` in metadata
     - Resolver configured: LDAP `github_id` → GitHub `github_username`
     - GitHub source returns a different key for `octocat`
     - Response includes both keys, LDAP key ranked higher (lower priority number)
  7. **Priority deduplication:**
     - Same fingerprint from LDAP (priority 10) and GitHub (priority 50)
     - Only LDAP version appears in results
  8. **POST /pks/add → 403**
  9. **Unknown op → 501**
  10. **Missing params → 400**
- **Fixtures in `conftest.py`:**
  - `test_pgp_key()` — generates a PGP key pair using `pgpy` for testing (RSA 2048, UID `"Test User <test@example.com>"`)
  - `mock_ldap_source()` — a `KeySource` subclass that returns predefined keys
  - `mock_github_source()` — a `KeySource` subclass that returns predefined keys
  - `test_app()` — creates a Quart app with mock sources wired in
- **Verification:** `uv run pytest tests/test_integration.py -v` — all pass

---

## Wave 9 — Audit Remediation

Source: `.state/OVERVIEW.md` § Audit Findings (2026-03-17). All tasks address confirmed findings.

### Task 9.1 — Fix LDAP connection race condition (HIGH)
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to modify:**
  - `src/hokeypokey/sources/ldap.py`
  - `tests/test_source_ldap.py`
- **Problem:** `_get_connection()` returns a single shared `ldap3.Connection`. Multiple concurrent `asyncio.to_thread(self._ldap_search, ...)` calls share this connection. `ldap3.Connection` is not thread-safe — `conn.entries` is mutated by each `search()` call, so interleaved calls corrupt results.
- **Fix — create a fresh connection per `_ldap_search()` call:**
  1. Remove the `self._conn` instance variable and the `_get_connection()` method entirely.
  2. Store connection parameters as instance state instead: `self._server_uri`, `self._bind_dn`, `self._bind_password` (already stored).
  3. In `_ldap_search()`, create a new `Server` + `Connection` at the top of the method, use it for the search, extract results into the `list[dict]` return value, then `conn.unbind()` in a `finally` block. Since `_ldap_search` runs in a thread via `asyncio.to_thread()`, each concurrent call gets its own connection — no shared mutable state.
  4. Update `close()` to be a no-op (or remove the connection teardown logic), since there is no longer a persistent connection to close.
  5. Create the `Server` object once in `__init__` and store as `self._server` — `ldap3.Server` is stateless and thread-safe, so it can be shared. Only `Connection` must be per-call.
- **Test additions in `tests/test_source_ldap.py`:**
  - Add a test that verifies concurrent searches do not share a connection: call `search()` twice concurrently via `asyncio.gather()`, mock `Connection` to record instance identity, assert two distinct `Connection` instances were created.
  - Verify existing tests still pass (they mock `Connection` — update mocks if constructor call site changed).
- **Verification:** `uv run pytest tests/test_source_ldap.py -v` — all pass, including new concurrency test.

### Task 9.2 — Add LRU eviction to KeyCache (MEDIUM)
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to modify:**
  - `src/hokeypokey/cache.py`
  - `src/hokeypokey/config.py`
  - `src/hokeypokey/app.py`
  - `hokeypokey.example.toml`
  - `tests/test_cache.py`
- **Problem:** `KeyCache` has no maximum size and no eviction. Memory grows without bound under sustained diverse queries.
- **Fix — add `max_size` parameter with LRU eviction:**
  1. Add `max_size: int | None = None` parameter to `KeyCache.__init__()`. `None` means unlimited (backward-compatible default).
  2. Track access order: change `self._store` from `dict[str, CachedKey]` to `collections.OrderedDict[str, CachedKey]`. On every `get_by_fingerprint()`, `get_by_key_id()`, and `search()` hit, call `self._store.move_to_end(fp)` to mark the entry as recently used.
  3. In `put()`, after inserting the new entry, if `self._max_size is not None and len(self._store) > self._max_size`: pop the **oldest** entry (`self._store.popitem(last=False)`) and call `self._deindex_key()` on it to clean up all secondary indexes.
  4. Add `max_size: int | None` field to `CacheConfig` dataclass in `config.py` (default `None`). Parse from TOML `[cache]` section key `max_size` as an integer.
  5. Pass `max_size` from `CacheConfig` to `KeyCache()` in `create_app()` in `app.py`.
  6. Add `max_size` to `hokeypokey.example.toml` with a comment: `# max_size = 10000  # Maximum number of cached keys (optional; omit for unlimited)`.
- **Test additions in `tests/test_cache.py`:**
  - Test eviction: create `KeyCache(max_size=3)`, put 4 keys, assert `len(cache) == 3` and the first key inserted is gone.
  - Test LRU ordering: create `KeyCache(max_size=3)`, put keys A, B, C, access A via `get_by_fingerprint`, put key D → B is evicted (least recently used), A is retained.
  - Test eviction cleans indexes: after eviction, `search()` and `get_by_key_id()` for the evicted key return empty.
  - Test `max_size=None` (default) does not evict.
- **Verification:** `uv run pytest tests/test_cache.py -v` — all pass, including new eviction tests.

### Task 9.3 — Enable LDAP TLS certificate verification (MEDIUM)
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to modify:**
  - `src/hokeypokey/sources/ldap.py`
  - `hokeypokey.example.toml`
  - `tests/test_source_ldap.py`
- **Problem:** `Server(self._uri)` uses `ldap3` defaults which do not validate TLS certificates, even for `ldaps://` URIs.
- **Fix — default to `CERT_REQUIRED`, make configurable:**
  1. In `LDAPSource.__init__()`, read optional config keys:
     - `tls_verify` (bool, default `True`) — whether to validate the server certificate
     - `tls_ca_file` (str, optional) — path to a CA bundle file for custom CAs
  2. If the URI scheme is `ldaps://` or if STARTTLS is implied, construct an `ldap3.Tls` object:
     ```python
     import ssl
     from ldap3 import Tls
     validate = ssl.CERT_REQUIRED if tls_verify else ssl.CERT_NONE
     tls_config = Tls(validate=validate, ca_certs_file=tls_ca_file)
     ```
  3. Pass `tls=tls_config` to `Server(self._uri, tls=tls_config)`.
  4. Note: after Task 9.1, the `Server` object is created once in `__init__` and stored as `self._server`. The `Tls` config is set on the `Server`, so all connections created from it inherit the TLS settings.
  5. Update `hokeypokey.example.toml` to document the new config keys:
     ```toml
     # TLS certificate verification (default: true). Set to false for self-signed certs.
     # tls_verify = true
     # Path to a custom CA bundle file (optional).
     # tls_ca_file = "/etc/ssl/certs/ca-certificates.crt"
     ```
- **Test additions in `tests/test_source_ldap.py`:**
  - Test that when `tls_verify` is `True` (default) and URI is `ldaps://`, the `Server` is constructed with a `Tls` object where `validate == ssl.CERT_REQUIRED`.
  - Test that when `tls_verify` is `False`, `validate == ssl.CERT_NONE`.
  - Test that `tls_ca_file` is passed through to the `Tls` object.
  - Test that plain `ldap://` URIs do not get a `Tls` object (no TLS on plaintext).
- **Verification:** `uv run pytest tests/test_source_ldap.py -v` — all pass.

### Task 9.4 — HTML-escape source names in landing page (LOW)
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to modify:**
  - `src/hokeypokey/hkp/routes.py`
  - `tests/test_hkp_routes.py`
- **Problem:** Source names from config are interpolated into HTML without escaping. Defense-in-depth fix.
- **Fix:**
  1. Add `import html` at the top of `routes.py`.
  2. In the `index()` route function (line 53), change:
     ```python
     "".join(f"<li><code>{s}</code></li>" for s in source_names)
     ```
     to:
     ```python
     "".join(f"<li><code>{html.escape(s)}</code></li>" for s in source_names)
     ```
- **Test additions in `tests/test_hkp_routes.py`:**
  - Add a test that configures a source with name `<script>alert(1)</script>`, requests `GET /`, and asserts the response body contains `&lt;script&gt;` (escaped) and does NOT contain `<script>alert(1)</script>` (raw).
- **Verification:** `uv run pytest tests/test_hkp_routes.py -v` — all pass.

### Task 9.5 — Add GitHub username validation guard (LOW)
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to modify:**
  - `src/hokeypokey/sources/github.py`
  - `tests/test_source_github.py`
- **Problem:** Usernames are interpolated into API paths without validation. Defense-in-depth fix.
- **Fix:**
  1. Add a module-level compiled regex: `_GITHUB_USERNAME_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?$")` — matches GitHub's actual username rules (alphanumeric, hyphens, no leading/trailing hyphen, 1-39 chars).
  2. Add a private validation method:
     ```python
     def _validate_username(self, username: str) -> bool:
         return bool(_GITHUB_USERNAME_RE.match(username)) and len(username) <= 39
     ```
  3. In `_fetch_keys_for_username()`, before the HTTP call, check `if not self._validate_username(username): return []` with a debug log.
  4. In `check_freshness()`, after parsing the username from the token, apply the same validation. Return `True` (assume fresh) if invalid — don't make a request with a bad username.
- **Test additions in `tests/test_source_github.py`:**
  - Test that `search("../evil", "github_username")` returns `[]` without making an HTTP request.
  - Test that `search("valid-user", "github_username")` proceeds normally.
  - Test that `check_freshness("fp", "../evil|||etag")` returns `True` without making an HTTP request.
  - Test edge cases: single char `"a"`, max length (39 chars), leading hyphen `"-bad"` → rejected, trailing hyphen `"bad-"` → rejected.
- **Verification:** `uv run pytest tests/test_source_github.py -v` — all pass.

### Task 9.6 — Fix type annotation in app factory (LOW)
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to modify:**
  - `src/hokeypokey/app.py`
- **Problem:** `sources: dict[str, object] = {}` should use `KeySource` type.
- **Fix:**
  1. Add import: `from hokeypokey.sources.base import KeySource`
  2. Change line 37 from `sources: dict[str, object] = {}` to `sources: dict[str, KeySource] = {}`
  3. Remove the `# type: ignore[arg-type]` comment on the `SearchOrchestrator(sources=sources, ...)` call (line 64) since the type now matches.
  4. Remove the `# type: ignore[attr-defined]` comments on `source.close()` and `source.name` in the shutdown hook (lines 81-84) since `KeySource` defines these.
- **Verification:** `uv run pytest tests/test_app.py -v` — all pass. No `# type: ignore` comments remain in `app.py`.

### Task 9.7 — Run full test suite and commit
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Depends on:** 9.1, 9.2, 9.3, 9.4, 9.5, 9.6
- **Actions:**
  1. Run `uv run pytest -v` — all tests must pass (existing + new).
  2. Run `uv run ruff check src/ tests/` — no lint errors.
  3. Verify test count increased (was 166, expect ~180+ with new tests).
- **Verification:** Clean test run, clean lint, no regressions.

---

## Wave 10 — Audit Backlog Remediation

Source: `analysis/comprehensive_audit_20260317.md`. All remaining findings not addressed in Wave 9.
Tasks 10.1–10.7 are independent and can run in parallel. Task 10.8 is the final gate.

### Task 10.1 — Log warning when GitHub token is missing (MEDIUM-2)
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to modify:**
  - `src/hokeypokey/sources/github.py`
  - `tests/test_source_github.py`
- **Problem:** If `token_env` is configured but the environment variable is absent, the source silently falls back to unauthenticated requests (60 req/hour), with no indication of the problem.
- **Fix:**
  1. In `GitHubSource.__init__()`, after `token = os.environ.get(token_env)` (line 65), add:
     ```python
     if not token:
         logger.warning(
             "GitHub source %r: environment variable %r is not set. "
             "Unauthenticated requests are limited to 60/hour.",
             name, token_env,
         )
     ```
  2. Keep the existing behaviour (unauthenticated client is still created) — this is a warning, not a hard failure.
- **Test additions in `tests/test_source_github.py`:**
  - Add a test that constructs `GitHubSource` with `token_env` pointing to an unset env var and asserts the warning is logged (use `caplog` fixture with `propagate=True`).
  - Add a test that constructs `GitHubSource` with a set env var and asserts no warning is logged.
- **Verification:** `uv run pytest tests/test_source_github.py -v` — all pass.

### Task 10.2 — Sanitize ValueError message in lookup route (MEDIUM-3)
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to modify:**
  - `src/hokeypokey/hkp/routes.py`
  - `tests/test_hkp_routes.py`
- **Problem:** `except ValueError as exc: return (str(exc), 400, ...)` returns the raw exception message to clients. The try block currently only wraps `parse_search()`, but the pattern is fragile — a future developer could add code inside the try block and accidentally leak internal error details.
- **Fix:**
  1. Restructure the route to make the scope explicit — move `parse_search()` outside the broader orchestrator try/except and give it its own narrow handler:
     ```python
     # Parse search term — ValueError means a malformed query, not a server error
     try:
         parsed = parse_search(search_term)
     except ValueError:
         return ("Invalid search term", 400, _ERR_HEADERS)
     ```
     The exception detail is intentionally dropped; the client's malformed input is
     already echoed back in the 400 context and the message from `parse_search` adds
     nothing actionable for the caller.
- **Test additions in `tests/test_hkp_routes.py`:**
  - Update existing invalid-hex test to verify the 400 body is exactly `"Invalid search term"` (not the raw exception message).
- **Verification:** `uv run pytest tests/test_hkp_routes.py -v` — all pass.

### Task 10.3 — Memoize source registry (LOW-1)
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to modify:**
  - `src/hokeypokey/sources/__init__.py`
- **Problem:** The `_REGISTRY` dict inside `get_source_class()` is rebuilt on every call. The lazy-import pattern that avoids circular imports is correct, but the dict construction can be memoized.
- **Fix:**
  1. Add a module-level `_REGISTRY: dict[str, type[KeySource]] | None = None`.
  2. Extract dict construction into a private `_build_registry()` function (keeping the lazy imports inside it to preserve the circular-import protection).
  3. In `get_source_class()`, lazily populate `_REGISTRY` on first call:
     ```python
     _REGISTRY: dict[str, type[KeySource]] | None = None

     def _build_registry() -> dict[str, type[KeySource]]:
         from hokeypokey.sources.github import GitHubSource
         from hokeypokey.sources.ldap import LDAPSource
         return {"ldap": LDAPSource, "github": GitHubSource}

     def get_source_class(type_name: str) -> type[KeySource]:
         global _REGISTRY
         if _REGISTRY is None:
             _REGISTRY = _build_registry()
         try:
             return _REGISTRY[type_name]
         except KeyError:
             from hokeypokey.config import ConfigError
             known = ", ".join(sorted(_REGISTRY))
             raise ConfigError(
                 f"Unknown source type {type_name!r}. Known types: {known}."
             ) from None
     ```
- **No new tests required** — existing tests exercise `get_source_class()` and will continue to pass. Optionally add a test that calls it twice and asserts `_build_registry` is not called on the second invocation (use `monkeypatch`).
- **Verification:** `uv run pytest tests/ -v` — no regressions.

### Task 10.4 — Eliminate model duplication: `_ResolverQuery` and `CachedKey.freshness_token` (LOW-2, LOW-3)
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to modify:**
  - `src/hokeypokey/orchestrator.py`
  - `src/hokeypokey/models.py`
- **Problems:**
  - LOW-2: `_ResolverQuery` in `orchestrator.py` (lines 21–26) is structurally identical to `ResolvedQuery` in `models.py`. Two dataclasses with the same fields serving the same purpose.
  - LOW-3: `CachedKey.freshness_token` (models.py lines 89–92) duplicates `source_key.freshness_token` and is never updated independently, creating two potentially-diverging copies.

**Fix LOW-2 — Remove `_ResolverQuery`, use `ResolvedQuery` throughout:**
  1. In `orchestrator.py`, add `ResolvedQuery` to the import from `hokeypokey.models`.
  2. Delete the `_ResolverQuery` dataclass (lines 21–26).
  3. In `_collect_resolver_queries()`, change the return type annotation and all `_ResolverQuery(...)` instantiations to `ResolvedQuery(...)`.
  4. Update `_run_resolver_query()` parameter type from `_ResolverQuery` to `ResolvedQuery`.
  5. The `_FanOutResult` dataclass is distinct (it bundles a different shape of data) — leave it as is.

**Fix LOW-3 — Replace `CachedKey.freshness_token` field with a property:**
  1. In `models.py`, remove the `freshness_token: str = field(init=False)` field declaration and the `__post_init__` method (or leave `__post_init__` empty if it serves no other purpose after this change).
  2. Add a `@property`:
     ```python
     @property
     def freshness_token(self) -> str:
         """Delegated to source_key — single source of truth for the freshness token."""
         return self.source_key.freshness_token
     ```
  3. All existing reads of `entry.freshness_token` (e.g., `orchestrator.py:296`) continue to work via the property. No callers need updating.
  4. Update the `CachedKey` docstring to remove the claim that `freshness_token` is "updated in-place" (it never was).
- **Verification:** `uv run pytest -v` — no regressions (no new tests required; the change is a cleanup with identical runtime behaviour).

### Task 10.5 — Add day support to duration parser (LOW-5)
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to modify:**
  - `src/hokeypokey/config.py`
  - `tests/test_config.py`
- **Problem:** `parse_duration()` supports `h`, `m`, `s` but not `d`. Cache TTLs of days (e.g., `"7d"`, `"1d12h"`) are a natural use case.
- **Fix:**
  1. Update `_DURATION_RE` to include an optional leading days component:
     ```python
     _DURATION_RE = re.compile(
         r"^(?:(?P<days>\d+)d)?(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?$"
     )
     ```
  2. In `parse_duration()`, update the `any(m.group(g) ...)` guard to include `"days"`:
     ```python
     if not m or not any(m.group(g) for g in ("days", "hours", "minutes", "seconds")):
     ```
  3. Add days to the total calculation:
     ```python
     days = int(m.group("days") or 0)
     total = days * 86400 + hours * 3600 + minutes * 60 + seconds
     ```
  4. Update the docstring and error message to include `d` in the examples.
- **Test additions in `tests/test_config.py`:**
  - `"1d"` → `86400`
  - `"7d"` → `604800`
  - `"1d12h"` → `129600`
  - `"2d6h30m"` → `196200`
- **Verification:** `uv run pytest tests/test_config.py -v` — all pass.

### Task 10.6 — Standardize `search()` return type to `SearchResult` (LOW-7)
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to modify:**
  - `src/hokeypokey/sources/base.py`
  - `src/hokeypokey/sources/github.py`
  - `tests/test_source_github.py`
- **Problem:** `KeySource.search()` in `base.py` declares `-> list[SourceKey]` but `LDAPSource.search()` returns `SearchResult`. This violates LSP and forces `isinstance` checks in the orchestrator. Standardizing on `SearchResult` is cleaner — it's a strict superset of `list[SourceKey]` (a `SearchResult` with empty `metadata_only` is equivalent).
- **Fix:**
  1. In `base.py`, add `SearchResult` to the `TYPE_CHECKING` import block and update the `search()` signature:
     ```python
     @abstractmethod
     async def search(self, query: str, field: str = "email") -> SearchResult:
     ```
  2. In `github.py`, add `SearchResult` to the import from `hokeypokey.models`. Wrap all `return []` and `return all_keys` / `return keys` statements in `search()` and its helpers that feed into `search()`:
     - In `search()`: return `SearchResult(keys=await self._fetch_keys_for_username(query))` etc., or wrap at the end of `search()`:
       ```python
       keys = await self._fetch_keys_for_username(query)  # (or _search_by_email)
       return SearchResult(keys=keys)
       ```
     - Update return type of `search()` from `-> list[SourceKey]` to `-> SearchResult`.
  3. The orchestrator's `isinstance(result, SearchResult)` / `isinstance(result, list)` branches in `_fan_out()` and `_run_resolver_query()` are now always `SearchResult` — simplify those branches to remove the `list` case.
- **Test additions in `tests/test_source_github.py`:**
  - Update all assertions that check `isinstance(result, list)` to check `isinstance(result, SearchResult)`.
  - Verify `result.keys` contains the expected `SourceKey` objects.
- **Verification:** `uv run pytest tests/test_source_github.py tests/test_source_ldap.py tests/test_orchestrator.py -v` — all pass.

### Task 10.7 — Add OPTIONS preflight handler and `/healthz` endpoint (LOW-8 + recommendation)
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Files to modify:**
  - `src/hokeypokey/hkp/routes.py`
  - `tests/test_hkp_routes.py`
- **Problem (LOW-8):** `Access-Control-Allow-Origin: *` is set on all responses but there is no `OPTIONS` handler. Browser preflight requests receive `405 Method Not Allowed`, preventing cross-origin JS clients from working.
- **Problem (recommendation):** No `/healthz` endpoint for container orchestration liveness/readiness probes.

**Fix — OPTIONS handler:**
  Add a route for `OPTIONS /pks/lookup` that returns the CORS preflight response:
  ```python
  _CORS_PREFLIGHT_HEADERS = {
      **_CORS_HEADERS,
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
      "Access-Control-Max-Age": "86400",
  }

  @hkp_bp.route("/pks/lookup", methods=["OPTIONS"])
  async def lookup_preflight():
      return ("", 204, _CORS_PREFLIGHT_HEADERS)
  ```

**Fix — `/healthz` endpoint:**
  ```python
  @hkp_bp.route("/healthz", methods=["GET"])
  async def healthz():
      source_count = len(_orchestrator()._sources)
      return (
          f"ok\nsources: {source_count}\n",
          200,
          {**_CORS_HEADERS, "Content-Type": _PLAIN},
      )
  ```

- **Test additions in `tests/test_hkp_routes.py`:**
  - `OPTIONS /pks/lookup` → 204, `Access-Control-Allow-Origin: *`, `Access-Control-Allow-Methods` header present.
  - `GET /healthz` with 2 mock sources → 200, body contains `"ok"` and `"sources: 2"`.
  - `GET /healthz` with 0 sources → 200, body contains `"sources: 0"`.
- **Verification:** `uv run pytest tests/test_hkp_routes.py -v` — all pass.

### Task 10.8 — Run full test suite and commit
- **Status:** `[x]` complete
- **Agent:** `@developer`
- **Depends on:** 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7
- **Actions:**
  1. Run `uv run pytest -v` — all tests must pass.
  2. Run `uv run ruff check src/ tests/` — no lint errors.
  3. Verify test count increased from Wave 9's ~180+ baseline.
- **Verification:** Clean test run, clean lint, no regressions.

---

## Parallelization Strategy

```
Waves 1–8: [all complete]

Wave 9 (audit remediation):
  [9.1] [9.2] [9.3] [9.4] [9.5] [9.6]   ← all 6 parallel (independent files/concerns)
                  \    |    /
                   [9.7]                   ← depends on all of 9.1–9.6
```

**Wave 9 parallelism:** Tasks 9.1–9.6 are fully independent — they touch different files and different concerns:
- 9.1 modifies `ldap.py` (connection logic) — no overlap with 9.3 (TLS config in `__init__`)? **Caution:** 9.1 and 9.3 both modify `ldap.py` and `test_source_ldap.py`. 9.3 depends on the `Server` object being created in `__init__` (which 9.1 introduces). **Therefore 9.3 depends on 9.1.**
- 9.2 modifies `cache.py`, `config.py`, `app.py` — independent of all others except 9.6 also modifies `app.py`. **Caution:** 9.2 and 9.6 both modify `app.py`. The changes are to different lines (9.2 adds `max_size` param to `KeyCache()` constructor call; 9.6 changes the type annotation and removes `# type: ignore` comments). **These can be done in either order but not truly in parallel — run 9.6 first (smaller change), then 9.2.**
- 9.4 modifies `routes.py` — independent of all others.
- 9.5 modifies `github.py` — independent of all others.

**Revised parallelization:**
```
Wave 9a: [9.1] [9.4] [9.5] [9.6]    ← all parallel
              \              |
Wave 9b:     [9.3]        [9.2]      ← 9.3 after 9.1; 9.2 after 9.6
                \          /
Wave 9c:       [9.7]                  ← final validation
```

**Maximum parallelism:** 4 tasks in Wave 9a, 2 in Wave 9b, 1 in Wave 9c.

**Critical path:** 9.1 → 9.3 → 9.7

```
Wave 10 (audit backlog):
  [10.1] [10.2] [10.3] [10.4] [10.5] [10.6] [10.7]   ← all 7 parallel (independent files)
                          \      |      /
                              [10.8]                    ← depends on all of 10.1–10.7
```

**Wave 10 parallelism:** All seven remediation tasks are fully independent — they touch disjoint files:
- 10.1 modifies `github.py` + its tests
- 10.2 modifies `routes.py` + its tests
- 10.3 modifies `sources/__init__.py` only
- 10.4 modifies `orchestrator.py` + `models.py`
- 10.5 modifies `config.py` + its tests
- 10.6 modifies `base.py` + `github.py` + its tests — **Caution:** 10.1 and 10.6 both modify `github.py`. The changes are to different methods (`__init__` vs `search()` return type). Run 10.1 first, then incorporate into 10.6, or merge into a single pass on `github.py`.
- 10.7 modifies `routes.py` + its tests — **Caution:** 10.2 and 10.7 both modify `routes.py` and its tests. Merge into a single pass or run sequentially.

**Revised parallelization accounting for file overlaps:**
```
Wave 10a: [10.1+10.6] [10.2+10.7] [10.3] [10.4] [10.5]   ← 5 parallel batches
                               \      |     /
Wave 10b:                          [10.8]
```

**Maximum parallelism:** 5 parallel batches, then 1 final gate.

**Critical path:** Any single task in Wave 10a → 10.8
