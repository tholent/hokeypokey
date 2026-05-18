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
- **Status:** `[ ]` pending
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
- **Status:** `[ ]` pending
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
- **Status:** `[ ]` pending
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
- **Status:** `[ ]` pending
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

## Parallelization Strategy

```
Wave 1: [1.1] [1.2] [1.3]           ← all parallel
            \    |    /
Wave 2:     [2.1] [2.2]             ← parallel with each other
              |     |
Wave 3:   [3.1] [3.2] [3.3]        ← 3.1 and 3.2 parallel; 3.3 depends on both
              \    |    /
Wave 4:     [4.1] [4.2]             ← parallel with each other
              \    /                    (also parallel with Wave 5)
Wave 5:   [5.1] [5.2]               ← parallel with each other AND with Wave 4
              \    /
Wave 6:     [6.1] [6.2]             ← 6.2 depends on 6.1
                |
Wave 7:       [7.1]                  ← depends on Wave 6
                |
Wave 8:       [8.1]                  ← depends on Wave 6 (can parallel with Wave 7)
```

**Maximum parallelism per wave:**
- Wave 1: 3 tasks
- Wave 2: 2 tasks
- Wave 3: 2 tasks (3.1 + 3.2), then 3.3
- Wave 4 + 5: 4 tasks (4.1, 4.2, 5.1, 5.2 — all in parallel)
- Wave 6: 2 tasks (sequential)
- Wave 7 + 8: 2 tasks (parallel)

**Critical path:** 1.2 → 2.1 → 3.3 → 4.2 → 6.1 → 6.2 → 8.1
