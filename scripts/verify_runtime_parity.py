#!/usr/bin/env python3
"""Compare the public runtime with the exact accepted production source object.

The comparison reads the baseline through ``git show``. It therefore ignores
the private repository's working tree and cannot accidentally include local or
uncommitted material. Every changed or removed runtime file must match one
explicitly recorded source-hash pair; all other files must be byte-identical.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BASELINE_COMMIT = "cd7edc5eb6a1f83b322c6314405fd72b57546114"
RUNTIME_PREFIXES = (
    "lambdas/LlamaPReviewPipeline",
    "lambdas/LlamaPReviewWebhookHandler",
)

# These hashes make the migration boundary executable. Updating a classified
# file requires reviewing and recording the new exact public-source hash; an
# arbitrary edit cannot disappear inside a broad path exemption.
EXPECTED_MODIFIED = {
    "lambdas/LlamaPReviewPipeline/config.py": {
        "classification": "generic_public_configuration",
        "baseline_sha256": "2f41c1ce9f22a8faee0cb9e1023cd90adc74d3e02bf22c9f98c4a42fa6356f52",
        "public_sha256": "2b8bdc970e83e7eba68ae21839b3e5af5692a674761c5796eb42ea37636a0a3b",
    },
    "lambdas/LlamaPReviewPipeline/persistence.py": {
        "classification": "retired_rollout_field_removal",
        "baseline_sha256": "c43be32c195618169c8bcbd7bb23d9e08545dc4e92fe328a154d7cbfde5c3baa",
        "public_sha256": "ae1f9c96ee1ec4400196f85947fbab0ce13735dfefacfe18791295086f65222f",
    },
    "lambdas/LlamaPReviewPipeline/pipeline_admission.py": {
        "classification": "retired_route_mode_removal",
        "baseline_sha256": "521a323583134d755676f126d7f6ca772a8b27f1a6629d66974eab4fcddd8a60",
        "public_sha256": "9818d6a7dea5bb87856544bef42bdbd2a16f122e82ec297869e8bb56988e9b5e",
    },
    "lambdas/LlamaPReviewPipeline/pipeline_accounting.py": {
        "classification": "public_source_wording_sanitization",
        "verification": "python_ast_without_docstrings_equal",
        "baseline_sha256": "5aff2bf9200353519ec94fb803461e6818b186f71c97727c333d1b7b77031a83",
        "public_sha256": "f1af5b4d557e13ac9fb54dbbc105c8ee42b76c34dcefcf8e53d3b3597503d3bd",
    },
    "lambdas/LlamaPReviewPipeline/provider_model_routing.py": {
        "classification": "public_source_wording_sanitization",
        "verification": "python_ast_without_docstrings_equal",
        "baseline_sha256": "1d64681574ce0e16dc95c4c517dcc5e91847512fd1d5265a5bf94c4b3e48c328",
        "public_sha256": "40b290f3792e698fb5599dd306a72adfe147739c77f97c6c3b87cd8429750bcd",
    },
    "lambdas/LlamaPReviewPipeline/review/publish.py": {
        "classification": "code_owned_open_source_footer",
        "baseline_sha256": "77be31b5110b2dcf80404df90da15db7570e8ca1e4e3a7e5e562983500186fcd",
        "public_sha256": "b8a3386d68f07276102e0fb4bacbff14f7eda5494dd887c0c39048d7774d2b0f",
    },
    "lambdas/LlamaPReviewWebhookHandler/lambda_function.py": {
        "classification": "hosted_private_event_zero_side_effect_boundary",
        "baseline_sha256": "e1d496b65002826843e26702e6ff9fa4cb7e116566b6456bd169a4862f583bb8",
        "public_sha256": "fc484c9c296cd5eac88cc9755380c0308a97ac137fb6232913cb201f11dfbddc",
    },
}

EXPECTED_REMOVED = {
    "lambdas/LlamaPReviewWebhookHandler/README.md": {
        "classification": "internal_runtime_notes_removal",
        "baseline_sha256": "86688ad25cbaae9a970695a17175526b5a97cba00917a8ff57321b7dda4cd907",
    },
    "lambdas/LlamaPReviewWebhookHandler/legacy_repo_insights.py": {
        "classification": "legacy_product_mutation_removal",
        "baseline_sha256": "483de011acf1cee19114a37e3a54b73c2dd717992fba90d22eb21f3fc1a63852",
    },
}


class ParityError(RuntimeError):
    """The requested baseline cannot be read as an exact Git object."""


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _python_ast_without_docstrings(content: bytes) -> str:
    tree = ast.parse(content.decode("utf-8"))
    for node in ast.walk(tree):
        if not isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            continue
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            del node.body[0]
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


def _git(baseline_repo: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(baseline_repo), *args],
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ParityError("unable to read the exact baseline Git object") from exc


def _baseline_sources(baseline_repo: Path) -> dict[str, bytes]:
    resolved = _git(
        baseline_repo,
        "rev-parse",
        f"{BASELINE_COMMIT}^{{commit}}",
    ).decode("ascii").strip()
    if resolved != BASELINE_COMMIT:
        raise ParityError("baseline commit did not resolve to the required object")

    listing = _git(
        baseline_repo,
        "ls-tree",
        "-r",
        "--name-only",
        BASELINE_COMMIT,
        "--",
        *RUNTIME_PREFIXES,
    ).decode("utf-8")
    paths = tuple(path for path in listing.splitlines() if path)
    return {
        path: _git(baseline_repo, "show", f"{BASELINE_COMMIT}:{path}")
        for path in paths
    }


def _public_sources(public_root: Path) -> dict[str, bytes]:
    sources: dict[str, bytes] = {}
    for prefix in RUNTIME_PREFIXES:
        directory = public_root / prefix
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if (
                not path.is_file()
                or "__pycache__" in path.parts
                or path.suffix == ".pyc"
            ):
                continue
            sources[path.relative_to(public_root).as_posix()] = path.read_bytes()
    return sources


def compare_sources(
    baseline: Mapping[str, bytes],
    public: Mapping[str, bytes],
    *,
    expected_modified: Mapping[str, Mapping[str, str]] = EXPECTED_MODIFIED,
    expected_removed: Mapping[str, Mapping[str, str]] = EXPECTED_REMOVED,
) -> dict[str, Any]:
    """Return a content-free report for exact and explicitly classified files."""

    identical: list[str] = []
    intentional: list[dict[str, str]] = []
    unexplained: list[dict[str, str]] = []

    for path in sorted(set(baseline) | set(public)):
        baseline_content = baseline.get(path)
        public_content = public.get(path)
        baseline_hash = _sha256(baseline_content) if baseline_content is not None else ""
        public_hash = _sha256(public_content) if public_content is not None else ""

        modified_rule = expected_modified.get(path)
        removed_rule = expected_removed.get(path)
        if modified_rule is not None:
            verification = modified_rule.get("verification", "exact_hash_pair")
            semantic_match = True
            if (
                verification == "python_ast_without_docstrings_equal"
                and baseline_content is not None
                and public_content is not None
            ):
                semantic_match = _python_ast_without_docstrings(
                    baseline_content
                ) == _python_ast_without_docstrings(public_content)
            if (
                baseline_content is not None
                and public_content is not None
                and baseline_hash == modified_rule["baseline_sha256"]
                and public_hash == modified_rule["public_sha256"]
                and semantic_match
            ):
                intentional.append(
                    {
                        "path": path,
                        "classification": modified_rule["classification"],
                        "change": "modified",
                        "verification": verification,
                        "baseline_sha256": baseline_hash,
                        "public_sha256": public_hash,
                    }
                )
            else:
                unexplained.append(
                    {
                        "path": path,
                        "reason": "classified modification did not match its exact hashes",
                        "baseline_sha256": baseline_hash,
                        "public_sha256": public_hash,
                    }
                )
            continue

        if removed_rule is not None:
            if (
                baseline_content is not None
                and public_content is None
                and baseline_hash == removed_rule["baseline_sha256"]
            ):
                intentional.append(
                    {
                        "path": path,
                        "classification": removed_rule["classification"],
                        "change": "removed",
                        "baseline_sha256": baseline_hash,
                        "public_sha256": "",
                    }
                )
            else:
                unexplained.append(
                    {
                        "path": path,
                        "reason": "classified removal did not match its exact baseline hash",
                        "baseline_sha256": baseline_hash,
                        "public_sha256": public_hash,
                    }
                )
            continue

        if baseline_content is not None and public_content == baseline_content:
            identical.append(path)
        else:
            unexplained.append(
                {
                    "path": path,
                    "reason": "unclassified runtime source difference",
                    "baseline_sha256": baseline_hash,
                    "public_sha256": public_hash,
                }
            )

    return {
        "schema": "llamapreview.runtime-parity/v1",
        "baseline_commit": BASELINE_COMMIT,
        "baseline_read_mode": "exact_git_object",
        "compared_file_count": len(set(baseline) | set(public)),
        "public_runtime_file_count": len(public),
        "identical_file_count": len(identical),
        "intentional_differences": intentional,
        "unexplained_differences": unexplained,
        "passed": not unexplained,
    }


def build_report(baseline_repo: Path, *, public_root: Path = ROOT) -> dict[str, Any]:
    return compare_sources(
        _baseline_sources(baseline_repo),
        _public_sources(public_root),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline-repo",
        type=Path,
        required=True,
        help="Local Git repository containing the accepted baseline commit",
    )
    args = parser.parse_args(argv)
    try:
        report = build_report(args.baseline_repo)
    except ParityError as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
