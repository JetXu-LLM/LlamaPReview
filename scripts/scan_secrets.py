#!/usr/bin/env python3
"""Fail closed on credential-shaped material without echoing matched values."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import zipfile

try:
    from lambda_manifest import ROOT_DIR
except ModuleNotFoundError:  # Imported as scripts.scan_secrets in tests.
    from scripts.lambda_manifest import ROOT_DIR


MAX_HISTORY_BLOB_BYTES = 25 * 1024 * 1024
PATTERNS = {
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "github-token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{40,255})\b"
    ),
    "provider-api-key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b"),
    "slack-token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{24,}\b"),
    "stripe-live-key": re.compile(r"\bsk_live_[A-Za-z0-9]{20,}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"),
    "private-key": re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
        r"[A-Za-z0-9+/=\r\n]{128,}"
        r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        re.MULTILINE,
    ),
}
KNOWN_HASH_BOUND_PUBLIC_EXAMPLES = {
    "pyjwt-2.13.0.dist-info/metadata": {"jwt"},
}


@dataclass(frozen=True)
class Finding:
    source: str
    line: int
    kind: str


def scan_text(text: str, source: str) -> list[Finding]:
    findings: list[Finding] = []
    for kind, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            findings.append(Finding(source, text.count("\n", 0, match.start()) + 1, kind))
    return findings


def scan_bytes(value: bytes, source: str) -> list[Finding]:
    findings = scan_text(value.decode("latin-1"), source)
    allowed_kinds = {
        kind
        for suffix, kinds in KNOWN_HASH_BOUND_PUBLIC_EXAMPLES.items()
        if source.lower().endswith(suffix)
        for kind in kinds
    }
    return [finding for finding in findings if finding.kind not in allowed_kinds]


def _public_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT_DIR,
        check=True,
        stdout=subprocess.PIPE,
    )
    return sorted(ROOT_DIR / value.decode("utf-8") for value in result.stdout.split(b"\0") if value)


def scan_path(path: Path, *, label: str | None = None) -> list[Finding]:
    source = label or path.as_posix()
    if path.suffix.lower() in {".zip", ".whl"}:
        findings: list[Finding] = []
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                findings.extend(scan_bytes(archive.read(info), f"{source}:{info.filename}"))
        return findings
    return scan_bytes(path.read_bytes(), source)


def scan_current() -> list[Finding]:
    return [finding for path in _public_files() for finding in scan_path(path)]


def scan_artifacts(directory: Path) -> list[Finding]:
    return [
        finding
        for path in sorted(item for item in directory.iterdir() if item.is_file())
        for finding in scan_path(path)
    ]


def scan_history() -> list[Finding]:
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=ROOT_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if head.returncode != 0:
        return []
    objects = subprocess.run(
        ["git", "rev-list", "--objects", "--all"],
        cwd=ROOT_DIR,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.splitlines()
    findings: list[Finding] = []
    for line in objects:
        object_id, _, path = line.partition(" ")
        kind = subprocess.run(
            ["git", "cat-file", "-t", object_id],
            cwd=ROOT_DIR,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        if kind != "blob":
            continue
        size = int(
            subprocess.run(
                ["git", "cat-file", "-s", object_id],
                cwd=ROOT_DIR,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout
        )
        if size > MAX_HISTORY_BLOB_BYTES:
            raise ValueError(f"History blob exceeds scan budget: {path or object_id[:12]}")
        value = subprocess.run(
            ["git", "cat-file", "blob", object_id],
            cwd=ROOT_DIR,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
        findings.extend(scan_bytes(value, f"history:{path or '<unnamed>'}@{object_id[:12]}"))
    return findings


def _report(findings: list[Finding]) -> None:
    for finding in sorted(set(findings), key=lambda item: (item.source, item.line, item.kind)):
        print(f"{finding.source}:{finding.line}: credential-shaped {finding.kind} (value redacted)")
    if findings:
        raise ValueError(f"Credential scan found {len(findings)} redacted match(es)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current", action="store_true")
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--artifacts", type=Path, action="append", default=[])
    args = parser.parse_args()
    if not any((args.current, args.history, args.artifacts)):
        parser.error("select --current, --history, and/or --artifacts")
    findings: list[Finding] = []
    if args.current:
        findings.extend(scan_current())
    if args.history:
        findings.extend(scan_history())
    for directory in args.artifacts:
        findings.extend(scan_artifacts(directory.resolve()))
    _report(findings)
    print("Credential scan passed (matched values are never printed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
