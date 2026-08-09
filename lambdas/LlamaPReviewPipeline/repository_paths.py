"""Single source of truth for high-intent repository file identities."""

from __future__ import annotations

from pathlib import PurePosixPath
import re
from typing import Literal, Optional


BoundedReadOptIn = Literal["dependency_lock", "ci_config"]

_DEPENDENCY_MANIFEST_BASENAMES = frozenset(
    {
        "package.json",
        "pyproject.toml",
        "pipfile",
        "setup.py",
        "setup.cfg",
        "go.mod",
        "cargo.toml",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "gemfile",
        "composer.json",
        "pubspec.yaml",
        "mix.exs",
        "project.clj",
        "deps.edn",
        "packages.config",
        "directory.packages.props",
        "package.swift",
    }
)

_DEPENDENCY_LOCK_BASENAMES = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "pnpm-lock.yml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "uv.lock",
        "poetry.lock",
        "pipfile.lock",
        "go.sum",
        "cargo.lock",
        "gradle.lockfile",
        "gemfile.lock",
        "composer.lock",
        "pubspec.lock",
        "mix.lock",
        "flake.lock",
        "renv.lock",
        "package.resolved",
        "packages.lock.json",
    }
)

_REQUIREMENTS_FAMILY_RE = re.compile(
    r"^requirements(?:[._-][a-z0-9][a-z0-9._-]*)?\.(?:txt|in)$",
    re.IGNORECASE,
)


def normalized_repo_path(path: object) -> str:
    return str(path or "").replace("\\", "/").strip().strip("/")


def repository_basename(path: object) -> str:
    normalized = normalized_repo_path(path)
    return PurePosixPath(normalized).name.lower() if normalized else ""


def is_dependency_lock_path(path: object) -> bool:
    """Recognize package-manager lock identities without matching source names."""

    basename = repository_basename(path)
    if not basename:
        return False
    return bool(
        basename in _DEPENDENCY_LOCK_BASENAMES
        or basename.endswith(".lock")
        or basename.endswith(".lockfile")
        or basename.endswith(".lock.json")
        or basename.endswith("-lock.json")
        or basename.endswith("_lock.json")
    )


def is_dependency_manifest_path(path: object) -> bool:
    basename = repository_basename(path)
    return bool(
        basename
        and (
            basename in _DEPENDENCY_MANIFEST_BASENAMES
            or _REQUIREMENTS_FAMILY_RE.fullmatch(basename)
            or is_dependency_lock_path(path)
        )
    )


def is_ci_config_path(path: object) -> bool:
    normalized = normalized_repo_path(path).lower()
    basename = repository_basename(normalized)
    if not normalized:
        return False
    return bool(
        normalized.startswith(".github/workflows/")
        or normalized in {".gitlab-ci.yml", ".gitlab-ci.yaml"}
        or normalized.startswith(".gitlab/ci/")
        or normalized.startswith(".circleci/")
        or normalized.startswith(".buildkite/")
        or basename in {
            "azure-pipelines.yml",
            "azure-pipelines.yaml",
            "jenkinsfile",
            "bitbucket-pipelines.yml",
            "bitbucket-pipelines.yaml",
            "appveyor.yml",
            "appveyor.yaml",
            ".travis.yml",
            ".travis.yaml",
        }
    )


def bounded_read_opt_in(path: object) -> Optional[BoundedReadOptIn]:
    if is_dependency_lock_path(path):
        return "dependency_lock"
    if is_ci_config_path(path):
        return "ci_config"
    return None
