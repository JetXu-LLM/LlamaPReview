"""Acquire exact-head source material needed for inline placement."""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..deadline import Deadline, DeadlineExceeded


__all__ = ["fetch_pr_files_and_contents"]


def fetch_pr_files_and_contents(
    runtime: Any,
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    *,
    target_paths: set[str],
    deadline: Optional[Deadline] = None,
):
    """Fetch only requested changed files and their exact-head contents."""

    repo_obj = runtime.get_repository(repo_full_name)
    requested = {
        str(path) for path in target_paths if str(path).strip()
    }
    metrics = {
        "requested_target_count": len(requested),
        "requested_targets": sorted(requested),
        "get_files_skipped": not requested,
        "deadline_skipped": False,
    }
    if not requested:
        return repo_obj, [], {}, {
            **metrics,
            "read_success_count": 0,
            "read_error_count": 0,
            "read_errors": [],
            "removed_path_skip_count": 0,
            "removed_paths": [],
            "unresolved_targets": [],
        }
    if deadline is not None:
        try:
            deadline.check(
                "review.placement.get_files",
                minimum_seconds=2.0,
            )
        except DeadlineExceeded:
            return repo_obj, [], {}, {
                **metrics,
                "deadline_skipped": True,
                "read_success_count": 0,
                "read_error_count": 0,
                "read_errors": [],
                "removed_path_skip_count": 0,
                "removed_paths": [],
                "unresolved_targets": sorted(requested),
            }
    pr = repo_obj.repo.get_pull(int(pr_number))
    files = []
    found_paths: set[str] = set()
    for file_obj in pr.get_files():
        path = str(getattr(file_obj, "filename", None) or "")
        if path in requested:
            files.append(file_obj)
            found_paths.add(path)
        if found_paths == requested:
            break
        if deadline is not None:
            try:
                deadline.check(
                    "review.placement.enumerate_files",
                    minimum_seconds=1.0,
                )
            except DeadlineExceeded:
                metrics["deadline_skipped"] = True
                break
    contents: Dict[str, str] = {}
    skipped_removed_paths = []
    read_errors = []
    for file_obj in files:
        path = getattr(file_obj, "filename", None)
        if not path:
            continue
        if (
            str(getattr(file_obj, "status", "") or "").lower()
            == "removed"
        ):
            skipped_removed_paths.append(path)
            continue
        if deadline is not None:
            try:
                deadline.check(
                    "review.placement.read_file",
                    minimum_seconds=1.0,
                )
            except DeadlineExceeded:
                metrics["deadline_skipped"] = True
                break
        try:
            content = runtime.get_file_content(
                repo_full_name,
                path,
                sha=head_sha,
            )
            if content is not None:
                contents[path] = str(content)
            else:
                read_errors.append(
                    {"path": path, "kind": "empty_content"}
                )
        except Exception as exc:
            read_errors.append(
                {"path": path, "kind": exc.__class__.__name__}
            )
    return repo_obj, files, contents, {
        **metrics,
        "read_success_count": len(contents),
        "read_error_count": len(read_errors),
        "read_errors": read_errors,
        "removed_path_skip_count": len(skipped_removed_paths),
        "removed_paths": skipped_removed_paths,
        "unresolved_targets": sorted(requested - found_paths),
    }
