"""Configuration loading and validation for hokeypokey."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Raised when the configuration file is invalid or fails validation."""


# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(
    r"^(?:(?P<days>\d+)d)?(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?$"
)


def parse_duration(s: str) -> int:
    """Convert a human-readable duration string to seconds.

    Supported formats: ``"30s"``, ``"5m"``, ``"1h"``, ``"2h30m"``, ``"1h15m30s"``,
    ``"1d"``, ``"7d"``, ``"1d12h"``.

    Raises:
        ConfigError: if the string cannot be parsed or evaluates to zero.
    """
    s = s.strip()
    if not s:
        raise ConfigError("Empty duration string")

    m = _DURATION_RE.match(s)
    if not m or not any(m.group(g) for g in ("days", "hours", "minutes", "seconds")):
        raise ConfigError(
            f"Invalid duration {s!r}. Expected d/h/m/s, e.g. '5m', '1h30m', '7d', '1d12h'."
        )

    days = int(m.group("days") or 0)
    hours = int(m.group("hours") or 0)
    minutes = int(m.group("minutes") or 0)
    seconds = int(m.group("seconds") or 0)
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds

    if total <= 0:
        raise ConfigError(f"Duration {s!r} must be greater than zero.")

    return total


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 11371
    tls_cert: str | None = None
    tls_key: str | None = None


@dataclass
class CacheConfig:
    backend: Literal["memory"] = "memory"
    default_ttl: int = 600  # 10 minutes
    max_size: int | None = None  # None = unlimited; set to enable LRU eviction


@dataclass
class SourceConfig:
    name: str
    type: str
    priority: int
    ttl: int | None  # None means use CacheConfig.default_ttl
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResolverConfig:
    name: str
    trigger_source: str
    trigger_field: str
    target_source: str
    target_field: str


@dataclass
class AppConfig:
    server: ServerConfig
    cache: CacheConfig
    sources: list[SourceConfig]
    resolvers: list[ResolverConfig]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _parse_server(raw: dict[str, Any]) -> ServerConfig:
    cfg = ServerConfig()
    if "host" in raw:
        cfg.host = str(raw["host"])
    if "port" in raw:
        cfg.port = int(raw["port"])
    if "tls_cert" in raw and raw["tls_cert"]:
        cfg.tls_cert = str(raw["tls_cert"])
    if "tls_key" in raw and raw["tls_key"]:
        cfg.tls_key = str(raw["tls_key"])
    return cfg


def _parse_cache(raw: dict[str, Any]) -> CacheConfig:
    cfg = CacheConfig()
    if "backend" in raw:
        backend = str(raw["backend"])
        if backend != "memory":
            raise ConfigError(f"Unsupported cache backend {backend!r}. Only 'memory' is supported.")
        cfg.backend = backend  # type: ignore[assignment]
    if "default_ttl" in raw:
        cfg.default_ttl = parse_duration(str(raw["default_ttl"]))
    if "max_size" in raw:
        max_size = int(raw["max_size"])
        if max_size <= 0:
            raise ConfigError(f"cache.max_size must be a positive integer, got {max_size}.")
        cfg.max_size = max_size
    return cfg


def _parse_source(raw: dict[str, Any]) -> SourceConfig:
    for required in ("name", "type", "priority"):
        if required not in raw:
            raise ConfigError(f"Source entry missing required field {required!r}.")

    name = str(raw["name"])
    type_ = str(raw["type"])
    priority = int(raw["priority"])

    if priority <= 0:
        raise ConfigError(f"Source {name!r}: priority must be a positive integer, got {priority}.")

    ttl: int | None = None
    if "ttl" in raw:
        ttl = parse_duration(str(raw["ttl"]))

    config_dict: dict[str, Any] = dict(raw.get("config", {}))

    return SourceConfig(name=name, type=type_, priority=priority, ttl=ttl, config=config_dict)


def _parse_resolver(raw: dict[str, Any]) -> ResolverConfig:
    for required in ("name", "trigger_source", "trigger_field", "target_source", "target_field"):
        if required not in raw:
            raise ConfigError(f"Resolver entry missing required field {required!r}.")
    return ResolverConfig(
        name=str(raw["name"]),
        trigger_source=str(raw["trigger_source"]),
        trigger_field=str(raw["trigger_field"]),
        target_source=str(raw["target_source"]),
        target_field=str(raw["target_field"]),
    )


def _validate(config: AppConfig) -> None:
    """Cross-field validation after all sections are parsed."""
    source_names = [s.name for s in config.sources]

    # Unique source names
    seen: set[str] = set()
    for name in source_names:
        if name in seen:
            raise ConfigError(f"Duplicate source name {name!r}.")
        seen.add(name)

    # Resolver references must point to declared sources
    for r in config.resolvers:
        if r.trigger_source not in seen:
            raise ConfigError(
                f"Resolver {r.name!r}: trigger_source {r.trigger_source!r} "
                f"does not match any declared source."
            )
        if r.target_source not in seen:
            raise ConfigError(
                f"Resolver {r.name!r}: target_source {r.target_source!r} "
                f"does not match any declared source."
            )

    # Field names must be globally unique across all sources
    field_registry: dict[str, str] = {}  # field_name -> source_name
    for src in config.sources:
        fields: dict[str, Any] = src.config.get("fields", {})
        for field_name in fields:
            if field_name in field_registry:
                raise ConfigError(
                    f"Field name {field_name!r} is declared in both source "
                    f"{field_registry[field_name]!r} and {src.name!r}. "
                    f"Field names must be globally unique."
                )
            field_registry[field_name] = src.name


def load_config(path: Path) -> AppConfig:
    """Load and validate a hokeypokey TOML configuration file.

    Args:
        path: Path to the ``.toml`` file.

    Returns:
        A fully validated :class:`AppConfig`.

    Raises:
        FileNotFoundError: if ``path`` does not exist.
        ConfigError: if the file is malformed or fails validation.
    """
    with open(path, "rb") as fh:
        try:
            raw = tomllib.load(fh)
        except Exception as exc:
            raise ConfigError(f"Failed to parse TOML: {exc}") from exc

    server = _parse_server(raw.get("server", {}))
    cache = _parse_cache(raw.get("cache", {}))
    sources = [_parse_source(s) for s in raw.get("sources", [])]
    resolvers = [_parse_resolver(r) for r in raw.get("resolvers", [])]

    config = AppConfig(server=server, cache=cache, sources=sources, resolvers=resolvers)
    _validate(config)
    return config
