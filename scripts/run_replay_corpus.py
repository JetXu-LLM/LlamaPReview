#!/usr/bin/env python3
"""Run compact current and sealed deterministic replay suites."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.replay_corpus.runner import (  # noqa: E402
    DEFAULT_MANIFEST,
    ReplayCorpusError,
    load_manifest,
    run_local_cases,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--suite",
        choices=("all", "current", "sealed"),
        default="all",
    )
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        suites = (
            ("current", "sealed")
            if args.suite == "all"
            else (args.suite,)
        )
        receipt = run_local_cases(manifest, suites=suites)
    except (OSError, json.JSONDecodeError, ReplayCorpusError) as exc:
        parser.error(str(exc))
    print(json.dumps(receipt, indent=2, ensure_ascii=False, sort_keys=True))
    return 1 if receipt["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
