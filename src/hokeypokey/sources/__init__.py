"""Key source plugins for hokeypokey.

Each source plugin implements :class:`KeySource` and is responsible for
fetching GPG keys from a specific upstream system (LDAP, GitHub, etc.).
"""

from __future__ import annotations

from hokeypokey.sources.base import KeySource

__all__ = ["KeySource", "get_source_class"]


def get_source_class(type_name: str) -> type[KeySource]:
    """Return the :class:`KeySource` subclass registered for *type_name*.

    This uses a simple explicit registry rather than dynamic plugin discovery,
    keeping the dependency graph clear and startup fast.

    Args:
        type_name: The ``type`` value from a ``[[sources]]`` config block
                   (e.g. ``"ldap"``, ``"github"``).

    Returns:
        The corresponding :class:`KeySource` subclass.

    Raises:
        :class:`~hokeypokey.config.ConfigError`: if *type_name* is not registered.
    """
    # Import lazily to avoid circular imports and to keep startup fast when
    # only a subset of source types are actually used.
    from hokeypokey.config import ConfigError
    from hokeypokey.sources.github import GitHubSource
    from hokeypokey.sources.ldap import LDAPSource

    _REGISTRY: dict[str, type[KeySource]] = {
        "ldap": LDAPSource,
        "github": GitHubSource,
    }

    try:
        return _REGISTRY[type_name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY))
        raise ConfigError(
            f"Unknown source type {type_name!r}. Known types: {known}."
        ) from None
