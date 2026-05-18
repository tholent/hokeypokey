"""Declarative cross-source search resolvers."""

from __future__ import annotations

from hokeypokey.config import ResolverConfig
from hokeypokey.models import ResolvedQuery


class ConfigResolver:
    """A declarative resolver that bridges two key sources.

    When a search against *trigger_source* returns a result whose metadata
    contains *trigger_field*, this resolver produces a follow-up query
    against *target_source* using the field value as the search term for
    *target_field*.

    Example configuration::

        [[resolvers]]
        name = "ldap-to-github"
        trigger_source = "corporate-ldap"
        trigger_field = "github_id"
        target_source = "github-org"
        target_field = "github_username"

    This means: if an LDAP result has a ``github_id`` metadata value, use
    that value to search the GitHub source by ``github_username``.
    """

    def __init__(self, config: ResolverConfig) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def trigger_source(self) -> str:
        return self._config.trigger_source

    @property
    def trigger_field(self) -> str:
        return self._config.trigger_field

    @property
    def target_source(self) -> str:
        return self._config.target_source

    @property
    def target_field(self) -> str:
        return self._config.target_field

    def can_resolve(self, metadata: dict[str, str], source_name: str) -> bool:
        """Return ``True`` if this resolver should fire for the given result.

        Conditions (all must hold):
        - *source_name* matches :attr:`trigger_source`
        - :attr:`trigger_field` is present in *metadata*
        - The value of :attr:`trigger_field` in *metadata* is non-empty

        Args:
            metadata:    Metadata dict from a :class:`~hokeypokey.models.SourceKey`.
            source_name: Name of the source that produced the result.
        """
        if source_name != self._config.trigger_source:
            return False
        value = metadata.get(self._config.trigger_field, "").strip()
        return bool(value)

    def resolve(self, metadata: dict[str, str]) -> list[ResolvedQuery]:
        """Produce the follow-up queries triggered by *metadata*.

        Args:
            metadata: Metadata dict from a :class:`~hokeypokey.models.SourceKey`
                      that passed :meth:`can_resolve`.

        Returns:
            A list containing a single :class:`~hokeypokey.models.ResolvedQuery`
            directing the orchestrator to search *target_source* for the
            trigger field's value.
        """
        value = metadata[self._config.trigger_field]
        return [
            ResolvedQuery(
                target_source=self._config.target_source,
                search_field=self._config.target_field,
                search_value=value,
            )
        ]
