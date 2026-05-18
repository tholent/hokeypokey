"""Application factory for hokeypokey."""

from __future__ import annotations

import logging

from quart import Quart

from hokeypokey.cache import KeyCache
from hokeypokey.config import AppConfig
from hokeypokey.hkp.routes import hkp_bp
from hokeypokey.orchestrator import SearchOrchestrator
from hokeypokey.resolver import ConfigResolver
from hokeypokey.sources import get_source_class
from hokeypokey.sources.base import KeySource

logger = logging.getLogger(__name__)


def create_app(config: AppConfig) -> Quart:
    """Create and configure the Quart application.

    This is the application factory.  It wires together all components:
    cache, source plugins, resolvers, orchestrator, and HKP routes.

    Args:
        config: A fully validated :class:`~hokeypokey.config.AppConfig`.

    Returns:
        A configured :class:`quart.Quart` application ready to serve.
    """
    app = Quart(__name__)

    # ---- Cache ----
    cache = KeyCache(max_size=config.cache.max_size)

    # ---- Source plugins ----
    sources: dict[str, KeySource] = {}
    for src_cfg in config.sources:
        source_class = get_source_class(src_cfg.type)
        ttl = src_cfg.ttl if src_cfg.ttl is not None else config.cache.default_ttl
        source = source_class(
            name=src_cfg.name,
            priority=src_cfg.priority,
            ttl=ttl,
            config=src_cfg.config,
        )
        sources[src_cfg.name] = source
        logger.info(
            "Registered source %r (type=%s, priority=%d, ttl=%ds)",
            src_cfg.name,
            src_cfg.type,
            src_cfg.priority,
            ttl,
        )

    # ---- Resolvers ----
    resolvers = [ConfigResolver(r) for r in config.resolvers]
    for r in resolvers:
        logger.info(
            "Registered resolver %r (%s.%s → %s.%s)",
            r.name,
            r.trigger_source,
            r.trigger_field,
            r.target_source,
            r.target_field,
        )

    # ---- Orchestrator ----
    orchestrator = SearchOrchestrator(
        sources=sources,
        cache=cache,
        resolvers=resolvers,
    )

    # Store on app so routes can access it via current_app.extensions
    app.extensions["orchestrator"] = orchestrator

    # ---- Blueprint ----
    app.register_blueprint(hkp_bp)

    # ---- Shutdown hook ----
    @app.after_serving
    async def _close_sources() -> None:
        """Close all source connections on server shutdown."""
        for source in sources.values():
            try:
                await source.close()
                logger.info("Closed source %r", source.name)
            except Exception as exc:
                logger.warning("Error closing source %r: %s", source.name, exc)

    return app
