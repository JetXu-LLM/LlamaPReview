#!/usr/bin/env python3
"""Verify the minimal Pipeline Layer inside Linux x86_64 / Python 3.12."""

from __future__ import annotations

import argparse
import importlib
from importlib import metadata
import json
from pathlib import Path
import platform
import socket
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import zipfile


EXPECTED_PYTHON = (3, 12)
EXPECTED_MACHINES = {"x86_64", "amd64"}
MAX_UNZIPPED_BYTES = 50 * 1024 * 1024
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
EXPECTED_IMPORTS = (
    "certifi",
    "cffi",
    "charset_normalizer",
    "cryptography",
    "dateutil",
    "github",
    "idna",
    "jwt",
    "llama_github",
    "llama_github.data_retrieval.github_api",
    "llama_github.github_integration.github_auth_manager",
    "nacl",
    "requests",
    "urllib3",
)


def verify_search_failure_contract() -> None:
    """Cross real locked SDK HTTP handling into the product evidence ledger."""
    import requests

    from lambdas.LlamaPReviewPipeline.context_engine.state import CollectionState
    from lambdas.LlamaPReviewPipeline.context_engine.tools import ToolExecutor
    from lambdas.LlamaPReviewPipeline.pr_ingest import GitHubRuntime

    hit = {
        "path": "caller.py",
        "name": "caller.py",
        "repository": {"full_name": "owner/repo"},
        "html_url": "https://github.com/owner/repo/blob/main/caller.py",
    }
    repository = SimpleNamespace(
        stargazers_count=0,
        watchers_count=0,
        language="Python",
        description="offline",
        updated_at=None,
    )
    for status, hits, expansion, expected in (
        (503, [], (None, None), "search_error"),
        (200, [], (None, None), "no_hit"),
        (200, [hit], (None, None), "search_error"),
        (200, [hit], (repository, "def caller():\n    return entrypoint()\n"), "hit"),
    ):
        sends = []

        def respond(_session, request, **_kwargs):
            if not request.url.startswith("https://api.github.com/search/code?"):
                raise AssertionError("Unexpected SDK endpoint")
            sends.append(request.url)
            if len(sends) > 5:
                raise AssertionError("Unexpected unbounded SDK retry")
            response = requests.Response()
            response.status_code = status
            response.url = request.url
            response._content = json.dumps(
                {"items": hits, "total_count": len(hits), "incomplete_results": False}
            ).encode()
            if expected == "hit":
                response.headers["Link"] = '<https://api.github.com/search/code?page=2>; rel="next"'
            return response

        with (
            patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")),
            patch.object(requests.sessions.Session, "send", respond),
        ):
            runtime = GitHubRuntime("synthetic-offline-token")
            try:
                # Expansion absence is distinct from a valid bounded positive page.
                with patch.object(
                    runtime.api, "_get_file_content_through_repository", return_value=expansion
                ):
                    state = CollectionState(
                        pr_details="offline",
                        pr_content={"file_changes": []},
                        repo_full_name="owner/repo",
                        head_sha="a" * 40,
                        default_branch="main",
                        runtime=runtime,
                        accessible_files=set(),
                    )
                    result = ToolExecutor(state).execute(
                        {
                            "id": "typed-search",
                            "function": {
                                "name": "search_code",
                                "arguments": json.dumps(
                                    {"query": "entrypoint", "reason": "Find its caller."}
                                ),
                            },
                        }
                    )
                if not sends or (expected == "search_error" and "search_code error" not in result):
                    raise AssertionError(f"SDK search transport outcome lost: {status}")
                if state.tool_events[-1]["outcome"] != expected:
                    raise AssertionError("Search evidence ledger lost the transport outcome")
                if expected == "hit" and (
                    "partial search" not in result
                    or state.tool_events[-1]["metadata"]["retrieval_outcome"] != "partial"
                ):
                    raise AssertionError("Bounded positive search lost its partial status")
            finally:
                runtime.close()


def verify(layer_zip: Path) -> dict:
    if platform.system() != "Linux":
        raise RuntimeError("Runtime verification must run on Linux")
    if platform.machine().lower() not in EXPECTED_MACHINES:
        raise RuntimeError("Runtime verification must run on x86_64")
    if sys.version_info[:2] != EXPECTED_PYTHON:
        raise RuntimeError("Runtime verification must run on Python 3.12")

    with tempfile.TemporaryDirectory(prefix="llamapreview-layer-runtime-") as directory:
        root = Path(directory)
        with zipfile.ZipFile(layer_zip) as archive:
            if sum(info.file_size for info in archive.infolist()) > MAX_UNZIPPED_BYTES:
                raise ValueError("Layer exceeds the 50 MiB minimal-layer budget")
            for info in archive.infolist():
                path = Path(info.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or not path.parts
                    or path.parts[0] != "python"
                ):
                    raise ValueError(f"Unsafe or invalid Layer entry: {info.filename}")
                if info.date_time != ZIP_TIMESTAMP:
                    raise ValueError(f"Non-deterministic Layer timestamp: {info.filename}")
            archive.extractall(root)

        layer_path = root / "python"
        source_path = str(Path(__file__).resolve().parents[1])
        sys.path.insert(0, str(layer_path))
        sys.path.insert(1, source_path)
        try:
            for module_name in EXPECTED_IMPORTS:
                importlib.import_module(module_name)
            layer_manifest = json.loads(
                (layer_path / ".llamapreview-layer-manifest.json").read_text(encoding="utf-8")
            )
            distributions = {
                dist.metadata["Name"].lower(): dist.version
                for dist in metadata.distributions(path=[str(layer_path)])
                if dist.metadata.get("Name")
            }
            if distributions.get("llama-github") != "0.4.6":
                raise ValueError("Layer does not contain llama-github==0.4.6")
            verify_search_failure_contract()
            return {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "machine": platform.machine(),
                "layer_manifest": layer_manifest,
                "imports_verified": list(EXPECTED_IMPORTS),
                "search_failure_contract_verified": True,
            }
        finally:
            sys.path.remove(str(layer_path))
            sys.path.remove(source_path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("layer_zip", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.layer_zip), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
