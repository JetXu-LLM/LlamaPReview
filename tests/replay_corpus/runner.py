"""Run the compact deterministic current and sealed replay suites."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence
import unittest


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "tests" / "replay_corpus" / "manifest.json"
SCHEMA = "llamapreview.replay/v1"
SUITES = ("current", "sealed")


class ReplayCorpusError(ValueError):
    pass


def _loaded_tests(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _loaded_tests(item)
        else:
            yield item


def validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA:
        raise ReplayCorpusError(f"manifest schema must be {SCHEMA}")
    seen: set[str] = set()
    for suite in SUITES:
        tests = value.get(suite)
        if not isinstance(tests, list) or not tests:
            raise ReplayCorpusError(f"{suite} must be a non-empty test list")
        for test_id in tests:
            if not isinstance(test_id, str) or not test_id.startswith("tests."):
                raise ReplayCorpusError(f"invalid {suite} test id")
            if test_id in seen:
                raise ReplayCorpusError(f"duplicate replay test: {test_id}")
            seen.add(test_id)
            loaded = unittest.defaultTestLoader.loadTestsFromName(test_id)
            resolved = list(_loaded_tests(loaded))
            if (
                len(resolved) != 1
                or type(resolved[0]).__name__ == "_FailedTest"
            ):
                raise ReplayCorpusError(
                    f"{test_id} resolves to {loaded.countTestCases()} tests"
                )
    return value


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    return validate_manifest(json.loads(path.read_text(encoding="utf-8")))


def _manifest_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _run(test_id: str) -> dict[str, Any]:
    suite = unittest.defaultTestLoader.loadTestsFromName(test_id)
    result = unittest.TestResult()
    suite.run(result)
    failures = result.failures + result.errors
    return {
        "test": test_id,
        "passed": result.wasSuccessful(),
        "diagnostic": failures[0][1][-2000:] if failures else "",
    }


def run_local_cases(
    manifest: Mapping[str, Any],
    *,
    suites: Sequence[str] = SUITES,
) -> dict[str, Any]:
    unknown = sorted(set(suites) - set(SUITES))
    if unknown:
        raise ReplayCorpusError(f"unknown suites: {', '.join(unknown)}")
    results = {
        suite: [_run(test_id) for test_id in manifest[suite]]
        for suite in suites
    }
    attempted = sum(len(items) for items in results.values())
    passed = sum(
        item["passed"] for items in results.values() for item in items
    )
    return {
        "schema_version": "llamapreview.replay-receipt/v1",
        "manifest_sha256": _manifest_sha256(manifest),
        "attempted": attempted,
        "passed": passed,
        "failed": attempted - passed,
        "suites": {
            suite: {
                "attempted": len(items),
                "passed": sum(item["passed"] for item in items),
                "failed": sum(not item["passed"] for item in items),
                "results": items,
            }
            for suite, items in results.items()
        },
    }
