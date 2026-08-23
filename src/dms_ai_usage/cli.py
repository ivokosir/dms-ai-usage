from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .collector import UsageError, collect, load_config


def default_config_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "dms-ai-usage" / "config.json"


def default_cache_path() -> Path:
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / "dms-ai-usage" / "usage.json"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Collect Claude and Codex subscription usage.")
    result.add_argument("--config", type=Path, default=default_config_path())
    result.add_argument("--cache", type=Path, default=default_cache_path())
    result.add_argument("--pretty", action="store_true")
    result.add_argument("--version", action="version", version=__version__)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config.expanduser())
        payload = collect(config, args.cache.expanduser())
    except UsageError as exc:
        payload = {
            "schema": 1,
            "bar_text": "AI !",
            "accounts": [],
            "error": exc.code,
        }
        print(json.dumps(payload, separators=(",", ":")))
        return 1
    indent = 2 if args.pretty else None
    separators = None if args.pretty else (",", ":")
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=indent, separators=separators)
    sys.stdout.write("\n")
    return 0
