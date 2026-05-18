# hokeypokey

A read-only HKP/HKPS-compliant GPG keyserver that lazily fetches keys from pluggable upstream sources (LDAP, GitHub) and serves them to standard `gpg --keyserver` clients as a drop-in replacement.

## Overview

Organizations have GPG keys scattered across multiple authoritative sources — corporate LDAP directories, GitHub accounts, and potentially other systems. There is no unified, HKP-compliant keyserver that can federate across these sources, present a coherent view, and let operators control which sources are most authoritative.

Hokeypokey solves this by implementing a read-only HKP/HKPS keyserver that fetches keys on demand from pluggable upstream sources, caches them with source-specific freshness checks, and serves them to standard GPG clients. Keys are never submitted to hokeypokey — they are only added through upstream sources. The trust model is simple: **if a key exists in a configured source, it is trusted to the degree that source is trusted.** A key in corporate LDAP is trusted because the organization controls that directory. A key on a GitHub account is trusted because GitHub verified the account.

When the same fingerprint appears in multiple sources, the source with the lowest priority number (highest authority) wins — its version of the key is the one served. No merging of subkeys or signatures across sources.

## Features

- **Drop-in HKP(S) compatibility** — fully compatible with `gpg --keyserver hkp://` and `gpg --keyserver hkps://`
- **Lazy loading with smart freshness checks** — keys are fetched on demand and cached locally; freshness is validated using lightweight, source-specific mechanisms (LDAP `modifyTimestamp`, GitHub `ETag`/`304`)
- **Pluggable source architecture** — modular system where each upstream key source (LDAP, GitHub, etc.) is a self-contained plugin implementing a common interface
- **Cross-source search resolvers** — declarative TOML configuration bridges sources; when a result from one source contains metadata that maps to another source, the resolver automatically triggers a follow-up query
- **Priority-ranked sources** — each source instance carries a numeric priority; lower number = more authoritative; same fingerprint from multiple sources → lower priority number wins
- **Flexible search** — keys are searchable by email, key ID, fingerprint, and custom indexed fields (configurable per source)
- **Dotenv support** — automatically loads `.env` files for credentials; no need to `export` in your shell
- **Read-only** — no key submission endpoint; keys are added only through upstream sources
- **CORS headers on all responses** — `Access-Control-Allow-Origin: *` for browser-based clients
- **Deployable via `uv` or Docker** — modern Python packaging with Docker image and Compose support

## How It Works

### Lazy Loading

Keys are fetched from upstream sources **on demand** when a client requests them, not during startup. Once fetched, keys are cached locally. This approach avoids rate limit issues with GitHub, avoids pulling massive LDAP directories, and means the server is ready to serve immediately — it just has an empty (cold) cache.

### Freshness Checks

On cache hit, hokeypokey validates freshness using lightweight, source-specific mechanisms before deciding whether to refetch:

| Source | Mechanism | How It Works |
|--------|-----------|--------------|
| LDAP | `modifyTimestamp` operational attribute | Query LDAP for only the `modifyTimestamp` of the entry. If unchanged since last fetch, serve from cache. If changed, refetch the full key. |
| GitHub | HTTP `HEAD` request + `ETag`/`Last-Modified` | Send a `HEAD` (or conditional `GET` with `If-None-Match`) to the GitHub API endpoint. `304 Not Modified` = serve from cache. Otherwise refetch. |

