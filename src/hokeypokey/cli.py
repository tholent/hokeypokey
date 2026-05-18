"""Command-line interface for hokeypokey."""

from __future__ import annotations

import argparse
import logging
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
    parser.add_argument(
        "--env-file",
        metavar="PATH",
        type=Path,
        default=None,
        help="Path to a .env file to load (default: .env in the current directory, if it exists)",
    )
    parser.add_argument(
        "--log-level",
        metavar="LEVEL",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    return parser


def run(
    config_path: Path,
    host_override: str | None,
    port_override: int | None,
    env_file: Path | None = None,
    log_level: str = "INFO",
) -> None:
    """Load config, create app, and serve with Hypercorn."""
    import asyncio

    import hypercorn.asyncio
    import hypercorn.config
    from dotenv import load_dotenv

    from hokeypokey.app import create_app
    from hokeypokey.config import ConfigError, load_config

    # Configure logging before anything else
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log = logging.getLogger(__name__)

    # Load .env file — must happen before config loading, because source
    # configs reference environment variables (bind_password_env, token_env).
    if env_file is not None:
        if not env_file.exists():
            print(f"error: env file not found: {env_file}", file=sys.stderr)
            sys.exit(1)
        load_dotenv(env_file, override=True)
        log.info("Loaded environment from %s", env_file)
    else:
        # Try default .env in current directory (silent if missing)
        if load_dotenv():
            log.info("Loaded environment from .env")

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

    protocol = "hkps" if (config.server.tls_cert and config.server.tls_key) else "hkp"
    log.info(
        "Starting hokeypokey on %s://%s:%d with %d source(s)",
        protocol, config.server.host, config.server.port, len(config.sources),
    )

    asyncio.run(hypercorn.asyncio.serve(app, hc))


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    run(args.config, args.host, args.port, args.env_file, args.log_level)
