"""HKP endpoint routes as a Quart Blueprint.

Implements the HTTP Keyserver Protocol (HKP) read-only endpoints:
  GET  /pks/lookup   — key retrieval and search
  POST /pks/add      — always returns 403 (read-only server)

Reference: draft-shaw-openpgp-hkp-00 / draft-gallagher-openpgp-hkp-09
"""

from __future__ import annotations

import logging

from quart import Blueprint, current_app, request

from hokeypokey.hkp.formatter import format_get_response, format_index_response
from hokeypokey.search import parse_search

logger = logging.getLogger(__name__)

hkp_bp = Blueprint("hkp", __name__)

# Operations we understand
_KNOWN_OPS = {"get", "index", "vindex"}

# CORS header required on all machine-readable responses (Gallagher draft)
_CORS_HEADERS = {"Access-Control-Allow-Origin": "*"}


def _orchestrator():
    """Retrieve the SearchOrchestrator from the current app's extensions."""
    return current_app.extensions["orchestrator"]


# ---------------------------------------------------------------------------
# GET /pks/lookup
# ---------------------------------------------------------------------------

@hkp_bp.route("/pks/lookup", methods=["GET"])
async def lookup():
    op = request.args.get("op", "").strip().lower()
    search_term = request.args.get("search", "").strip()
    options = request.args.get("options", "").strip().lower()

    # --- Validate required parameters ---
    if not op:
        return (
            "Missing required parameter: op",
            400,
            _CORS_HEADERS,
        )
    if not search_term:
        return (
            "Missing required parameter: search",
            400,
            _CORS_HEADERS,
        )

    # --- Dispatch unknown operations ---
    if op not in _KNOWN_OPS:
        return (
            f"Operation not supported: {op!r}",
            501,
            _CORS_HEADERS,
        )

    # --- Parse search term ---
    try:
        parsed = parse_search(search_term)
    except ValueError as exc:
        return (str(exc), 400, _CORS_HEADERS)

    # --- Execute lookup ---
    orchestrator = _orchestrator()
    try:
        keys = await orchestrator.lookup(parsed)
    except Exception as exc:
        logger.exception("Orchestrator lookup failed: %s", exc)
        return ("Internal server error", 500, _CORS_HEADERS)

    if not keys:
        return ("No keys found", 404, _CORS_HEADERS)

    # --- Format response ---
    if op == "get":
        body = format_get_response(keys)
        return (
            body,
            200,
            {**_CORS_HEADERS, "Content-Type": "application/pgp-keys"},
        )

    # op == "index" or "vindex"
    body = format_index_response(keys)
    return (
        body,
        200,
        {**_CORS_HEADERS, "Content-Type": "text/plain; charset=utf-8"},
    )


# ---------------------------------------------------------------------------
# POST /pks/add  — read-only; always reject
# ---------------------------------------------------------------------------

@hkp_bp.route("/pks/add", methods=["POST"])
async def add():
    return (
        "Keyserver is read-only. Key submission is not supported.",
        403,
        _CORS_HEADERS,
    )
