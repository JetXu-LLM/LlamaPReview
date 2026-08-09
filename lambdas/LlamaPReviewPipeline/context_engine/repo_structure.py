"""Bounded repository inventory and tree rendering for context collection."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import posixpath
import re
from typing import Any, Dict, Iterable, List, Optional, Set


OWNER_DOC_PATHS = (
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "CONTRIBUTING",
    ".github/copilot-instructions.md",
)

_SAFE_ENV_EXAMPLE_RE = re.compile(r"(^|/)\.env(?:\.(?:example|sample|template))$", re.IGNORECASE)
_SENSITIVE_BASENAMES = {
    ".env",
    ".envrc",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "credentials.yml",
    "credentials.yaml",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
    "secrets.yml",
    "secrets.yaml",
}
_SENSITIVE_SUFFIXES = (".key", ".p12", ".pfx", ".pem")
_SENSITIVE_PARTS = {".git", ".aws", ".ssh"}


def normalize_repo_path(path: str) -> str:
    """Return a safe repo-relative POSIX path, or an empty string for invalid input."""
    raw = str(path or "").replace("\\", "/").strip().strip("/")
    if not raw:
        return ""
    normalized = posixpath.normpath(raw)
    if normalized in {".", ".."} or normalized.startswith("../"):
        return ""
    return normalized


def is_sensitive_repo_path(path: str) -> bool:
    """Exclude likely credentials while retaining committed dotfile configuration examples."""
    normalized = normalize_repo_path(path)
    if not normalized:
        return True
    if _SAFE_ENV_EXAMPLE_RE.search(normalized):
        return False
    parts = normalized.split("/")
    lowered_parts = [part.lower() for part in parts]
    basename = lowered_parts[-1]
    if any(part in _SENSITIVE_PARTS for part in lowered_parts):
        return True
    if basename in _SENSITIVE_BASENAMES or basename.startswith(".env."):
        return True
    if basename.endswith(_SENSITIVE_SUFFIXES):
        return True
    return False


@dataclass
class RepoInventory:
    """One-tree-request inventory used for validation, discovery, and audit metadata."""

    repository: str
    requested_sha: str
    status: str = "error"  # complete | partial | error
    tree_truncated: bool = False
    items: List[Dict[str, Any]] = field(default_factory=list)
    discoverable_files: Set[str] = field(default_factory=set)
    readable_files: Set[str] = field(default_factory=set)
    excluded_sensitive: Set[str] = field(default_factory=set)
    direct_probe_paths: Set[str] = field(default_factory=set)
    direct_probe_failures: Set[str] = field(default_factory=set)
    error: str = ""

    @property
    def owner_doc_paths(self) -> List[str]:
        return [path for path in OWNER_DOC_PATHS if path in self.discoverable_files]

    def can_direct_probe(self, path: str) -> bool:
        normalized = normalize_repo_path(path)
        return bool(
            normalized
            and self.status == "partial"
            and not is_sensitive_repo_path(normalized)
            and normalized not in self.direct_probe_paths
            and len(self.direct_probe_paths) < 6
        )

    def exact_path_state(self, path: str) -> str:
        """Return the inventory-only truth for one exact PR-head path.

        ``present`` is intentionally not a content observation. A complete tree
        can establish ``absent``; a truncated/error tree cannot, so it returns
        ``unknown`` rather than turning inventory incompleteness into absence.
        """

        normalized = normalize_repo_path(path)
        if not normalized or is_sensitive_repo_path(normalized):
            return "unknown"
        if normalized in self.discoverable_files:
            return "present"
        if any(
            item.get("type") == "tree" and item.get("path") == normalized
            for item in self.items
        ):
            # The tool contract asks about one exact file path. A known tree is
            # neither a present file nor evidence that the path is absent.
            return "directory"
        if self.status == "complete":
            return "absent"
        return "unknown"

    def file_size_bytes(self, path: str) -> Optional[int]:
        """Return a tree-advertised blob size without fetching its content."""

        normalized = normalize_repo_path(path)
        if not normalized:
            return None
        for item in self.items:
            if item.get("type") == "blob" and item.get("path") == normalized:
                size = item.get("size")
                return size if isinstance(size, int) and not isinstance(size, bool) else None
        return None

    def record_direct_probe(self, path: str, *, readable: bool) -> None:
        normalized = normalize_repo_path(path)
        if not normalized:
            return
        self.direct_probe_paths.add(normalized)
        if readable:
            self.discoverable_files.add(normalized)
            self.readable_files.add(normalized)
            self.direct_probe_failures.discard(normalized)
        else:
            self.direct_probe_failures.add(normalized)

    def record_read(self, path: str, *, readable: bool) -> None:
        normalized = normalize_repo_path(path)
        if not normalized:
            return
        if readable:
            self.readable_files.add(normalized)

    def render_tree(
        self,
        *,
        target_path: Optional[str] = None,
        max_depth: int = 3,
        show_files: bool = True,
        include_summary: bool = True,
        include_file_list: bool = False,
        file_extensions: Optional[List[str]] = None,
        exclude_hidden: bool = False,
    ) -> Dict[str, Any]:
        if self.status == "error":
            return {"error": self.error or "Repository inventory is unavailable."}
        return _render_inventory(
            self,
            target_path=target_path,
            max_depth=max_depth,
            show_files=show_files,
            include_summary=include_summary,
            include_file_list=include_file_list,
            file_extensions=file_extensions,
            exclude_hidden=exclude_hidden,
        )

    def to_meta(self) -> Dict[str, Any]:
        return {
            "repository": self.repository,
            "requested_sha": self.requested_sha,
            "status": self.status,
            "tree_truncated": self.tree_truncated,
            "known_blob_size_count": sum(
                1
                for item in self.items
                if item.get("type") == "blob" and isinstance(item.get("size"), int)
            ),
            "discoverable_file_count": len(self.discoverable_files),
            "readable_file_count": len(self.readable_files),
            "excluded_sensitive_count": len(self.excluded_sensitive),
            "excluded_sensitive_paths": sorted(self.excluded_sensitive)[:20],
            "owner_doc_paths": self.owner_doc_paths,
            "direct_probe_paths": sorted(self.direct_probe_paths),
            "direct_probe_failures": sorted(self.direct_probe_failures),
            "error": self.error,
        }


def _is_hidden(path: str) -> bool:
    return any(part.startswith(".") for part in path.split("/"))


def _iter_visible_items(
    inventory: RepoInventory,
    *,
    target_path: str,
    max_depth: int,
    file_extensions: Optional[List[str]],
    exclude_hidden: bool,
) -> Iterable[Dict[str, Any]]:
    prefix = f"{target_path}/" if target_path else ""
    for item in inventory.items:
        path = str(item.get("path") or "")
        if target_path and path != target_path and not path.startswith(prefix):
            continue
        rel_path = path[len(prefix) :] if prefix and path.startswith(prefix) else path
        if not rel_path or (exclude_hidden and _is_hidden(rel_path)):
            continue
        depth = rel_path.count("/") + 1
        if max_depth and depth > max_depth:
            continue
        if file_extensions and item.get("type") == "blob" and not any(rel_path.endswith(ext) for ext in file_extensions):
            continue
        yield {**item, "path": rel_path, "full_path": path}


def _tree_lines(node: Dict[str, Any], prefix_text: str = "") -> List[str]:
    visible = [(key, value) for key, value in sorted(node.items()) if not key.startswith("__")]
    lines: List[str] = []
    for index, (name, value) in enumerate(visible):
        last = index == len(visible) - 1
        connector = "`-- " if last else "|-- "
        if value.get("__type__") == "dir":
            lines.append(f"{prefix_text}{connector}{name}/")
            lines.extend(_tree_lines(value["__children__"], prefix_text + ("    " if last else "|   ")))
        else:
            size = int(value.get("__size__") or 0)
            lines.append(f"{prefix_text}{connector}{name}" + (f" ({size}B)" if size else ""))
    return lines


def _render_inventory(
    inventory: RepoInventory,
    *,
    target_path: Optional[str],
    max_depth: int,
    show_files: bool,
    include_summary: bool,
    include_file_list: bool,
    file_extensions: Optional[List[str]],
    exclude_hidden: bool,
) -> Dict[str, Any]:
    normalized_target = normalize_repo_path(target_path or "")
    tree: Dict[str, Any] = {}
    files: List[Dict[str, Any]] = []
    stats = {"total_files": 0, "total_dirs": 0, "total_size": 0, "file_types": defaultdict(int)}
    for item in _iter_visible_items(
        inventory,
        target_path=normalized_target,
        max_depth=max_depth,
        file_extensions=file_extensions,
        exclude_hidden=exclude_hidden,
    ):
        parts = str(item["path"]).split("/")
        current = tree
        for part in parts[:-1]:
            current = current.setdefault(part, {"__type__": "dir", "__children__": {}})["__children__"]
        final = parts[-1]
        if item.get("type") == "blob":
            if show_files:
                current[final] = {"__type__": "file", "__size__": int(item.get("size") or 0)}
            stats["total_files"] += 1
            stats["total_size"] += int(item.get("size") or 0)
            extension = "." + final.split(".")[-1] if "." in final else "[no extension]"
            stats["file_types"][extension] += 1
            if include_file_list:
                files.append({"path": item["full_path"], "size": int(item.get("size") or 0), "extension": extension})
        else:
            current.setdefault(final, {"__type__": "dir", "__children__": {}})
            stats["total_dirs"] += 1

    result: Dict[str, Any] = {
        "tree": "\n".join(_tree_lines(tree)) if tree else f"[Empty or no items match criteria in: {normalized_target or 'root'}]",
        "metadata": {
            "repository": inventory.repository,
            "sha": inventory.requested_sha,
            "target_path": normalized_target or "[root]",
            "max_depth": max_depth,
            "inventory_status": inventory.status,
            "tree_truncated": inventory.tree_truncated,
        },
        "_inventory": inventory,
    }
    if include_summary:
        result["summary"] = {
            "total_files": stats["total_files"],
            "total_directories": stats["total_dirs"],
            "total_size_bytes": stats["total_size"],
            "file_type_distribution": [
                {"extension": extension, "count": count}
                for extension, count in sorted(stats["file_types"].items())
            ],
        }
    if include_file_list:
        result["files"] = sorted(files, key=lambda item: item["path"])
    return result


def fetch_repo_inventory(repo_full_name: str, *, token: str, sha: str = "", deadline: Any = None) -> RepoInventory:
    import requests

    """Fetch exactly one recursive Git tree and preserve completeness/sensitivity truth."""
    inventory = RepoInventory(repository=repo_full_name, requested_sha=sha)
    try:
        owner, repo_name = repo_full_name.split("/", 1)
    except ValueError:
        inventory.error = "Repository name must use owner/repo format."
        return inventory
    tree_ref = sha or "HEAD"
    url = f"https://api.github.com/repos/{owner}/{repo_name}/git/trees/{tree_ref}?recursive=1"
    try:
        read_timeout = deadline.timeout_for(30, stage="context.repo_inventory") if deadline is not None else 30
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=(min(5, read_timeout), read_timeout),
        )
    except requests.RequestException as exc:
        inventory.error = f"Repository tree request failed: {type(exc).__name__}"
        return inventory
    if response.status_code != 200:
        inventory.error = f"Repository tree request returned HTTP {response.status_code}."
        return inventory
    try:
        payload = response.json()
    except ValueError:
        inventory.error = "Repository tree response was not valid JSON."
        return inventory
    raw_items = payload.get("tree") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        inventory.error = "Repository tree response did not contain a tree list."
        return inventory

    inventory.tree_truncated = bool(payload.get("truncated"))
    inventory.status = "partial" if inventory.tree_truncated else "complete"
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        path = normalize_repo_path(raw.get("path") or "")
        item_type = str(raw.get("type") or "")
        if not path or item_type not in {"blob", "tree"}:
            continue
        if is_sensitive_repo_path(path):
            if item_type == "blob":
                inventory.excluded_sensitive.add(path)
            continue
        item = {"path": path, "type": item_type, "size": int(raw.get("size") or 0)}
        inventory.items.append(item)
        if item_type == "blob":
            inventory.discoverable_files.add(path)
    return inventory


def get_repo_structure_for_llm(
    repo_full_name: str,
    *,
    token: str,
    sha: str = "",
    target_path: Optional[str] = None,
    max_depth: int = 3,
    show_files: bool = True,
    include_summary: bool = True,
    include_file_list: bool = False,
    file_extensions: Optional[List[str]] = None,
    exclude_hidden: bool = False,
    inventory: Optional[RepoInventory] = None,
    deadline: Any = None,
) -> Dict[str, Any]:
    """Render a tree from an existing inventory, fetching one tree only when absent."""
    repo_inventory = inventory or fetch_repo_inventory(repo_full_name, token=token, sha=sha, deadline=deadline)
    return repo_inventory.render_tree(
        target_path=target_path,
        max_depth=max_depth,
        show_files=show_files,
        include_summary=include_summary,
        include_file_list=include_file_list,
        file_extensions=file_extensions,
        exclude_hidden=exclude_hidden,
    )
