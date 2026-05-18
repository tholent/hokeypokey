# Hokeypokey — Comprehensive Code Audit Report

**Date:** 2026-03-17
**Auditor:** Senior Auditor (claude-opus-4-6)
**Project:** hokeypokey — Read-only HKP/HKPS keyserver
**Scope:** Full codebase (all source, tests, config, infrastructure)
**Test Suite:** 166 tests, all passing

---

## 1. Executive Summary

**Overall Grade: B+**

Hokeypokey is a well-architected, cleanly implemented HKP keyserver with a thoughtful plugin system, proper separation of concerns, and comprehensive test coverage. The codebase demonstrates strong Python practices: proper use of dataclasses, type hints, async/await patterns, and a clear layered architecture that closely follows the design document. Security fundamentals are mostly sound — credentials are loaded from environment variables (never hardcoded), LDAP filter injection is properly prevented via `escape_filter_chars()`, `.env` and `hokeypokey.toml` are in `.gitignore`, and the Docker image runs as a non-root user.

However, the audit identified several findings that warrant attention. The most significant are: (1) a stored XSS vector in the landing page where source names from configuration are interpolated into HTML without escaping, (2) a potential path traversal / SSRF issue in the GitHub source where user-supplied usernames are interpolated directly into API URL paths, (3) the LDAP connection is shared across `asyncio.to_thread()` calls without synchronization creating a potential race condition, (4) the in-memory cache has no size limit, creating an unbounded memory growth risk, and (5) the LDAP source does not explicitly configure TLS verification. None of these are immediately exploitable in a typical deployment (source names come from trusted config, GitHub usernames are typically validated by upstream sources), but they represent defense-in-depth gaps that should be addressed before production deployment.

---

## 2. Critical Findings

### CRITICAL-1: Stored XSS in Landing Page via Source Names

- **File:** `src/hokeypokey/hkp/routes.py`, lines 51-53
- **Severity:** CRITICAL (if source names come from untrusted input) / MEDIUM (if config is trusted)
- **Description:** Source names from the orchestrator are interpolated directly into HTML without any escaping:
  ```python
  sources_html = (
      "<ul>" + "".join(f"<li><code>{s}</code></li>" for s in source_names) + "</ul>"
  ```
  If a source name in `hokeypokey.toml` contains HTML/JavaScript (e.g., `name = "<script>alert(1)</script>"`), it will be rendered as executable HTML in the browser. While source names come from the operator-controlled TOML config file, this violates defense-in-depth principles. The `__version__` string (line 74) is similarly unescaped but comes from source code, making it lower risk.
- **Impact:** An attacker who can modify the TOML config (or if config is ever loaded from an untrusted source) could inject arbitrary JavaScript into the landing page, potentially stealing cookies or session tokens from administrators visiting the page.
- **Recommendation:** Use `markupsafe.escape()` or `html.escape()` on all interpolated values in the HTML template.

### CRITICAL-2: GitHub API Path Traversal / SSRF via Username

- **File:** `src/hokeypokey/sources/github.py`, lines 179, 137-138
- **Severity:** HIGH
- **Description:** User-supplied usernames are interpolated directly into GitHub API URL paths without validation or sanitization:
  ```python
  resp = await self._client.get(f"/users/{username}/gpg_keys")
  ```
  And in `check_freshness`:
  ```python
  resp = await self._client.get(
      f"/users/{username}/gpg_keys",
      headers={"If-None-Match": etag},
  )
  ```
  While `httpx` will URL-encode path segments, a username containing `../` or other path manipulation characters could potentially be used to access unintended API endpoints. The `username` value can originate from LDAP metadata (via resolver) or from the GitHub user search API response, both of which could potentially contain unexpected values.
- **Impact:** In the worst case, a crafted username could cause requests to unintended GitHub API endpoints, potentially leaking the authentication token to unexpected endpoints. The `httpx` library does provide some protection via URL encoding, but explicit validation is the correct defense.
- **Recommendation:** Validate that usernames match `^[a-zA-Z0-9_-]+$` before using them in URL paths.

---

## 3. Important Findings (HIGH / MEDIUM)

### HIGH-1: LDAP Connection Race Condition with `asyncio.to_thread()`

