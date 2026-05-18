"""Tests for the declarative cross-source search resolver."""

from __future__ import annotations

import pytest

from hokeypokey.config import ResolverConfig
from hokeypokey.models import ResolvedQuery
from hokeypokey.resolver import ConfigResolver


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def ldap_to_github() -> ConfigResolver:
    return ConfigResolver(
        ResolverConfig(
            name="ldap-to-github",
            trigger_source="ldap",
            trigger_field="github_id",
            target_source="github",
            target_field="github_username",
        )
    )


# ---------------------------------------------------------------------------
# can_resolve
# ---------------------------------------------------------------------------


def test_can_resolve_true(ldap_to_github):
    assert ldap_to_github.can_resolve({"github_id": "octocat"}, "ldap") is True


def test_can_resolve_wrong_source(ldap_to_github):
    assert ldap_to_github.can_resolve({"github_id": "octocat"}, "other-source") is False


def test_can_resolve_missing_trigger_field(ldap_to_github):
    assert ldap_to_github.can_resolve({"email": "x@y.com"}, "ldap") is False


def test_can_resolve_empty_trigger_value(ldap_to_github):
    assert ldap_to_github.can_resolve({"github_id": ""}, "ldap") is False


def test_can_resolve_whitespace_only_value(ldap_to_github):
    # Whitespace-only is falsy in Python — treated as empty
    assert ldap_to_github.can_resolve({"github_id": "   "}, "ldap") is False


def test_can_resolve_empty_metadata(ldap_to_github):
    assert ldap_to_github.can_resolve({}, "ldap") is False


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


def test_resolve_produces_correct_query(ldap_to_github):
    queries = ldap_to_github.resolve({"github_id": "octocat"})
    assert len(queries) == 1
    q = queries[0]
    assert isinstance(q, ResolvedQuery)
    assert q.target_source == "github"
    assert q.search_field == "github_username"
    assert q.search_value == "octocat"


def test_resolve_preserves_value_case(ldap_to_github):
    queries = ldap_to_github.resolve({"github_id": "OctoCat"})
    assert queries[0].search_value == "OctoCat"


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


def test_resolver_properties(ldap_to_github):
    assert ldap_to_github.name == "ldap-to-github"
    assert ldap_to_github.trigger_source == "ldap"
    assert ldap_to_github.trigger_field == "github_id"
    assert ldap_to_github.target_source == "github"
    assert ldap_to_github.target_field == "github_username"
