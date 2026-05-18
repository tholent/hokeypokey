"""Command-line interface for hokeypokey."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hokeypokey",
        description="A read-only HKP/HKPS keyserver that federates GPG keys from pluggable sources.",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        type=Path,
        default=Path("hokeypokey.toml"),
        help="Path to the TOML configuration file (default: hokeypokey.toml)",
    )
    parser.add_argument(
        "--host",
        metavar="HOST",
        default=None,
        help="Override the bind host from config",
    )
    parser.add_argument(
        "--port",
        metavar="PORT",
        type=int,
        default=None,
        help="Override the bind port from config",
    )
    return parser


def run(config_path: Path, host_override: str | None, port_override: int | None) -> None:
    """Load config, create app, and serve with Hypercorn."""
    import asyncio

    import hypercorn.asyncio
    import hypercorn.config

    from hokeypokey.app import create_app
    from hokeypokey.config import ConfigError, load_config

    try:
        config = load_config(config_path)
    except FileNotFoundError:
        print(f"error: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    if host_override is not None:
        config.server.host = host_override
    if port_override is not None:
        config.server.port = port_override

    app = create_app(config)

    hc = hypercorn.config.Config()
    hc.bind = [f"{config.server.host}:{config.server.port}"]
    if config.server.tls_cert and config.server.tls_key:
        hc.certfile = config.server.tls_cert
        hc.keyfile = config.server.tls_key

    asyncio.run(hypercorn.asyncio.serve(app, hc))


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    run(args.config, args.host, args.port)
