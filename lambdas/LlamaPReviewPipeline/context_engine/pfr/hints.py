"""Repository facts, owner guidance, and literal-grounded path hints."""

from __future__ import annotations

from typing import Dict, List

from ...repository_paths import is_dependency_manifest_path
from ..repo_structure import OWNER_DOC_PATHS
from .common import _truncate


def build_repo_fact_sheet(
    accessible_files: set[str],
    inventory=None,
) -> str:
    """Describe inventory facts without guessing meaning from path names."""

    files = sorted(accessible_files)
    manifests = [
        path for path in files if is_dependency_manifest_path(path)
    ][:12]
    top_dirs = sorted(
        {path.split("/", 1)[0] for path in files if "/" in path}
    )[:12]
    owner_docs = [path for path in files if path in OWNER_DOC_PATHS]
    return "\n".join(
        [
            f"- Files visible: {len(files)}",
            "- Inventory: "
            + (
                f"{inventory.status} "
                f"(recursive tree truncated={inventory.tree_truncated})"
                if inventory is not None
                else "unavailable; do not infer repository-wide absence"
            ),
            "- Top directories: "
            + (", ".join(top_dirs) if top_dirs else "none"),
            "- Manifests: "
            + (", ".join(manifests) if manifests else "none"),
            "- Owner docs: "
            + (", ".join(owner_docs) if owner_docs else "none"),
        ]
    )


def read_owner_docs(state) -> str:
    """Read bounded owner-authored review guidance at the exact PR head."""

    blocks: List[str] = []
    for path in OWNER_DOC_PATHS:
        inventory = state.repo_inventory
        direct_probe = (
            path not in state.accessible_files
            and inventory is not None
            and inventory.can_direct_probe(path)
        )
        if path not in state.accessible_files and not direct_probe:
            continue
        try:
            content = (
                state.runtime.get_file_content(
                    state.repo_full_name,
                    path,
                    sha=state.head_sha,
                )
                or ""
            )
        except Exception:
            if direct_probe:
                inventory.record_direct_probe(path, readable=False)
            continue
        if direct_probe:
            inventory.record_direct_probe(path, readable=bool(content))
            if content:
                state.accessible_files.add(path)
        elif inventory is not None:
            inventory.record_read(path, readable=bool(content))
        if content.strip():
            blocks.append(
                f"--- BEGIN OWNER DOC {path} ---\n"
                f"{_truncate(content.strip(), 4000)}\n"
                f"--- END OWNER DOC {path} ---"
            )
    return (
        "\n\n".join(blocks)
        if blocks
        else "No owner-authored review instructions found."
    )


def format_unique_suffix_path_hints(
    candidates: List[Dict[str, str]],
) -> str:
    """Render inventory-exact suffix matches as weak read candidates."""

    if not candidates:
        return "None detected."
    return "\n".join(
        f"- literal `{item.get('literal_reference')}` has one exact suffix "
        f"candidate `{item.get('candidate_path')}` in the complete inventory "
        "(weak candidate only; read it before using its contents)"
        for item in candidates[:6]
    )
