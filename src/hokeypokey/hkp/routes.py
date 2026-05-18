"""HKP endpoint routes as a Quart Blueprint.

Implements the HTTP Keyserver Protocol (HKP) read-only endpoints:
  GET  /             — human-readable status/info page
  GET  /pks/lookup   — key retrieval and search
  POST /pks/add      — always returns 403 (read-only server)

Reference: draft-shaw-openpgp-hkp-00 / draft-gallagher-openpgp-hkp-09
"""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING

from quart import Blueprint, current_app, request
from quart.typing import ResponseReturnValue

if TYPE_CHECKING:
    from hokeypokey.orchestrator import SearchOrchestrator

from hokeypokey import __version__
from hokeypokey.hkp.formatter import format_get_response, format_index_response
from hokeypokey.search import parse_search

logger = logging.getLogger(__name__)

hkp_bp = Blueprint("hkp", __name__)

# Operations we understand
_KNOWN_OPS = {"get", "index", "vindex"}

# Content-Type for plain-text error responses
_PLAIN = "text/plain; charset=utf-8"

# CORS header required on all responses (Gallagher HKP draft)
_CORS_HEADERS = {"Access-Control-Allow-Origin": "*"}

# Combined headers for plain-text error responses
_ERR_HEADERS = {**_CORS_HEADERS, "Content-Type": _PLAIN}

# Preflight response headers for OPTIONS requests
_CORS_PREFLIGHT_HEADERS = {
    **_CORS_HEADERS,
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
}


def _orchestrator() -> SearchOrchestrator:
    """Retrieve the SearchOrchestrator from the current app's extensions."""
    return current_app.extensions["orchestrator"]  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# GET /  — human-readable landing page
# ---------------------------------------------------------------------------


@hkp_bp.route("/", methods=["GET"])
async def index() -> ResponseReturnValue:
    """Return a simple HTML status page for browser visitors."""
    orchestrator = _orchestrator()
    source_names = list(orchestrator._sources.keys())
    sources_html = (
        "<ul>" + "".join(f"<li><code>{html.escape(s)}</code></li>" for s in source_names) + "</ul>"
        if source_names
        else "<p><em>No sources configured.</em></p>"
    )

    page_html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>hokeypokey keyserver</title>
  <style>
    body {{ font-family: monospace; max-width: 720px; margin: 2rem auto; padding: 0 1rem; }}
    h1 {{ font-size: 1.4rem; }}
    code {{ background: #f4f4f4; padding: 0.1em 0.3em; border-radius: 3px; }}
    pre {{ background: #f4f4f4; padding: 1rem; overflow-x: auto; border-radius: 4px; }}
    .ok {{ color: #2a2; font-weight: bold; }}
  </style>
</head>
<body>
  <h1>hokeypokey <span class="ok">&#x25cf; running</span></h1>
  <p>Version: <code>{__version__}</code> &mdash; Read-only HKP/HKPS keyserver</p>

  <h2>Configured sources</h2>
  {sources_html}

  <h2>Usage</h2>
  <p>Search for a key by email:</p>
  <pre>gpg --keyserver hkp://localhost:11371 --search-keys user@example.com</pre>
  <p>Retrieve a key by fingerprint:</p>
  <pre>gpg --keyserver hkp://localhost:11371 --recv-keys 0xFINGERPRINT</pre>
  <p>Or query the API directly:</p>
  <pre>curl "http://localhost:11371/pks/lookup?op=index&amp;search=user@example.com&amp;options=mr"</pre>

  <h2>Endpoints</h2>
  <table>
    <tr><th>Method</th><th>Path</th><th>Description</th></tr>
    <tr>
      <td>GET</td>
      <td><code>/pks/lookup?op=get&amp;search=...</code></td>
      <td>Retrieve ASCII-armored key</td>
    </tr>
    <tr>
      <td>GET</td>
      <td><code>/pks/lookup?op=index&amp;search=...</code></td>
      <td>Machine-readable key index</td>
    </tr>
    <tr><td>POST</td><td><code>/pks/add</code></td><td>403 &mdash; read-only server</td></tr>
  </table>

  <p style="margin-top:2rem; color:#888; font-size:0.85rem;">
    <a href="https://github.com/wells/hokeypokey">hokeypokey</a> &mdash; Apache 2.0
  </p>
</body>
</html>
"""
    return (page_html, 200, {**_CORS_HEADERS, "Content-Type": "text/html; charset=utf-8"})


# ---------------------------------------------------------------------------
# GET /pks/lookup
# ---------------------------------------------------------------------------


@hkp_bp.route("/pks/lookup", methods=["GET", "OPTIONS"])
async def lookup() -> ResponseReturnValue:
    if request.method == "OPTIONS":
        return ("", 204, _CORS_PREFLIGHT_HEADERS)

    op = request.args.get("op", "").strip().lower()
    search_term = request.args.get("search", "").strip()

    # --- Validate required parameters ---
    if not op:
        return ("Missing required parameter: op", 400, _ERR_HEADERS)
    if not search_term:
        return ("Missing required parameter: search", 400, _ERR_HEADERS)

    # --- Dispatch unknown operations ---
    if op not in _KNOWN_OPS:
        return (f"Operation not supported: {op!r}", 501, _ERR_HEADERS)

    # --- Parse search term ---
    try:
        parsed = parse_search(search_term)
    except ValueError:
        return ("Invalid search term", 400, _ERR_HEADERS)

    # --- Execute lookup ---
    orchestrator = _orchestrator()
    try:
        keys = await orchestrator.lookup(parsed)
    except Exception as exc:
        logger.exception("Orchestrator lookup failed: %s", exc)
        return ("Internal server error", 500, _ERR_HEADERS)

    if not keys:
        return ("No keys found", 404, _ERR_HEADERS)

    # --- Format response ---
    if op == "get":
        body = format_get_response(keys)
        return (body, 200, {**_CORS_HEADERS, "Content-Type": "application/pgp-keys"})

    # op == "index" or "vindex"
    body = format_index_response(keys)
    return (body, 200, {**_CORS_HEADERS, "Content-Type": "text/plain; charset=utf-8"})


# ---------------------------------------------------------------------------
# POST /pks/add  — read-only; always reject
# ---------------------------------------------------------------------------


@hkp_bp.route("/pks/add", methods=["POST"])
async def add() -> ResponseReturnValue:
    return (
        "Keyserver is read-only. Key submission is not supported.",
        403,
        _ERR_HEADERS,
    )


# ---------------------------------------------------------------------------
# GET /healthz  — liveness/readiness probe
# ---------------------------------------------------------------------------


@hkp_bp.route("/healthz", methods=["GET"])
async def healthz() -> ResponseReturnValue:
    source_count = len(_orchestrator()._sources)
    return (
        f"ok\nsources: {source_count}\n",
        200,
        {**_CORS_HEADERS, "Content-Type": _PLAIN},
    )
