"""Key source plugins for hokeypokey.

Each source plugin implements :class:`KeySource` and is responsible for
fetching GPG keys from a specific upstream system (LDAP, GitHub, etc.).
"""

from __future__ import annotations

from hokeypokey.sources.base import KeySource

__all__ = ["KeySource", "get_source_class"]

# Built once on first call; None until then. Lazy imports inside _build_registry
# prevent circular imports (sources → models/config → sources).
_REGISTRY: dict[str, type[KeySource]] | None = None


def _build_registry() -> dict[str, type[KeySource]]:
    from hokeypokey.sources.github import GitHubSource
    from hokeypokey.sources.ldap import LDAPSource

    return {"ldap": LDAPSource, "github": GitHubSource}


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
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_registry()

    from hokeypokey.config import ConfigError

    try:
        return _REGISTRY[type_name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY))
        raise ConfigError(f"Unknown source type {type_name!r}. Known types: {known}.") from None
