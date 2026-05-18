"""GitHub key source plugin."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx
import pgpy

from hokeypokey.models import FieldDefinition, SearchResult, SourceKey
from hokeypokey.sources.base import KeySource

logger = logging.getLogger(__name__)

_FRESHNESS_SEP = "|||"
_DEFAULT_API_BASE = "https://api.github.com"

# GitHub username rules: alphanumeric and hyphens, no leading/trailing hyphen,
# 1–39 characters.  See https://docs.github.com/en/github/creating-cloning-and-archiving-repositories/about-repositories
_GITHUB_USERNAME_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?$")


class GitHubSource(KeySource):
    """Key source that fetches GPG keys from GitHub user accounts.

    Configuration keys (under ``[sources.config]``):

    ==================  ============================================================
    Key                 Description
    ==================  ============================================================
    ``token_env``       Environment variable holding the GitHub personal access token
    ``api_base``        GitHub API base URL (default: ``https://api.github.com``)
    ``fields``          Mapping of logical field name → GitHub response field name.
                        Supported GitHub field names: ``"login"`` (username),
                        ``"email"`` (primary email from user search).
    ==================  ============================================================

    Freshness token format: ``"<username>|||<etag>"``

    Search behaviour:

    - Field mapped to ``"login"``: fetch ``GET /users/{query}/gpg_keys`` directly.
    - Field mapped to ``"email"``: search users by email via
      ``GET /search/users?q={email}+in:email``, then fetch keys for each match.
    - Any other field mapping: unsupported, returns empty list.

    Rate limiting:
    - ``429 Too Many Requests`` or ``403`` with ``X-RateLimit-Remaining: 0``:
      log a warning and return an empty list.
    """

    def __init__(self, name: str, priority: int, ttl: int, config: dict[str, Any]) -> None:
        super().__init__(name, priority, ttl, config)

        self._api_base: str = config.get("api_base", _DEFAULT_API_BASE).rstrip("/")
        self._fields: dict[str, str] = dict(config.get("fields", {}))

        # Resolve token from environment
        token: str | None = None
        token_env = config.get("token_env")
        if token_env:
            token = os.environ.get(token_env)
            if not token:
                logger.warning(
                    "GitHub source %r: environment variable %r is not set. "
                    "Unauthenticated requests are limited to 60/hour.",
                    name, token_env,
                )

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"token {token}"

        self._client = httpx.AsyncClient(
            base_url=self._api_base,
            headers=headers,
            follow_redirects=True,
            timeout=30.0,
        )

        # Reverse mapping: GitHub field name → logical field name
        self._reverse_fields: dict[str, str] = {v: k for k, v in self._fields.items()}

    # ------------------------------------------------------------------
    # KeySource interface
    # ------------------------------------------------------------------

    def searchable_fields(self) -> list[FieldDefinition]:
        return [
            FieldDefinition(
                name=logical,
                source_attribute=gh_field,
                searchable=True,
                # GitHub fields should not participate in unqualified TEXT searches.
                # They should only be reached via resolvers or explicit field queries.
                text_searchable=False,
            )
            for logical, gh_field in self._fields.items()
        ]

    async def search(self, query: str, field: str = "github_username") -> SearchResult:
        """Search GitHub for GPG keys.

        Routes the search based on which GitHub field the logical *field* maps to.
        """
        gh_field = self._fields.get(field)
        if gh_field is None:
            logger.debug("GitHub source %r has no mapping for field %r", self.name, field)
            return SearchResult()

        if gh_field == "login":
            return SearchResult(keys=await self._fetch_keys_for_username(query))

        if gh_field == "email":
            return SearchResult(keys=await self._search_by_email(query))

        logger.debug(
            "GitHub source %r: unsupported field mapping %r → %r",
            self.name, field, gh_field,
        )
        return SearchResult()

    async def fetch_by_fingerprint(self, fingerprint: str) -> SourceKey | None:
        """Not supported — GitHub has no fingerprint-based key lookup API."""
        return None

    async def check_freshness(self, fingerprint: str, token: str) -> bool:
        """Check freshness via a conditional GET using the stored ETag.

        Returns ``True`` (fresh) if the server responds with ``304 Not Modified``.
        Returns ``False`` (stale) if the server responds with ``200 OK``.
        Returns ``True`` on error to avoid cascading failures.
        """
        if _FRESHNESS_SEP not in token:
            return False

        username, etag = token.split(_FRESHNESS_SEP, 1)
        if not username or not etag:
            return False

        if not self._validate_username(username):
            logger.debug(
                "GitHub source %r: invalid username %r in freshness token, assuming fresh",
                self.name, username,
            )
            return True  # assume fresh — don't make a request with a bad username

        try:
            resp = await self._client.get(
                f"/users/{username}/gpg_keys",
                headers={"If-None-Match": etag},
            )
        except Exception as exc:
            logger.warning("GitHub freshness check failed for source %r: %s", self.name, exc)
            return True  # assume fresh on network error

        if resp.status_code == 304:
            return True
        if resp.status_code == 200:
            return False
        # Any other status (rate limit, auth error, etc.) — assume fresh
        logger.warning(
            "GitHub freshness check for source %r returned unexpected status %d",
            self.name, resp.status_code,
        )
        return True

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_username(self, username: str) -> bool:
        """Return True if *username* matches GitHub's username rules.

        Validates against the pattern ``^[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?$``
        and enforces the 39-character maximum length.  This is a defense-in-depth
        guard; ``httpx`` already URL-encodes path segments, but we reject obviously
        invalid values before making any network request.
        """
        return bool(_GITHUB_USERNAME_RE.match(username)) and len(username) <= 39

    def _is_rate_limited(self, resp: httpx.Response) -> bool:
        """Return True if the response indicates a rate limit hit."""
        if resp.status_code == 429:
            return True
        if resp.status_code == 403:
            remaining = resp.headers.get("X-RateLimit-Remaining", "1")
            try:
                return int(remaining) == 0
            except ValueError:
                return False
        return False

    async def _fetch_keys_for_username(self, username: str) -> list[SourceKey]:
        """Fetch all GPG keys for a specific GitHub username."""
        if not self._validate_username(username):
            logger.debug(
                "GitHub source %r: rejecting invalid username %r", self.name, username
            )
            return []

        try:
            resp = await self._client.get(f"/users/{username}/gpg_keys")
        except Exception as exc:
            logger.warning("GitHub API request failed for source %r: %s", self.name, exc)
            return []

        if self._is_rate_limited(resp):
            retry_after = resp.headers.get("Retry-After", "unknown")
            logger.warning(
                "GitHub rate limit hit for source %r (retry after: %s)",
                self.name, retry_after,
            )
            return []

        if resp.status_code == 404:
            return []

        if resp.status_code != 200:
            logger.warning(
                "GitHub API returned %d for source %r user %r",
                resp.status_code, self.name, username,
            )
            return []

        etag = resp.headers.get("ETag", "")
        freshness_token = f"{username}{_FRESHNESS_SEP}{etag}"

        try:
            gpg_keys_data: list[dict[str, Any]] = resp.json()
        except Exception as exc:
            logger.warning("Failed to parse GitHub GPG keys response: %s", exc)
            return []

        return self._parse_gpg_keys(gpg_keys_data, username, freshness_token)

    async def _search_by_email(self, email: str) -> list[SourceKey]:
        """Search GitHub users by email, then fetch their GPG keys."""
        try:
            resp = await self._client.get(
                "/search/users",
                params={"q": f"{email} in:email"},
            )
        except Exception as exc:
            logger.warning("GitHub user search failed for source %r: %s", self.name, exc)
            return []

        if self._is_rate_limited(resp):
            logger.warning("GitHub rate limit hit during user search for source %r", self.name)
            return []

        if resp.status_code != 200:
            logger.warning(
                "GitHub user search returned %d for source %r",
                resp.status_code, self.name,
            )
            return []

        try:
            data = resp.json()
            users = data.get("items", [])
        except Exception as exc:
            logger.warning("Failed to parse GitHub user search response: %s", exc)
            return []

        all_keys: list[SourceKey] = []
        for user in users:
            username = user.get("login", "")
            if username:
                keys = await self._fetch_keys_for_username(username)
                all_keys.extend(keys)

        return all_keys

    def _parse_gpg_keys(
        self,
        gpg_keys_data: list[dict[str, Any]],
        username: str,
        freshness_token: str,
    ) -> list[SourceKey]:
        """Parse GitHub GPG key API response objects into SourceKey instances."""
        keys: list[SourceKey] = []

        for key_data in gpg_keys_data:
            raw_key = key_data.get("raw_key", "")
            if not raw_key:
                continue

            # Parse the key to get the fingerprint
            try:
                pgp_key, _ = pgpy.PGPKey.from_blob(raw_key)
                fingerprint = str(pgp_key.fingerprint).replace(" ", "").upper()
            except Exception as exc:
                logger.warning(
                    "Failed to parse PGP key for GitHub user %r: %s", username, exc
                )
                continue

            # Build metadata
            metadata: dict[str, str] = {}

            # Map GitHub fields to logical field names
            login_logical = self._reverse_fields.get("login")
            if login_logical:
                metadata[login_logical] = username

            # Collect email addresses from the key's emails list
            email_logical = self._reverse_fields.get("email")
            gh_emails: list[dict] = key_data.get("emails", [])
            if gh_emails and email_logical:
                # Use the first verified email if available
                verified = [e["email"] for e in gh_emails if e.get("verified")]
                all_emails = [e["email"] for e in gh_emails]
                primary = verified[0] if verified else (all_emails[0] if all_emails else "")
                if primary:
                    metadata[email_logical] = primary

            keys.append(SourceKey(
                fingerprint=fingerprint,
                key_armor=raw_key,
                metadata=metadata,
                freshness_token=freshness_token,
                source_name=self.name,
                source_priority=self.priority,
            ))

        return keys
