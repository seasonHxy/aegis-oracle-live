from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .publisher import DryRunPublisher, PublishError, SdkPublisher
from .service import OracleService
from .sources import SourceError, build_sources
from .state import JsonStateStore, StateError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hip3-oracle", description="HIP-3 multi-source oracle")
    parser.add_argument("--config", default="config/example.json", help="path to JSON configuration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="validate configuration without fetching or publishing")
    once = subparsers.add_parser("once", help="run one aggregation and publishing cycle")
    once.add_argument("--live", action="store_true", help="publish through the official Hyperliquid SDK")
    run = subparsers.add_parser("run", help="run continuously")
    run.add_argument("--live", action="store_true", help="publish through the official Hyperliquid SDK")
    return parser


def _make_service(config_path: str, *, live_flag: bool) -> OracleService:
    config = load_config(config_path)
    sources = build_sources(config.sources)
    if live_flag and config.dry_run:
        raise PublishError("live publishing requires both --live and dryRun=false in the config")
    if not live_flag and not config.dry_run:
        raise PublishError("config has dryRun=false; add --live to confirm publishing or restore dryRun=true")
    publisher = SdkPublisher.from_environment(config) if live_flag else DryRunPublisher()
    state = JsonStateStore(config.state_file)
    return OracleService(config, sources, publisher, state)


async def _once(config_path: str, live: bool) -> int:
    service = _make_service(config_path, live_flag=live)
    result = await service.cycle()
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True, default=str))
    return 0 if result.published else 2


async def _run(config_path: str, live: bool) -> int:
    service = _make_service(config_path, live_flag=live)
    await service.run_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            config = load_config(args.config)
            build_sources(config.sources)
            print(
                json.dumps(
                    {
                        "valid": True,
                        "config": str(Path(args.config).resolve()),
                        "network": config.network,
                        "dex": config.dex,
                        "feeds": [feed.coin for feed in config.feeds],
                        "dryRun": config.dry_run,
                    },
                    indent=2,
                )
            )
            return 0
        if args.command == "once":
            return asyncio.run(_once(args.config, args.live))
        if args.command == "run":
            return asyncio.run(_run(args.config, args.live))
        return 1
    except KeyboardInterrupt:
        return 130
    except (ConfigError, SourceError, StateError, PublishError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
