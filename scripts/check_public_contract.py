#!/usr/bin/env python3
"""Check public repository, documentation, SVG, and workflow trust boundaries."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

try:
    from lambda_manifest import ROOT_DIR, load_manifest
except ModuleNotFoundError:  # Imported as scripts.check_public_contract in tests.
    from scripts.lambda_manifest import ROOT_DIR, load_manifest


FORBIDDEN_PATH_PARTS = {
    ".aws_sync",
    ".terraform",
    "LlamaPReviewAdvancedHandler",
    "LlamaPReviewHandler",
    "qualification",
    "release-receipts",
}
FORBIDDEN_FILE_SUFFIXES = {
    ".auto.tfvars",
    ".pem",
    ".p12",
    ".pfx",
    ".tfplan",
    ".tfstate",
    ".whl",
    ".zip",
}
ACCOUNT_ID_RE = re.compile(r"(?<![A-Za-z0-9])(\d{12})(?![A-Za-z0-9])")
AWS_ARN_RE = re.compile(r"arn:aws(?:-[a-z]+)?:[^\s:'\"<>]+:[^\s:'\"<>]*:(\d{12}):")
SYNTHETIC_ACCOUNT_IDS = {"111111111111", "123456789012"}
LITERAL_S3_RE = re.compile(r"s3://(?![<{])[a-z0-9][a-z0-9.-]{2,62}(?:/|\b)")
ACTIVE_RETIRED_RE = re.compile(r"\b(?:SHADOW_REPOS|CANARY_REPOS|parallel_dryrun)\b")
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+['\"][^)]*['\"])?\)")
REFERENCE_LINK_RE = re.compile(r"^\s*\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
HTML_LINK_RE = re.compile(r"(?:href|src)=[\"']([^\"']+)[\"']", re.IGNORECASE)
ACTION_USE_RE = re.compile(r"^\s*-?\s*uses:\s*([^@\s]+)@([^\s#]+)", re.MULTILINE)
WRITE_PERMISSION_RE = re.compile(r"^\s+([a-z-]+):\s*write\s*$", re.MULTILINE)
ALLOWED_ACTIONS = {
    "actions/attest-build-provenance",
    "actions/checkout",
    "actions/dependency-review-action",
    "actions/setup-python",
    "github/codeql-action/analyze",
    "github/codeql-action/init",
    "hashicorp/setup-terraform",
}


def _public_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT_DIR,
        check=True,
        stdout=subprocess.PIPE,
    )
    return sorted(ROOT_DIR / value.decode("utf-8") for value in result.stdout.split(b"\0") if value)


def check_boundary() -> None:
    load_manifest()
    failures: list[str] = []
    for path in _public_files():
        relative = path.relative_to(ROOT_DIR)
        if FORBIDDEN_PATH_PARTS.intersection(relative.parts):
            failures.append(f"forbidden public path: {relative}")
        if any(path.name.endswith(suffix) for suffix in FORBIDDEN_FILE_SUFFIXES):
            failures.append(f"generated or sensitive file is tracked: {relative}")
        if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            failures.append(f"environment secret file is tracked: {relative}")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(match.group(1) not in SYNTHETIC_ACCOUNT_IDS for match in AWS_ARN_RE.finditer(text)):
            failures.append(f"literal AWS account ARN in {relative}")
        for match in ACCOUNT_ID_RE.finditer(text):
            if match.group(1) not in SYNTHETIC_ACCOUNT_IDS:
                failures.append(f"literal 12-digit account identity in {relative}")
                break
        if LITERAL_S3_RE.search(text):
            failures.append(f"literal S3 bucket URI in {relative}")
        if relative.parts and relative.parts[0] == "lambdas" and ACTIVE_RETIRED_RE.search(text):
            failures.append(f"retired routing mechanism in active runtime: {relative}")
    if failures:
        raise ValueError("Public boundary failed:\n- " + "\n- ".join(sorted(set(failures))))


def _local_link_target(document: Path, raw_target: str) -> Path | None:
    target = raw_target.strip("<>")
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or target.startswith(("#", "mailto:")):
        return None
    path_text = unquote(parsed.path)
    if not path_text:
        return None
    if path_text.startswith("/"):
        return ROOT_DIR / path_text.lstrip("/")
    return document.parent / path_text


def _check_svg(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise ValueError(f"SVG declarations are not allowed: {path}")
    root = ET.fromstring(text)
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise ValueError(f"Not an SVG root: {path}")
    view_box = root.attrib.get("viewBox", "").split()
    if len(view_box) != 4:
        raise ValueError(f"SVG requires a responsive viewBox: {path}")
    try:
        if float(view_box[2]) <= 0 or float(view_box[3]) <= 0:
            raise ValueError
    except ValueError as exc:
        raise ValueError(f"SVG viewBox dimensions must be positive: {path}") from exc
    children = {element.tag.rsplit("}", 1)[-1]: element for element in root}
    if not (children.get("title") is not None and "".join(children["title"].itertext()).strip()):
        raise ValueError(f"SVG requires a title: {path}")
    if not (children.get("desc") is not None and "".join(children["desc"].itertext()).strip()):
        raise ValueError(f"SVG requires a description: {path}")
    forbidden_tags = {"a", "embed", "foreignObject", "iframe", "image", "object", "script", "use"}
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in forbidden_tags:
            raise ValueError(f"Unsafe SVG element <{tag}>: {path}")
        for attribute, value in element.attrib.items():
            local_attribute = attribute.rsplit("}", 1)[-1].lower()
            lowered_value = value.strip().lower()
            if local_attribute.startswith("on"):
                raise ValueError(f"SVG event handler is forbidden: {path}")
            if local_attribute in {"href", "src"} and not lowered_value.startswith("#"):
                raise ValueError(f"SVG external resource is forbidden: {path}")
        if tag == "style":
            style = "".join(element.itertext()).lower()
            if "@import" in style or re.search(r"url\(\s*['\"]?(?:https?:|data:|//)", style):
                raise ValueError(f"SVG style has an external resource: {path}")


def check_docs() -> None:
    readme = ROOT_DIR / "README.md"
    if not readme.is_file():
        raise ValueError("README.md is required")
    markdown = sorted([readme, *(ROOT_DIR / "docs").rglob("*.md")])
    failures: list[str] = []
    svg_paths = sorted((ROOT_DIR / "docs").rglob("*.svg"))
    if not svg_paths:
        failures.append("at least one documentation SVG is required")
    for path in svg_paths:
        try:
            _check_svg(path)
        except (ET.ParseError, ValueError) as exc:
            failures.append(str(exc))
    for document in markdown:
        text = document.read_text(encoding="utf-8")
        targets = [
            *MARKDOWN_LINK_RE.findall(text),
            *REFERENCE_LINK_RE.findall(text),
            *HTML_LINK_RE.findall(text),
        ]
        for raw_target in targets:
            target = _local_link_target(document, raw_target)
            if target is not None and not target.exists():
                failures.append(
                    f"broken local link in {document.relative_to(ROOT_DIR)}: {raw_target}"
                )
    if failures:
        raise ValueError("Documentation contract failed:\n- " + "\n- ".join(sorted(set(failures))))


def check_workflows() -> None:
    workflow_dir = ROOT_DIR / ".github" / "workflows"
    expected = {"ci.yml", "codeql.yml", "release.yml"}
    actual = {path.name for path in workflow_dir.glob("*.y*ml")}
    if actual != expected:
        raise ValueError(
            f"Workflow set diverges; expected={sorted(expected)}, actual={sorted(actual)}"
        )
    failures: list[str] = []
    for path in sorted(workflow_dir.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        if "pull_request_target" in text:
            failures.append(f"pull_request_target is forbidden: {path.name}")
        if re.search(r"\b(?:aws-actions|aws\s+(?:lambda|s3|sts|iam)|terraform\s+apply)\b", lowered):
            failures.append(f"production/AWS mutation capability is forbidden: {path.name}")
        if "permissions: write-all" in lowered:
            failures.append(f"write-all permissions are forbidden: {path.name}")
        if "${{ secrets." in lowered:
            failures.append(f"repository or production secrets are forbidden: {path.name}")
        if any(
            token in lowered
            for token in (
                "getfunctionconfiguration",
                "lambda:updatefunction",
                "sts:assumerole",
                "s3:putobject",
            )
        ):
            failures.append(f"production permission is forbidden: {path.name}")
        for action, reference in ACTION_USE_RE.findall(text):
            if action.startswith("./"):
                continue
            if action not in ALLOWED_ACTIONS:
                failures.append(f"action is outside the reviewed allowlist: {path.name}: {action}")
            if not re.fullmatch(r"[0-9a-f]{40}", reference):
                failures.append(f"action is not commit-pinned: {path.name}: {action}")
        allowed_writes = {
            "ci.yml": set(),
            "codeql.yml": {"security-events"},
            "release.yml": {"attestations", "contents", "id-token"},
        }[path.name]
        unexpected_writes = set(WRITE_PERMISSION_RE.findall(text)) - allowed_writes
        if unexpected_writes:
            failures.append(
                f"workflow has unexpected write capability: {path.name}: "
                f"{', '.join(sorted(unexpected_writes))}"
            )
    if failures:
        raise ValueError("Workflow contract failed:\n- " + "\n- ".join(sorted(set(failures))))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "check", choices=("all", "boundary", "docs", "workflows"), nargs="?", default="all"
    )
    args = parser.parse_args()
    checks = {
        "boundary": check_boundary,
        "docs": check_docs,
        "workflows": check_workflows,
    }
    selected = checks.values() if args.check == "all" else (checks[args.check],)
    for check in selected:
        check()
    print(f"Public contract passed: {args.check}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