Each cached entry stores the key data, the source it came from (and that source's priority), a source-specific freshness token, and a configurable TTL after which freshness must be re-validated.

### Sources

Sources are pluggable modules; each has a priority, TTL, and field mappings. Built-in sources include:

- **LDAP** — connects to LDAP servers, searches by configurable attributes, validates freshness via `modifyTimestamp`
- **GitHub** — fetches GPG keys from GitHub user accounts, validates freshness via `ETag`

### Resolvers

Search resolvers are declarative bridges between sources. When a source returns metadata containing a field that maps to another source, the resolver fires a follow-up query. Example:

> LDAP stores each user's GitHub username in a custom attribute (`githubUsername`). When a search finds an LDAP key, the resolver automatically fetches the user's GitHub GPG keys too.

Resolvers are configured in TOML and are evaluated with a configurable depth limit (default: 2) to prevent infinite loops.

### Priority

When the same fingerprint appears in multiple sources, the source with the **lowest priority number** (highest authority) wins — its version of the key is the one served. No merging of subkeys or signatures across sources.

## Installation

### Via uv (recommended)

```bash
# Install from PyPI
uv pip install hokeypokey

# Or run directly without installing
uvx hokeypokey --config hokeypokey.toml

# Or from source
git clone https://github.com/your-org/hokeypokey.git
cd hokeypokey
uv sync
uv run hokeypokey --config hokeypokey.toml
```

### Via Docker

```bash
docker build -t hokeypokey .
docker run -v ./hokeypokey.toml:/etc/hokeypokey/hokeypokey.toml:ro \
           -p 11371:11371 \
           hokeypokey
```

### Via Docker Compose

```bash
# Copy the example config and edit it
cp hokeypokey.example.toml hokeypokey.toml
# edit hokeypokey.toml

# Create a .env file for credentials
cat > .env <<EOF
LDAP_BIND_PASSWORD=your_password
GITHUB_TOKEN=ghp_your_token
EOF

docker compose up
```

## Configuration

Configuration is via a single TOML file (default: `hokeypokey.toml`). See `hokeypokey.example.toml` for a complete annotated example.

### Server Settings

```toml
[server]
host = "0.0.0.0"           # Bind address
port = 11371               # Standard HKP port
# tls_cert = "/path/to/cert.pem"  # Optional: TLS certificate for HKPS
# tls_key  = "/path/to/key.pem"   # Optional: TLS key for HKPS
```

### Cache Settings

```toml
[cache]
backend = "memory"         # Currently only "memory" is supported
default_ttl = "10m"        # Default time before freshness re-validation
```

### LDAP Source

```toml
[[sources]]
name = "corporate-ldap"
type = "ldap"
priority = 10              # Lower = more authoritative
ttl = "5m"                 # Override default TTL for this source

[sources.config]
uri = "ldaps://ldap.corp.example.com"
base_dn = "ou=people,dc=corp,dc=example,dc=com"
bind_dn = "cn=readonly,dc=corp,dc=example,dc=com"
bind_password_env = "LDAP_BIND_PASSWORD"  # Never store password in config!
key_attribute = "pgpKey"   # LDAP attribute containing the PGP key
search_filter = "(pgpKey=*)"  # Base filter for all searches
# fingerprint_attribute = "pgpCertID"  # Optional: enables fingerprint lookup

# Field mappings: logical field name → LDAP attribute
[sources.config.fields]
email = "mail"
username = "uid"
employee_id = "employeeNumber"
github_id = "githubUsername"  # Custom attribute for resolver
```

### GitHub Source

```toml
[[sources]]
name = "github-org"
type = "github"
priority = 50
ttl = "15m"

[sources.config]
token_env = "GITHUB_TOKEN"  # Never store token in config!
# api_base = "https://github.example.com/api/v3"  # For GitHub Enterprise

# Field mappings: logical field name → GitHub response field
[sources.config.fields]
github_username = "login"   # Search by GitHub username
# github_email = "email"    # Uncomment to enable email search
```

### Search Resolvers

```toml
[[resolvers]]
name = "ldap-to-github"
trigger_source = "corporate-ldap"
trigger_field = "github_id"      # Metadata field from LDAP results
target_source = "github-org"
target_field = "github_username" # Search field in GitHub source
```

When an LDAP search result contains a `github_id` field, this resolver automatically queries the GitHub source for that username's GPG keys.

### Duration Format

Durations are specified as strings with combinations of hours, minutes, and seconds:

- `"30s"` — 30 seconds
- `"5m"` — 5 minutes
- `"1h"` — 1 hour
- `"2h30m"` — 2 hours and 30 minutes
- `"1h15m30s"` — 1 hour, 15 minutes, and 30 seconds

### Credentials

**Important:** Credentials are **never** stored in the configuration file. Hokeypokey uses `python-dotenv` to automatically load credentials from a `.env` file in the current directory.

Create a `.env` file with your credentials:

```bash
LDAP_BIND_PASSWORD=your_password
GITHUB_TOKEN=ghp_your_token
```

Hokeypokey will automatically load this file when it starts. The `.env` file is already in `.gitignore` to prevent accidental commits.

To use a custom `.env` file path, use the `--env-file` flag:

```bash
hokeypokey --config hokeypokey.toml --env-file /path/to/custom.env
```

In your TOML configuration, reference these environment variables:

- LDAP bind password: `bind_password_env = "LDAP_BIND_PASSWORD"`
- GitHub token: `token_env = "GITHUB_TOKEN"`

## Usage with GPG

### One-Time Lookups

```bash
# Search for keys by email
gpg --keyserver hkp://localhost:11371 --search-keys user@example.com

# Retrieve a key by fingerprint
gpg --keyserver hkp://localhost:11371 --recv-keys 0xFINGERPRINT

# Retrieve a key by key ID
gpg --keyserver hkp://localhost:11371 --recv-keys 0xKEYID
```

### Set as Default Keyserver

Add to `~/.gnupg/gpg.conf`:

```
keyserver hkp://hokeypokey.corp.example.com:11371
```

Or for HKPS (TLS):

```
keyserver hkps://hokeypokey.corp.example.com
```

Then use `gpg --search-keys` and `gpg --recv-keys` without specifying the keyserver.

## HKP API Reference

Hokeypokey implements the read-only portion of the HKP specification. All responses include `Access-Control-Allow-Origin: *` headers.

### Endpoints

| Method | Path | Parameters | Description |
|--------|------|-----------|-------------|
| GET | `/` | — | HTML status page (browser-friendly) |
| GET | `/pks/lookup` | `op=get&search=...` | Retrieve ASCII-armored key(s) |
| GET | `/pks/lookup` | `op=index&search=...` | Machine-readable key index |
| GET | `/pks/lookup` | `op=vindex&search=...` | Verbose key index (same as index) |
| POST | `/pks/add` | — | Always returns 403 Forbidden (read-only) |

### Search Parameter Formats

The `search` parameter supports multiple formats:

- `0x<40 hex>` — V4 fingerprint (e.g., `0x1234567890ABCDEF1234567890ABCDEF12345678`)
- `0x<16 hex>` — Long key ID (e.g., `0x1234567890ABCDEF`)
- `0x<8 hex>` — Short key ID (e.g., `0x12345678`) — accepted but discouraged
- `user@example.com` — Email address
- `John Doe` — Free-text UID search
- Custom indexed fields — any field configured in source field mappings

### Response Formats

**`op=get`** returns ASCII-armored key data:

```
-----BEGIN PGP PUBLIC KEY BLOCK-----
...
-----END PGP PUBLIC KEY BLOCK-----
```

**`op=index`** and **`op=vindex`** return machine-readable key listings:

```
info:1:1
pub:<keyid>:<algo>:<keylen>:<creationdate>:<expirationdate>:<flags>
uid:<uidhash>:<creationdate>:<expirationdate>:<flags>:<uid>
```

## CLI Reference

hokeypokey [--config PATH] [--host HOST] [--port PORT] [--env-file PATH] [--log-level LEVEL]

| Flag | Default | Description |
|------|---------|-------------|
| `--config PATH` | `hokeypokey.toml` | Path to the TOML configuration file |
| `--host HOST` | from config | Override the bind host |
| `--port PORT` | from config | Override the bind port |
| `--env-file PATH` | `.env` (auto) | Path to a `.env` file for credentials |
| `--log-level LEVEL` | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

## Writing a Custom Source Plugin

To add a new key source (e.g., a custom directory system), subclass `KeySource` and implement the required methods:

```python
from hokeypokey.sources.base import KeySource
from hokeypokey.models import FieldDefinition, SourceKey

class MyCustomSource(KeySource):
    """Custom key source plugin."""

    async def search(self, query: str, field: str = "email") -> list[SourceKey]:
        """Search for keys matching query against the named field.
        
        Args:
            query: The search term (email, username, custom ID, etc.)
            field: The logical field name to search against
            
        Returns:
            List of matching SourceKey objects with metadata and freshness tokens
        """
        # Implement your search logic here
        # Return a list of SourceKey objects
        ...

    async def fetch_by_fingerprint(self, fingerprint: str) -> SourceKey | None:
        """Fetch a specific key by its fingerprint.
        
        Args:
            fingerprint: Uppercase hex fingerprint without 0x prefix
            
        Returns:
            The matching SourceKey, or None if not found or unsupported
        """
        # Implement fingerprint-based lookup
        # Return None if your source doesn't support this
        ...

    async def check_freshness(self, fingerprint: str, token: str) -> bool:
        """Check if a cached key is still fresh.
        
        This should be a lightweight check — avoid fetching full key data.
        
        Args:
            fingerprint: The key's fingerprint
            token: The opaque freshness token stored when the key was cached
            
        Returns:
            True if the cached version is still current, False if it needs refetching
        """
        # Implement your freshness check logic
        # Examples: compare timestamps, send conditional HTTP requests, etc.
        ...

    def searchable_fields(self) -> list[FieldDefinition]:
        """Declare the fields this source can search against.
        
        Returns:
            List of FieldDefinition objects describing each searchable field
        """
        return [
            FieldDefinition(name="email", source_attribute="email_attr", searchable=True),
            FieldDefinition(name="username", source_attribute="user_attr", searchable=True),
        ]

    async def close(self) -> None:
        """Release any resources held by this source."""
        # Close connections, cleanup, etc.
        ...
```

### Registering Your Source

1. Place your source module in `src/hokeypokey/sources/`
2. Register it in `src/hokeypokey/sources/__init__.py` in the `get_source_class()` function:

```python
def get_source_class(source_type: str) -> type[KeySource]:
    sources = {
        "ldap": LDAPSource,
        "github": GitHubSource,
        "mycustom": MyCustomSource,  # Add your source here
    }
    ...
```

3. Use it in your configuration:

```toml
[[sources]]
name = "my-custom"
type = "mycustom"
priority = 20

[sources.config]
# Your custom configuration here
```

### Key Concepts

- **Freshness token**: An opaque string your source defines and later uses in `check_freshness()`. Examples: LDAP `modifyTimestamp`, HTTP `ETag`, database row version, etc.
- **SourceKey**: Bundles the raw ASCII-armored (or binary) public key data, the fingerprint, a dict of metadata/index field values, and a source-specific freshness token.
- **Searchable fields**: Declare what fields your source can search against via `searchable_fields()`. Field names must be globally unique across all sources.
- **Fingerprint lookup**: If your source doesn't support fingerprint-based lookup, return `None` from `fetch_by_fingerprint()`. The system will rely on other sources or prior cache entries.

## Architecture

```
                         +---------------------+
                         |   GPG Clients        |
                         |  (gpg --keyserver)   |
                         +----------+----------+
                                    |
                              HKP / HKPS
                                    |
                         +----------v----------+
                         |   Quart HTTP Server  |
                         |   (HKP Endpoints)    |
                         +----------+----------+
                                    |
                         +----------v----------+
                         |   Search Orchestrator |
                         |   (resolve + rank)   |
                         +----------+----------+
                                    |
                    +---------------+---------------+
                    |               |               |
             +------v------+ +------v------+ +------v------+
             |   Search     | |   Search    | |   Search    |
             |  Resolvers   | |  Resolvers  | |  Resolvers  |
             +------+------+ +------+------+ +------+------+
                    |               |               |
             +------v------+ +------v------+ +------v------+
             |    Cache     | |    Cache    | |    Cache    |
             |   (per src)  | |  (per src)  | |  (per src)  |
             +------+------+ +------+------+ +------+------+
                    |               |               |
             +------v------+ +------v------+ +------v------+
             | LDAP Source  | |GitHub Source | | Custom Src  |
             | (plugin)     | | (plugin)    | | (plugin)    |
             +------+------+ +------+------+ +------+------+
                    |               |               |
                LDAP(S)        GitHub API         ???
```

### Components

**Quart HTTP Server (HKP Endpoints)** — Implements the HKP protocol over HTTP and HTTPS (HKPS). Parses HKP query parameters, returns responses in standard HKP format, and returns 403 Forbidden for key submission attempts.

**Search Orchestrator** — Coordinates search requests across all sources. Fans out queries concurrently, evaluates search resolvers, deduplicates results by fingerprint, ranks by source priority, and caches discovered keys.

**Key Cache** — Per-source cache layer that stores fetched keys with freshness tokens and metadata. Maintains search indexes for fingerprint, key ID, email, and custom fields. Configurable TTL and eviction policy.

**Source Interface** — Abstract base class that every key source plugin implements. Defines methods for searching, fetching by fingerprint, checking freshness, and declaring searchable fields.

**Search Resolvers** — Declarative bridges between sources. When a result from one source contains metadata that triggers a resolver, a follow-up query is fired in the target source.

## Development

```bash
# Clone the repository
git clone https://github.com/your-org/hokeypokey.git
cd hokeypokey

# Install dependencies (including dev tools)
uv sync --extra dev

# Run the server locally
uv run hokeypokey --config hokeypokey.example.toml

# Run tests
uv run pytest

# Run linter
uv run ruff check src/

# Format code
uv run ruff format src/
```

## License

Apache 2.0 — see LICENSE file.