- **File:** `src/hokeypokey/sources/ldap.py`, lines 201-212, 214-243
- **Severity:** HIGH
- **Description:** The `_get_connection()` method creates and stores a single LDAP connection on `self._conn`. The `_ldap_search()` method is called via `asyncio.to_thread()`, which runs it in a thread pool. If multiple concurrent requests trigger LDAP searches simultaneously, multiple threads could call `_get_connection()` and `_ldap_search()` concurrently, sharing the same `ldap3.Connection` object. The `ldap3` library's `Connection` class is not thread-safe — concurrent searches on the same connection will corrupt state.
- **Impact:** Under concurrent load, LDAP searches could return incorrect results, raise exceptions, or corrupt the connection state, leading to service degradation.
- **Recommendation:** Either (a) create a new connection per `_ldap_search()` call (connection pooling), (b) use a threading lock to serialize access, or (c) use `ldap3`'s `SAFE_RESTARTABLE` strategy which handles reconnection.

### HIGH-2: Unbounded Cache Growth — No Eviction Policy

- **File:** `src/hokeypokey/cache.py`, entire class
- **Severity:** HIGH
- **Description:** The `KeyCache` class stores all fetched keys indefinitely in memory. There is no maximum size limit, no LRU eviction, and no periodic cleanup of expired entries. The OVERVIEW.md (line 481) mentions "LRU eviction prevents unbounded growth" as a non-functional requirement, but this is not implemented. Every unique key ever requested will remain in the cache forever (even after TTL expiry — TTL only controls freshness revalidation, not eviction).
- **Impact:** Over time, memory usage will grow monotonically. In a deployment serving many unique keys, this could lead to OOM conditions. The index maps (`_by_email`, `_by_field`, etc.) also grow without bound.
- **Recommendation:** Implement an LRU eviction policy with a configurable `max_entries` parameter. Alternatively, implement periodic cleanup of entries that have been stale for longer than N*TTL.

### MEDIUM-1: LDAP Source Does Not Explicitly Configure TLS Verification

- **File:** `src/hokeypokey/sources/ldap.py`, lines 201-212
- **Severity:** MEDIUM
- **Description:** The LDAP `Server` object is created with only the URI:
  ```python
  server = Server(self._uri)
  ```
  When using `ldaps://` URIs, `ldap3` defaults to using TLS but does not validate the server certificate by default (depends on the `ldap3` version and `Tls` object configuration). There is no explicit `Tls` object configured with `validate=ssl.CERT_REQUIRED` and a CA bundle. This means the connection could be vulnerable to man-in-the-middle attacks.
- **Impact:** An attacker on the network path between hokeypokey and the LDAP server could intercept and modify LDAP traffic, including bind credentials and PGP key data.
- **Recommendation:** Create an explicit `ldap3.Tls` object with `validate=ssl.CERT_REQUIRED` and pass it to the `Server` constructor. Make TLS verification configurable but default to strict.

### MEDIUM-2: GitHub Source Operates Without Token (Silent Degradation)

- **File:** `src/hokeypokey/sources/github.py`, lines 57-67
- **Severity:** MEDIUM
- **Description:** If the environment variable specified by `token_env` is not set, the GitHub source silently operates without authentication:
  ```python
  token = os.environ.get(token_env)
  # ...
  if token:
      headers["Authorization"] = f"token {token}"
  ```
  There is no warning logged when the token is missing. Unauthenticated GitHub API requests have a rate limit of 60 requests/hour (vs. 5,000 authenticated), which will be quickly exhausted in any real deployment.
- **Impact:** The server will silently degrade — GitHub lookups will start failing with 403/429 errors after ~60 requests per hour, with no clear indication of why.
- **Recommendation:** Log a WARNING when `token_env` is configured but the environment variable is not set. Consider making the token required (raise `ConfigError`) or at minimum log the rate limit tier on startup.

### MEDIUM-3: Error Information Leakage in 500 Responses

- **File:** `src/hokeypokey/hkp/routes.py`, lines 131-135
- **Severity:** MEDIUM
- **Description:** When the orchestrator raises an exception, the route handler logs the full exception with `logger.exception()` (which is correct) but returns a generic "Internal server error" message (which is also correct). However, the `ValueError` from `parse_search()` is returned directly to the client:
  ```python
  except ValueError as exc:
      return (str(exc), 400, _ERR_HEADERS)
  ```
  While `ValueError` messages from `parse_search()` are safe (they only contain the user's input and descriptive text), this pattern could leak internal details if other `ValueError` exceptions bubble up from deeper in the stack.
- **Impact:** Low risk currently, but the pattern is fragile — future code changes could cause internal error details to leak to clients.
- **Recommendation:** Catch `ValueError` specifically from `parse_search()` rather than broadly, or sanitize the error message before returning it.

### MEDIUM-4: `app.py` Uses `object` Type Annotation Instead of `KeySource`

- **File:** `src/hokeypokey/app.py`, line 37
- **Severity:** MEDIUM (code quality)
- **Description:** The sources dict is typed as `dict[str, object]` instead of `dict[str, KeySource]`:
  ```python
  sources: dict[str, object] = {}
  ```
  This loses type safety and requires `# type: ignore[arg-type]` on line 64 and `# type: ignore[attr-defined]` on lines 81-84. The type annotation should match the actual type being stored.
- **Impact:** Reduced type safety; mypy/pyright cannot catch type errors in this code path.
- **Recommendation:** Import `KeySource` and use `dict[str, KeySource]` as the type annotation.

---

## 4. Minor Findings (LOW)

### LOW-1: `_REGISTRY` Dict Rebuilt on Every Call to `get_source_class()`

- **File:** `src/hokeypokey/sources/__init__.py`, lines 36-39
- **Severity:** LOW
- **Description:** The `_REGISTRY` dict is defined inside the `get_source_class()` function body, meaning it's rebuilt on every call. While this is done intentionally to avoid circular imports (per the comment), the dict could be built once at module level using a lazy pattern or moved to a separate registry module.
- **Impact:** Negligible performance impact (dict creation is cheap), but slightly non-idiomatic.

### LOW-2: Unused Import in `orchestrator.py`

- **File:** `src/hokeypokey/orchestrator.py`, line 7
- **Severity:** LOW
- **Description:** The `dataclass` import from `dataclasses` is used, but the `_ResolverQuery` and `_FanOutResult` dataclasses defined at module level duplicate the `ResolvedQuery` model. While not strictly unused, the `_ResolverQuery` dataclass is identical in structure to `ResolvedQuery` from `models.py` and could be replaced.
- **Impact:** Minor code duplication.

### LOW-3: `CachedKey.freshness_token` Duplicates `source_key.freshness_token`

- **File:** `src/hokeypokey/models.py`, lines 89-92
- **Severity:** LOW
- **Description:** `CachedKey` has a `freshness_token` field that is initialized from `source_key.freshness_token` in `__post_init__`. This creates two copies of the same string. The orchestrator updates `entry.freshness_token` but not `entry.source_key.freshness_token`, which could lead to inconsistency if both are read.
- **Impact:** Potential confusion about which `freshness_token` is authoritative. Currently the orchestrator correctly uses `entry.freshness_token` (line 298), but this is fragile.

### LOW-4: `_cache_lookup` Has Unused Import

- **File:** `src/hokeypokey/orchestrator.py`, line 261
- **Severity:** LOW
- **Description:** `from hokeypokey.models import CachedKey` is imported inside `_cache_lookup()` but `CachedKey` is only used as a type annotation for the `results` list. This could be moved to the `TYPE_CHECKING` block at the top of the file.

### LOW-5: Duration Parser Does Not Support Days

- **File:** `src/hokeypokey/config.py`, lines 33-64
- **Severity:** LOW
- **Description:** The `parse_duration()` function supports hours, minutes, and seconds but not days. While unlikely to be needed for cache TTLs, the regex pattern could be extended to support `d` for days for completeness.

### LOW-6: No Request Size Limits

- **File:** `src/hokeypokey/hkp/routes.py`
- **Severity:** LOW
- **Description:** There are no explicit request size limits configured. While the server is read-only (POST /pks/add returns 403), Quart/Hypercorn have default limits. The POST endpoint still accepts and parses the request body before returning 403.
- **Impact:** Minimal — the 403 is returned quickly, and Hypercorn has default body size limits.

### LOW-7: `search()` Return Type Inconsistency

- **File:** `src/hokeypokey/sources/ldap.py`, line 77
- **Severity:** LOW
- **Description:** The `search()` method signature declares `-> list[SourceKey] | SearchResult` but the base class declares `-> list[SourceKey]`. The LDAP source returns `SearchResult` objects (which contain both keys and metadata-only entries), while the GitHub source returns `list[SourceKey]`. The orchestrator handles both via `isinstance` checks (lines 236-244), but the type signatures are inconsistent with the ABC.
- **Impact:** Type checkers will flag this as a Liskov Substitution Principle violation. The ABC should be updated to declare `SearchResult` as the return type, or a union type.

### LOW-8: No `OPTIONS` / Preflight CORS Handler

- **File:** `src/hokeypokey/hkp/routes.py`
- **Severity:** LOW
- **Description:** While `Access-Control-Allow-Origin: *` is set on all responses, there is no handler for `OPTIONS` preflight requests. Browsers making cross-origin requests with custom headers will send an `OPTIONS` preflight that will return 405 Method Not Allowed.
- **Impact:** Browser-based JavaScript clients cannot make cross-origin requests to the keyserver. This is unlikely to be a real use case (GPG clients don't use browsers), but the CORS header implies cross-origin support.

---

## 5. Positive Observations

### Architecture & Design
1. **Excellent separation of concerns.** The layered architecture (routes → orchestrator → cache → sources) is clean and well-defined. Each component has a single responsibility.
2. **Plugin system is well-designed.** The `KeySource` ABC provides a clear contract. Adding new source types requires only implementing the interface and registering in the registry.
3. **Resolver pattern is elegant.** The declarative, config-driven resolver system avoids coupling between sources while enabling powerful cross-source lookups.
4. **`SearchResult` with metadata-only entries** is a thoughtful addition that enables the keyless-LDAP-to-GitHub flow — a real-world scenario where an LDAP entry has no PGP key but has a GitHub username that can be resolved.

### Security
5. **Credentials are never stored in config files.** The `bind_password_env` and `token_env` pattern correctly loads secrets from environment variables.
6. **`.env` and `hokeypokey.toml` are in `.gitignore`** (lines 138, 213), preventing accidental credential commits.
7. **LDAP filter injection is properly prevented** using `ldap3.utils.conv.escape_filter_chars()` (ldap.py lines 94, 141).
8. **Docker image runs as non-root** (Dockerfile line 45: `USER hokeypokey`).
9. **Read-only server** — POST /pks/add correctly returns 403, eliminating an entire class of write-path vulnerabilities.
10. **Error responses use `text/plain`** content type, preventing HTML injection in error messages.

### Code Quality
11. **Comprehensive type hints** throughout the codebase. Dataclasses are used appropriately for data transfer objects.
12. **Consistent logging** with appropriate levels (DEBUG for expected conditions, WARNING for errors, INFO for lifecycle events).
13. **`imghdr` compatibility shim** (both in `__init__.py` and root `conftest.py`) is a pragmatic solution for the pgpy/Python 3.13 incompatibility.
14. **Async patterns are correct** — `asyncio.to_thread()` is used properly to wrap synchronous LDAP operations, and `asyncio.gather()` is used for concurrent fan-out.

### Testing
15. **166 tests, all passing.** Excellent coverage across all components.
16. **Test isolation is good** — mock sources, patched LDAP connections, and `pytest-httpx` for GitHub API mocking.
17. **Integration tests cover real-world scenarios** including cold cache, warm cache, stale cache, resolver chaining, priority deduplication, and the keyless-LDAP-to-GitHub flow.
18. **Edge cases are tested** — LDAP filter injection, invalid hex, empty strings, rate limiting, missing entries.

### Configuration & Deployment
19. **TOML config validation is thorough** — unique source names, valid resolver references, globally unique field names, positive priorities.
20. **Docker multi-stage build** keeps the final image lean.
21. **`hokeypokey.example.toml`** is well-documented with extensive comments.

---

## 6. Architecture Compliance

### Alignment with `.state/OVERVIEW.md`

The implementation closely follows the architectural overview with the following observations:

| Aspect | OVERVIEW.md | Implementation | Status |
|--------|-------------|----------------|--------|
| Framework | Quart (ASGI) | Quart | Aligned |
| LDAP client | ldap3 | ldap3 | Aligned |
| HTTP client | httpx | httpx | Aligned |
| PGP parsing | pgpy | pgpy | Aligned |
| Cache backend | In-memory, LRU eviction | In-memory, **no eviction** | **DEVIATION** |
| HKP ops | get, index, vindex, add(403) | get, index, vindex, add(403) | Aligned |
| Landing page | HTML status page | HTML status page | Aligned |
| Source interface | ABC with 5 abstract methods | ABC with 5 abstract methods | Aligned |
| Resolver | Declarative ConfigResolver | Declarative ConfigResolver | Aligned |
| Depth limit | Default 2 | Default 2 | Aligned |
| Cycle detection | Visited set | Visited set | Aligned |
| Priority ranking | Lower number wins | Lower number wins | Aligned |
| LDAP freshness | modifyTimestamp | modifyTimestamp | Aligned |
| GitHub freshness | ETag/conditional GET | ETag/conditional GET | Aligned |
| Dotenv support | python-dotenv | python-dotenv | Aligned |
| Structured logging | Timestamps + levels | Timestamps + levels | Aligned |

**Deviations:**

1. **Cache eviction not implemented** (HIGH-2 above). OVERVIEW.md line 481 states "LRU eviction prevents unbounded growth" but no eviction is implemented. The cache grows without bound.

2. **`search()` return type extended.** The OVERVIEW.md defines `search()` as returning `list[SourceKey]`, but the LDAP implementation returns `SearchResult` (which includes `metadata_only` entries). This is a post-design addition that enables the keyless-LDAP-to-GitHub flow and is documented as such. The orchestrator handles both return types.

3. **`SourceMetadata` and `SearchResult` models added.** These are not in the original OVERVIEW.md but are necessary for the metadata-only resolver flow. This is a well-motivated addition.

4. **`text_searchable` field on `FieldDefinition`.** Added to prevent GitHub fields from participating in unqualified text searches. This is a sensible addition not in the original design.

---

## 7. Recommendations (Prioritized)

### Priority 1 — Security (Address Before Production)

1. **Escape HTML in landing page** (CRITICAL-1): Use `html.escape()` on all interpolated values in the HTML template at `routes.py:53`.

2. **Validate GitHub usernames** (CRITICAL-2): Add a regex check (`^[a-zA-Z0-9_-]+$`) in `_fetch_keys_for_username()` and `check_freshness()` before interpolating into URL paths.

3. **Fix LDAP connection thread safety** (HIGH-1): Create a new connection per `_ldap_search()` call, or use a connection pool with a threading lock.

4. **Configure LDAP TLS verification** (MEDIUM-1): Create an explicit `ldap3.Tls` object with certificate validation enabled by default.

5. **Warn on missing GitHub token** (MEDIUM-2): Log a WARNING when `token_env` is configured but the environment variable is empty/missing.

### Priority 2 — Reliability

6. **Implement cache eviction** (HIGH-2): Add a `max_entries` config option and implement LRU eviction in `KeyCache`. This is listed as a requirement in OVERVIEW.md but not implemented.

7. **Fix `search()` return type in ABC** (LOW-7): Update `KeySource.search()` to return `SearchResult | list[SourceKey]` or standardize on `SearchResult`.

### Priority 3 — Code Quality

8. **Fix type annotation in `app.py`** (MEDIUM-4): Change `dict[str, object]` to `dict[str, KeySource]`.

9. **Add `OPTIONS` handler for CORS** (LOW-8): If cross-origin browser access is intended.

10. **Consider adding health check endpoint** (`/healthz` or similar) for container orchestration readiness/liveness probes.

---

## 8. Dependency Analysis

| Dependency | Version Constraint | Known Issues |
|------------|-------------------|--------------|
| `quart>=0.19` | Current | No known CVEs |
| `hypercorn>=0.17` | Current | No known CVEs |
| `ldap3>=2.9` | Current | Deprecation warnings for `pyasn1` (visible in test output) |
| `httpx>=0.27` | Current | No known CVEs |
| `pgpy>=0.6` | Current | Requires `imghdr` shim on Python 3.13; project appears unmaintained |
| `python-dotenv>=1.0` | Current | No known CVEs |

**Note:** `pgpy` is the highest-risk dependency. The project has limited maintenance activity, and the `imghdr` shim is a workaround for a compatibility issue that should ideally be fixed upstream. Consider monitoring for alternatives or forks.

---

## 9. Test Coverage Analysis

| Component | Test File | Tests | Coverage Assessment |
|-----------|-----------|-------|-------------------|
| Config | `test_config.py` | 12 | Good — covers parsing, validation, edge cases |
| Cache | `test_cache.py` | 17 | Excellent — priority, indexes, freshness, removal |
| Search parser | `test_search.py` | 12 | Excellent — all search types, edge cases |
| Resolver | `test_resolver.py` | 7 | Good — all conditions tested |
| Orchestrator | `test_orchestrator.py` | 9 | Good — cache hit/miss, resolvers, cycles, depth |
| HKP routes | `test_hkp_routes.py` | 13 | Excellent — all ops, errors, CORS |
| HKP formatter | `test_hkp_formatter.py` | 12 | Good — structure, encoding, edge cases |
| GitHub source | `test_source_github.py` | 9 | Good — search, freshness, rate limiting |
| LDAP source | `test_source_ldap.py` | 11 | Good — filter construction, injection, freshness |
| App factory | `test_app.py` | 7 | Good — wiring, end-to-end basics |
| Integration | `test_integration.py` | 13 | Excellent — 13 real-world scenarios |

**Missing test scenarios:**
- No test for concurrent LDAP searches (would expose HIGH-1)
- No test for cache memory growth under load
- No test for the landing page HTML content (would catch CRITICAL-1)
- No test for GitHub username validation (would catch CRITICAL-2)
- No test for `close()` being called on sources during shutdown
- No negative test for `parse_duration("0s")` (zero duration — currently raises ConfigError, which is correct)
- No test for TOML parse errors (malformed TOML file)

---

*End of audit report.*
