"""PR ingestion helpers ported from the advanced Lambda."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from copy import deepcopy
from itertools import islice
from typing import Any, Dict, List, Optional, Tuple

from . import config

logger = logging.getLogger(__name__)


_ACTIONABLE_CHECK_DETAIL_CONCLUSIONS = {
    "action_required",
    "failure",
    "startup_failure",
    "timed_out",
}
_MAX_DETAILED_CHECK_RUNS = 8
_MAX_ANNOTATIONS_PER_CHECK = 12
_MAX_ACTIONS_JOB_LOGS = 3
_MAX_ACTIONS_JOB_LOG_BYTES = 512 * 1024
_MAX_ACTIONS_JOB_LOG_TAIL_CHARS = 6_000
_ACTIONS_JOB_URL_RE = re.compile(
    r"^https://github\.com/[^/]+/[^/]+/actions/runs/\d+/job/(\d+)(?:[/?#].*)?$"
)
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
# Initial PR ingestion needs enough source to build useful 10-line custom
# diffs, but it must not cache every base/head file without a ceiling. Larger
# or later-budget files use GitHub's changed-file patch and remain eligible for
# targeted PFR reads. These are product memory bounds, not GitHub API limits.
PR_INGEST_SOURCE_FILE_MAX_BYTES = 128 * 1024
PR_INGEST_SOURCE_TOTAL_MAX_BYTES = 16 * 1024 * 1024


def _github_value(value: Any) -> Any:
    """Return a JSON-safe scalar from PyGithub, filtering its ``NotSet`` sentinel."""
    value_type = type(value)
    if (
        value_type.__module__ == "github.GithubObject"
        and value_type.__name__ == "_NotSetType"
    ):
        return None
    return value


def _github_field(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return _github_value(value.get(key))
    return _github_value(getattr(value, key, None))


def _github_count(value: Any) -> Optional[int]:
    normalized = _github_value(value)
    if isinstance(normalized, bool) or normalized in (None, ""):
        return None
    try:
        count = int(normalized)
    except (TypeError, ValueError):
        return None
    return max(0, count)


def _github_error_status(error: Exception) -> Optional[int]:
    status = _github_value(getattr(error, "status", None))
    if status is None:
        response = getattr(error, "response", None)
        status = _github_value(getattr(response, "status_code", None))
    return _github_count(status)


def _bounded_text(value: Any, limit: int) -> str:
    normalized = _github_value(value)
    text = str(normalized) if normalized not in (None, "") else ""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _http_get(*args: Any, **kwargs: Any) -> Any:
    """Load the Layer-owned HTTP client only when remote evidence is requested."""

    import requests

    return requests.get(*args, **kwargs)


def _actions_job_log_tail(
    repo_full_name: str,
    details_url: str,
    installation_token: str,
) -> tuple[str, str]:
    """Return one bounded GitHub Actions job-log tail when directly available."""

    match = _ACTIONS_JOB_URL_RE.fullmatch(str(details_url or "").strip())
    if not match or not installation_token:
        return "", "not_available"
    response = _http_get(
        (
            f"https://api.github.com/repos/{repo_full_name}/actions/jobs/"
            f"{match.group(1)}/logs"
        ),
        headers={
            "Authorization": f"Bearer {installation_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=20,
        allow_redirects=True,
        stream=True,
    )
    try:
        response.raise_for_status()
        raw_length = response.headers.get("content-length")
        try:
            content_length = int(raw_length) if raw_length else None
        except (TypeError, ValueError):
            content_length = None
        if content_length is not None and content_length > _MAX_ACTIONS_JOB_LOG_BYTES:
            return "", "too_large"
        body = response.raw.read(
            _MAX_ACTIONS_JOB_LOG_BYTES + 1,
            decode_content=True,
        )
        if len(body) > _MAX_ACTIONS_JOB_LOG_BYTES:
            return "", "too_large"
        rendered = body.decode("utf-8", errors="replace")
        rendered = _ANSI_ESCAPE_RE.sub("", rendered).replace("\x00", "")
        tail = rendered[-_MAX_ACTIONS_JOB_LOG_TAIL_CHARS :]
        if len(rendered) > _MAX_ACTIONS_JOB_LOG_TAIL_CHARS and "\n" in tail:
            tail = tail.split("\n", 1)[1]
        return tail.strip(), "ok" if tail.strip() else "empty"
    finally:
        response.close()


def _check_output_details(value: Any) -> Dict[str, Any]:
    details: Dict[str, Any] = {
        key: rendered
        for key, rendered in (
            ("title", _bounded_text(_github_field(value, "title"), 500)),
            ("summary", _bounded_text(_github_field(value, "summary"), 2_000)),
            ("text", _bounded_text(_github_field(value, "text"), 2_000)),
        )
        if rendered
    }
    annotations_count = _github_count(_github_field(value, "annotations_count"))
    if annotations_count is not None:
        details["annotations_count"] = annotations_count
    return details


def _check_annotation_details(annotation: Any) -> Dict[str, Any]:
    return {
        key: value
        for key, value in {
            "path": _bounded_text(_github_field(annotation, "path"), 500),
            "start_line": _github_count(
                _github_field(annotation, "start_line")
            ),
            "end_line": _github_count(_github_field(annotation, "end_line")),
            "annotation_level": _bounded_text(
                _github_field(annotation, "annotation_level"), 40
            ),
            "title": _bounded_text(_github_field(annotation, "title"), 500),
            "message": _bounded_text(_github_field(annotation, "message"), 2_000),
        }.items()
        if value not in (None, "")
    }


def _is_bot_author(author: Any) -> bool:
    return isinstance(author, str) and "[bot]" in author.lower()


_BOT_REVIEW_BLOCK_PATTERNS = (
    re.compile(r"(?is)#+\s*\[?AI Code Review by LlamaPReview\]?.*?(?=\n#{1,6}\s|\Z)"),
    re.compile(r"(?is)#+\s*Auto Pull Request Review.*?(?=\n#{1,6}\s|\Z)"),
    re.compile(r"(?is)<!--\s*(?:llamapreview|coderabbit|codeant|sourcery).*?-->.*?(?:<!--\s*/(?:llamapreview|coderabbit|codeant|sourcery)\s*-->|$)"),
)


def _contains_llamapreview_review(pr_content: Dict[str, Any]) -> bool:
    haystack = str(pr_content.get("interactions", "")).lower()
    return "llamapreview[bot]" in haystack or "auto pull request review" in haystack


def _extract_repo_description(runtime: Any, repo_full_name: str) -> str:
    try:
        repo = runtime.get_repository(repo_full_name)
    except Exception:
        logger.debug("Unable to fetch repository metadata for %s", repo_full_name, exc_info=True)
        return ""
    rendered = str(repo).strip()
    if rendered and repo_full_name in rendered and "object at 0x" not in rendered and not rendered.startswith("namespace("):
        return rendered
    for source in (repo, getattr(repo, "repo", None)):
        if source is None:
            continue
        description = getattr(source, "description", None)
        if description:
            return f"{repo_full_name} - {description}"
    return f"{repo_full_name} - None"


def _clean_bot_generated_text(text: Any) -> tuple[Any, int]:
    if not isinstance(text, str) or not text:
        return text, 0
    cleaned = text
    removed = 0
    for pattern in _BOT_REVIEW_BLOCK_PATTERNS:
        cleaned, count = pattern.subn("[removed bot-generated review block]", cleaned)
        removed += count
    return cleaned, removed


def _clean_metadata_bot_text(metadata: Dict[str, Any]) -> tuple[Dict[str, Any], int]:
    cleaned = dict(metadata)
    removed_total = 0
    for key in ("description", "body"):
        value, removed = _clean_bot_generated_text(cleaned.get(key))
        if removed:
            cleaned[key] = value
            removed_total += removed
    return cleaned, removed_total


def _metadata_with_repo_description(metadata: Dict[str, Any], repo_description: str) -> Dict[str, Any]:
    if not repo_description:
        return dict(metadata)
    updated: Dict[str, Any] = {}
    inserted = False
    for key, value in metadata.items():
        updated[key] = value
        if key == "number":
            updated["repo_description"] = repo_description
            inserted = True
    if not inserted:
        updated["repo_description"] = repo_description
    return updated


def sanitize_pr_content_for_review(raw_pr_content: Dict[str, Any], *, repo_description: str = "") -> Dict[str, Any]:
    """Return model-facing PR content while preserving raw duplicate-guard facts."""
    sanitized = deepcopy(raw_pr_content or {})
    interactions = sanitized.get("interactions")
    filtered_bot_count = 0
    if isinstance(interactions, list):
        human_interactions = []
        for item in interactions:
            author = item.get("author") if isinstance(item, dict) else None
            if _is_bot_author(author):
                filtered_bot_count += 1
                continue
            human_interactions.append(item)
        sanitized["interactions"] = human_interactions

    metadata = sanitized.get("pr_metadata")
    cleaned_bot_blocks = 0
    if isinstance(metadata, dict):
        metadata, cleaned_bot_blocks = _clean_metadata_bot_text(metadata)
        sanitized["pr_metadata"] = _metadata_with_repo_description(metadata, repo_description)
    elif repo_description:
        sanitized["pr_metadata"] = {"repo_description": repo_description}

    ingest_meta = dict(sanitized.get("_ingest_meta") or {})
    ingest_meta.update(
        {
            "raw_llamapreview_review_present": _contains_llamapreview_review(raw_pr_content or {}),
            "filtered_bot_interaction_count": filtered_bot_count,
            "cleaned_bot_generated_block_count": cleaned_bot_blocks,
        }
    )
    sanitized["_ingest_meta"] = ingest_meta
    return sanitized


def operational_ci_results(pr_content: Dict[str, Any]) -> Dict[str, Any]:
    """Return immutable CI facts, independent of model-input compaction.

    Large PR formatting is allowed to remove verbose CI text from the bounded
    model view. Routing and semantic guards must never read operational truth
    from that lossy view, so ``fetch_pr_details`` preserves the pre-trim object
    under a non-rendered namespace.
    """
    facts = (pr_content or {}).get("_operational_facts") or {}
    ci = facts.get("ci_cd_results") if isinstance(facts, dict) else None
    if isinstance(ci, dict):
        return ci
    visible = (pr_content or {}).get("ci_cd_results")
    return visible if isinstance(visible, dict) else {}


class GitHubRuntime:
    """Direct llama-github wiring without Mistral/HF initialization."""

    def __init__(self, installation_token: str):
        from llama_github.data_retrieval.github_api import GitHubAPIHandler
        from llama_github.data_retrieval.github_entities import RepositoryPool
        from llama_github.github_integration.github_auth_manager import GitHubAuthManager

        auth = GitHubAuthManager()
        self._installation_token = installation_token
        self.github = auth.authenticate_with_token(installation_token)
        # Each Lambda phase owns and closes this short-lived runtime. Starting
        # the library's long-lived cache-cleanup thread adds no value here and
        # needlessly creates one daemon thread per warm invocation.
        self.pool = RepositoryPool(self.github, cleanup_enabled=False)
        self.api = GitHubAPIHandler(self.github, pool=self.pool)

    def get_repository(self, repo_full_name: str):
        return self.pool.get_repository(repo_full_name, github_instance=self.github)

    def get_pr_content(self, repo_full_name: str, pr_number: int, *, context_lines: int = 10, force_update: bool = True) -> Dict[str, Any]:
        repo = self.get_repository(repo_full_name)
        # Resolve the native PullRequest exactly once and pass it through to the
        # SDK. Repository.get_pr_content otherwise performs this same get_pull
        # internally, so this exposes immutable base/head identity without
        # adding a GitHub request or racing a second provenance lookup.
        native_repo = getattr(repo, "repo", None)
        get_pull = getattr(native_repo, "get_pull", None)
        pull = get_pull(int(pr_number)) if callable(get_pull) else None
        kwargs: Dict[str, Any] = {
            "number": int(pr_number),
            "context_lines": context_lines,
            "force_update": force_update,
            "source_file_max_bytes": PR_INGEST_SOURCE_FILE_MAX_BYTES,
            "source_total_max_bytes": PR_INGEST_SOURCE_TOTAL_MAX_BYTES,
        }
        if pull is not None:
            kwargs["pr"] = pull
        result = repo.get_pr_content(
            **kwargs,
        )
        if not isinstance(result, dict):
            return result
        metadata = (
            dict(result.get("pr_metadata") or {})
            if isinstance(result.get("pr_metadata"), dict)
            else {}
        )
        if pull is not None:
            raw_head_sha = getattr(getattr(pull, "head", None), "sha", "")
            raw_base_sha = getattr(getattr(pull, "base", None), "sha", "")
            # PyGithub exposes both as strings. Refuse coercion here so a
            # malformed SDK object (or a permissive test double) cannot turn
            # object repr text into immutable provenance.
            head_sha = (
                raw_head_sha.strip()
                if isinstance(raw_head_sha, str)
                else ""
            )
            base_sha = (
                raw_base_sha.strip()
                if isinstance(raw_base_sha, str)
                else ""
            )
            if head_sha:
                metadata["head_sha"] = head_sha
            if base_sha:
                metadata["base_sha"] = base_sha
        if metadata:
            result["pr_metadata"] = metadata
        return result

    def get_file_content(self, repo_full_name: str, path: str, *, sha: Optional[str] = None) -> Optional[str]:
        return self.get_repository(repo_full_name).get_file_content(path, sha=sha)

    def read_text_file_bounded(
        self,
        repo_full_name: str,
        path: str,
        *,
        sha: Optional[str] = None,
        opt_in: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return bounded text plus content-free typed retrieval metadata."""

        repo = self.get_repository(repo_full_name)
        reader = getattr(repo, "read_text_file_bounded", None)
        if not callable(reader):
            raise RuntimeError(
                "Installed llama-github does not provide bounded text retrieval"
            )
        result = reader(path, sha=sha, opt_in=opt_in)
        serializer = getattr(result, "to_meta", None)
        meta = serializer() if callable(serializer) else {}
        if not isinstance(meta, dict):
            raise RuntimeError("Bounded text retrieval returned invalid metadata")
        return {**meta, "content": getattr(result, "content", None)}

    def get_pr_head_sha(self, repo_full_name: str, pr_number: int) -> str:
        return str(
            self.get_pr_head_snapshot(repo_full_name, pr_number).get("head_sha")
            or ""
        )

    def get_pr_head_snapshot(
        self, repo_full_name: str, pr_number: int
    ) -> Dict[str, Any]:
        """Return the current head and lifecycle from one fresh pull request read."""

        repo = self.get_repository(repo_full_name)
        pull = repo.repo.get_pull(int(pr_number))
        return {
            "head_sha": str(
                getattr(getattr(pull, "head", None), "sha", "") or ""
            ),
            "state": str(getattr(pull, "state", "") or "").strip().lower(),
            "merged": bool(
                getattr(pull, "merged", False)
                or getattr(pull, "merged_at", None)
            ),
        }

    def get_ci_results_for_head(
        self,
        repo_full_name: str,
        head_sha: str,
        *,
        include_actionable_details: bool = True,
    ) -> Dict[str, Any]:
        """Fetch bounded typed CI facts and actionable failure details for one head.

        llama-github intentionally exposes Commit Statuses and Check Runs as
        separate typed sources.  GitHub check output/annotations are fetched
        only for a small number of actionable failed runs, because those
        changed-line diagnostics often carry the highest-value maintainer
        action while successful/cancelled runs do not justify extra API calls.
        """
        repo = self.get_repository(repo_full_name)
        getter = getattr(repo, "get_ci_status_with_status", None)
        if not callable(getter):
            raise RuntimeError(
                "Installed llama-github does not provide current-head CI retrieval"
            )
        snapshot = getter(head_sha)
        serializer = getattr(snapshot, "to_dict", None)
        value = serializer() if callable(serializer) else snapshot
        if not isinstance(value, dict):
            raise RuntimeError("Current-head CI retrieval returned an invalid snapshot")
        checks = value.get("check_runs")
        if not isinstance(checks, list):
            checks = []
            value["check_runs"] = checks

        retrieval_meta = value.get("_retrieval_meta")
        if not isinstance(retrieval_meta, dict):
            retrieval_meta = {}
            value["_retrieval_meta"] = retrieval_meta
        detail_meta: Dict[str, Any] = {
            "outcome": "no_hit" if include_actionable_details else "not_requested",
            "attempted_check_count": 0,
            "enriched_check_count": 0,
            "unmatched_actionable_check_count": 0,
            "annotation_count": 0,
            "annotation_available_count": 0,
            "annotation_omitted_count": 0,
            "truncated_check_count": 0,
            "actions_log_attempted_count": 0,
            "actions_log_enriched_count": 0,
            "actions_log_omitted_count": 0,
            "error_count": 0,
            "errors": [],
        }
        if not include_actionable_details:
            retrieval_meta["ci_actionable_details"] = detail_meta
            return value
        try:
            native_repo = getattr(repo, "repo", None)
            if native_repo is None:
                raise RuntimeError("Native GitHub repository is unavailable")
            commit = native_repo.get_commit(sha=str(head_sha))
            native_runs = commit.get_check_runs()
            matched_indexes: set[int] = set()
            for run in islice(native_runs, 100):
                conclusion = _bounded_text(
                    _github_field(run, "conclusion"), 80
                ).strip().lower()
                if conclusion not in _ACTIONABLE_CHECK_DETAIL_CONCLUSIONS:
                    continue
                run_url = _bounded_text(_github_field(run, "html_url"), 2_000)
                run_name = _bounded_text(_github_field(run, "name"), 500)
                target_index = next(
                    (
                        index
                        for index, item in enumerate(checks)
                        if index not in matched_indexes
                        and isinstance(item, dict)
                        and run_url
                        and str(item.get("details_url") or "") == run_url
                    ),
                    None,
                )
                if target_index is None:
                    target_index = next(
                        (
                            index
                            for index, item in enumerate(checks)
                            if index not in matched_indexes
                            and isinstance(item, dict)
                            and str(item.get("name") or "") == run_name
                            and str(item.get("conclusion") or "").strip().lower()
                            == conclusion
                        ),
                        None,
                    )
                if target_index is None:
                    detail_meta["unmatched_actionable_check_count"] += 1
                    continue
                if detail_meta["attempted_check_count"] >= _MAX_DETAILED_CHECK_RUNS:
                    break
                detail_meta["attempted_check_count"] += 1
                matched_indexes.add(target_index)
                target = checks[target_index]
                try:
                    run_id = _github_count(_github_field(run, "id"))
                    if run_id is not None:
                        target["id"] = run_id
                    output = _check_output_details(_github_field(run, "output"))
                except Exception as exc:
                    detail_meta["error_count"] += 1
                    error_record = {
                        "stage": "check_output",
                        "check_identity": _bounded_text(
                            run_url or run_name, 500
                        ),
                        "error_type": type(exc).__name__,
                    }
                    error_status = _github_error_status(exc)
                    if error_status is not None:
                        error_record["status"] = error_status
                    detail_meta["errors"].append(error_record)
                    logger.warning(
                        "CI check output retrieval failed error_type=%s status=%s",
                        type(exc).__name__,
                        error_status,
                    )
                    continue
                has_output_content = any(
                    output.get(key) for key in ("title", "summary", "text")
                )
                annotations_available = _github_count(
                    output.get("annotations_count")
                )
                if annotations_available is not None:
                    detail_meta["annotation_available_count"] += (
                        annotations_available
                    )
                annotations: List[Dict[str, Any]] = []
                if annotations_available != 0:
                    try:
                        sampled_annotations = list(
                            islice(
                                run.get_annotations(),
                                _MAX_ANNOTATIONS_PER_CHECK + 1,
                            )
                        )
                        annotations = [
                            _check_annotation_details(annotation)
                            for annotation in sampled_annotations[
                                :_MAX_ANNOTATIONS_PER_CHECK
                            ]
                        ]
                        annotations = [item for item in annotations if item]
                        observed_available = max(
                            len(sampled_annotations),
                            annotations_available or 0,
                        )
                        if annotations_available is None:
                            detail_meta["annotation_available_count"] += (
                                observed_available
                            )
                        omitted = max(0, observed_available - len(annotations))
                        detail_meta["annotation_omitted_count"] += omitted
                        if omitted:
                            detail_meta["truncated_check_count"] += 1
                    except Exception as exc:
                        detail_meta["error_count"] += 1
                        error_record: Dict[str, Any] = {
                            "stage": "check_annotations",
                            "check_identity": _bounded_text(
                                run_url or run_id or run_name, 500
                            ),
                            "error_type": type(exc).__name__,
                        }
                        error_status = _github_error_status(exc)
                        if error_status is not None:
                            error_record["status"] = error_status
                        detail_meta["errors"].append(error_record)
                        logger.warning(
                            "CI check annotation retrieval failed error_type=%s status=%s",
                            type(exc).__name__,
                            error_status,
                        )
                actions_log_tail = ""
                actions_job = _ACTIONS_JOB_URL_RE.fullmatch(run_url)
                if (
                    actions_job
                    and not has_output_content
                    and detail_meta["actions_log_attempted_count"]
                    >= _MAX_ACTIONS_JOB_LOGS
                ):
                    detail_meta["actions_log_omitted_count"] += 1
                    detail_meta["truncated_check_count"] += 1
                elif actions_job and not has_output_content and getattr(
                    self,
                    "_installation_token",
                    "",
                ):
                    detail_meta["actions_log_attempted_count"] += 1
                    try:
                        actions_log_tail, log_outcome = _actions_job_log_tail(
                            repo_full_name,
                            run_url,
                            self._installation_token,
                        )
                        if log_outcome == "ok":
                            detail_meta["actions_log_enriched_count"] += 1
                        elif log_outcome == "too_large":
                            detail_meta["actions_log_omitted_count"] += 1
                            detail_meta["truncated_check_count"] += 1
                    except Exception as exc:
                        detail_meta["error_count"] += 1
                        error_record = {
                            "stage": "actions_job_log",
                            "check_identity": _bounded_text(
                                run_url or run_id or run_name,
                                500,
                            ),
                            "error_type": type(exc).__name__,
                        }
                        error_status = _github_error_status(exc)
                        if error_status is not None:
                            error_record["status"] = error_status
                        detail_meta["errors"].append(error_record)
                        logger.warning(
                            "CI Actions job-log retrieval failed error_type=%s status=%s",
                            type(exc).__name__,
                            error_status,
                        )
                if actions_log_tail:
                    output["log_tail"] = actions_log_tail
                    has_output_content = True
                if output:
                    target["output"] = output
                if annotations:
                    target["annotations"] = annotations
                    detail_meta["annotation_count"] += len(annotations)
                if has_output_content or annotations:
                    detail_meta["enriched_check_count"] += 1
        except Exception as exc:
            detail_meta["error_count"] += 1
            error_record = {
                "stage": "actionable_check_details",
                "error_type": type(exc).__name__,
            }
            error_status = _github_error_status(exc)
            if error_status is not None:
                error_record["status"] = error_status
            detail_meta["errors"].append(error_record)
            logger.warning(
                "CI actionable detail retrieval failed error_type=%s status=%s",
                type(exc).__name__,
                error_status,
            )

        if detail_meta["error_count"]:
            detail_meta["outcome"] = (
                "partial" if detail_meta["enriched_check_count"] else "error"
            )
        elif detail_meta["enriched_check_count"]:
            detail_meta["outcome"] = "ok"
        retrieval_meta["ci_actionable_details"] = detail_meta
        return value

    def close(self) -> None:
        close = getattr(self.pool, "close", None)
        if callable(close):
            close()
        close_github = getattr(self.github, "close", None)
        if callable(close_github):
            close_github()

    def search_code_with_status(self, query: str, repo_full_name: str) -> Dict[str, Any]:
        try:
            from github import GithubException
            from llama_github.config.config import config as llama_config
        except Exception:
            GithubException = Exception  # type: ignore[assignment]
            llama_config = None

        full_query = f"{query} repo:{repo_full_name}" if repo_full_name else query
        try:
            per_page = int(llama_config.get("code_search_max_hits") if llama_config is not None else 10)
            code_results = self.github.search_code(query=full_query, per_page=per_page)
            results = []
            for index, code_result in enumerate(code_results):
                if index >= per_page:
                    break
                try:
                    repository_obj, file_content = self.api._get_file_content_through_repository(code_result)
                    if repository_obj and file_content:
                        results.append(
                            {
                                "index": index,
                                "name": code_result["name"],
                                "path": code_result["path"],
                                "repository_full_name": code_result["repository"]["full_name"],
                                "url": code_result["html_url"],
                                "content": file_content,
                                "stargazers_count": repository_obj.stargazers_count,
                                "watchers_count": repository_obj.watchers_count,
                                "language": repository_obj.language,
                                "description": repository_obj.description,
                                "updated_at": repository_obj.updated_at,
                            }
                        )
                except Exception:
                    logger.debug("Unable to expand code search result for %s", full_query, exc_info=True)
            return {"results": sorted(results, key=lambda item: item["index"])}
        except GithubException as exc:  # type: ignore[misc]
            status = getattr(exc, "status", None)
            data = getattr(exc, "data", None)
            return {"results": [], "error": str(data or exc), "status": status}
        except Exception as exc:
            return {"results": [], "error": str(exc)}

    def search_code(self, query: str, repo_full_name: str) -> List[Dict[str, Any]]:
        status = self.search_code_with_status(query, repo_full_name)
        if status.get("error"):
            logger.warning("GitHub search failed for %s: %s", query, status.get("error"))
            return []
        return status.get("results") or []


def trim_pr_data(pr_data: dict, max_size: int = 50_000) -> dict:
    def clean_svg_paths(text: str) -> str:
        if not isinstance(text, str):
            return text
        pattern = r'(<path[^>]*?d=")[^"]*("(?:[^>]*?>|[^>]*\n[^>]*>))'
        return re.sub(pattern, lambda m: m.group(1) + "..." + m.group(2), text, flags=re.DOTALL)

    def map_values(data: Any, fn, key: str = "") -> Any:
        if isinstance(data, dict):
            return {k: map_values(v, fn, k) for k, v in data.items()}
        if isinstance(data, list):
            return [map_values(item, fn, key) for item in data]
        if isinstance(data, str):
            return fn(data, key)
        return data

    def clean_text(text: str, key: str = "") -> str:
        if key != "diff":
            text = re.sub(r"\(https?://[^)]+\)", "", text)
            text = re.sub(r'"https?://[^)]+"', '"..."', text)
        return re.sub(r"sha(?:\d+)?-[A-Za-z0-9+/=]+", "", text)

    trimmed = dict(pr_data)
    trimmed = map_values(trimmed, lambda text, _key: clean_svg_paths(text))
    if len(str(trimmed)) <= max_size:
        return trimmed
    for field in ("ci_cd_results", "commits", "related_issues"):
        if field in trimmed:
            trimmed[field] = []
        if len(str(trimmed)) <= max_size:
            return trimmed
    trimmed = map_values(trimmed, clean_text)
    if len(str(trimmed)) > max_size and isinstance(trimmed.get("interactions"), list):
        trimmed["interactions"] = [
            item
            for item in trimmed["interactions"]
            if not (isinstance(item, dict) and _is_bot_author(item.get("author")))
        ]
    return trimmed


def json_to_markdown(pr_data: Dict[str, Any]) -> str:
    def format_value(value: Any, indent: int = 0) -> str:
        if value is None:
            return "N/A"
        if isinstance(value, dict):
            return "\n".join(f"{'  ' * indent}- **{k}**: {format_value(v, indent + 1)}" for k, v in value.items())
        if isinstance(value, list):
            return "\n".join(f"{'  ' * indent}- {format_value(item, indent + 1)}" for item in value)
        return str(value).replace("\\n", "\n").replace("\\r", "")

    def format_file_change(file_change: Dict[str, Any]) -> str:
        md = f"### {file_change.get('file_path', 'Unknown file')}\n"
        md += f"**Change Type:** {file_change.get('change_type', 'Unknown')}\n"
        md += f"**Language:** {file_change.get('language', 'Unknown')}\n"
        md += f"**Additions:** {file_change.get('additions', 0)}\n"
        md += f"**Deletions:** {file_change.get('deletions', 0)}\n"
        md += f"**Changes:** {file_change.get('changes', 0)}\n"
        md += f"**Change Categories:** {', '.join(file_change.get('change_categories', []))}\n"
        diff = file_change.get("diff") or ""
        if diff:
            rendered_diff = diff.replace("\\n", "\n").replace("\\r", "")
            if rendered_diff.lstrip().startswith("[SKIPPED]"):
                coverage_note = rendered_diff.lstrip()[len("[SKIPPED]") :].strip()
                md += f"\n**Diff coverage:** unavailable ({coverage_note})\n\n"
            else:
                md += "\n```diff\n" + rendered_diff + "\n```\n\n"
        return md

    def format_issue(issue: Dict[str, Any]) -> str:
        md = f"### Issue #{issue.get('issue_number', issue.get('number', 'Unknown'))}\n"
        issue_content = issue.get("issue_content", issue.get("content", ""))
        if issue_content is not None:
            issue_text = str(issue_content)
            if issue_text.startswith("This is a Github Issue related to repo"):
                start_pos = issue_text.find("\n\n")
                if start_pos != -1:
                    issue_text = issue_text[start_pos + 2 :]
            md += issue_text.replace("\\n", "\n").replace("\\r", "")
        else:
            md += "No issue content available"
        return md + "\n\n"

    def format_commits(commits: List[Dict[str, Any]]) -> str:
        if not commits:
            return ""
        md = "## Commits\n"
        for commit in commits:
            md += f"### Commit [{str(commit.get('sha', ''))[:7]}]\n"
            md += f"**Author:** {commit.get('author', 'Unknown')}\n"
            md += f"**Date:** {commit.get('date', 'Unknown')}\n"
            stats = commit.get("stats", {}) if isinstance(commit.get("stats"), dict) else {}
            md += "**Changes:**\n"
            md += f"- Additions: {stats.get('additions', 0)}\n"
            md += f"- Deletions: {stats.get('deletions', 0)}\n"
            md += f"- Total: {stats.get('total', 0)}\n"
            md += "**Message:**\n```\n"
            md += str(commit.get("message", "")).strip()
            md += "\n```\n"
            files = commit.get("files", [])
            if files:
                md += "**Modified files:**\n"
                for file_item in files:
                    if isinstance(file_item, str):
                        md += f"- {file_item}\n"
                    elif isinstance(file_item, dict):
                        md += f"- {file_item.get('filename', 'Unknown file')}\n"
            md += "\n---\n\n"
        return md

    def format_content_section(items: List[Dict[str, Any]], label: str) -> str:
        md = f"## {label}\n"
        for item in items:
            md += f"### {item.get('file_path', 'Unknown file')}\n"
            md += "```\n"
            content = item.get("content")
            md += str(content).replace("\\n", "\n").replace("\\r", "") if content is not None else "No content available"
            md += "\n```\n\n"
        return md

    def format_ci_cd_results(ci_cd_results: Dict[str, Any]) -> str:
        md = "## CI/CD Results\n\n"
        commit_status_state = ci_cd_results.get("state", ci_cd_results.get("status"))
        md += (
            "**Commit Status State (Statuses only; Check Runs listed separately):** "
            f"{commit_status_state or 'not_reported'}\n\n"
        )
        statuses = ci_cd_results.get("statuses") or []
        if statuses:
            md += "### Statuses\n\n"
            for status in statuses:
                md += f"- **{status.get('context', status.get('name', 'Unknown status'))}**\n"
                md += f"  - State: {status.get('state', 'unknown')}\n"
                md += f"  - Description: {status.get('description', '')}\n"
                md += f"  - URL: {status.get('target_url', status.get('details_url', ''))}\n"
                md += f"  - Created: {status.get('created_at', '')}\n"
                md += f"  - Updated: {status.get('updated_at', '')}\n\n"
        check_runs = ci_cd_results.get("check_runs") or []
        if check_runs:
            md += "### Check Runs\n\n"
            for check_run in check_runs:
                md += f"- **{check_run.get('name', 'Unknown check')}**\n"
                md += f"  - Status: {check_run.get('status', 'unknown')}\n"
                md += f"  - Conclusion: {check_run.get('conclusion', '')}\n"
                md += f"  - Started: {check_run.get('started_at', '')}\n"
                md += f"  - Completed: {check_run.get('completed_at', '')}\n"
                md += f"  - Details URL: {check_run.get('details_url', '')}\n\n"
        return md

    metadata = pr_data.get("pr_metadata", {})
    number = metadata.get("number", "?")
    title = metadata.get("title", "Untitled")
    # The description has its own evidence section immediately below. Keeping
    # it inside the metadata map as well can duplicate tens of thousands of
    # characters in bot-authored dependency PRs and charges every downstream
    # model stage for the same evidence twice.
    metadata_summary = {
        key: value for key, value in metadata.items() if key != "description"
    }
    markdown = f"# Pull Request #{number}: {title}\n\n"
    markdown += "## Metadata\n" + format_value(metadata_summary) + "\n\n"
    markdown += "## Description\n" + (metadata.get("description") or "No description provided") + "\n\n"
    if pr_data.get("related_issues"):
        markdown += "## Related Issues\n"
        for issue in pr_data["related_issues"]:
            markdown += format_issue(issue) if isinstance(issue, dict) else str(issue) + "\n\n"
    if pr_data.get("commits"):
        markdown += format_commits(pr_data["commits"])
    markdown += "## File Changes\n"
    for file_change in pr_data.get("file_changes", []):
        markdown += format_file_change(file_change)
    for section, label in (("dependency_changes", "Dependency Changes"), ("config_changes", "Configuration Changes")):
        if pr_data.get(section):
            if all(isinstance(item, dict) and "content" in item for item in pr_data[section]):
                markdown += format_content_section(pr_data[section], label)
            else:
                markdown += f"## {label}\n" + format_value(pr_data[section]) + "\n\n"
    if pr_data.get("ci_cd_results"):
        try:
            markdown += format_ci_cd_results(pr_data["ci_cd_results"])
        except Exception as exc:
            logger.error("Error formatting CI/CD results: %s", exc)
    if pr_data.get("interactions"):
        markdown += "## Interactions\n" + format_value(pr_data["interactions"]) + "\n\n"
    return markdown


def _complete_line_excerpt(value: Any, limit: int) -> tuple[str, bool]:
    """Return a bounded prefix without manufacturing a partial source line."""

    text = str(value or "").replace("\\n", "\n").replace("\\r", "")
    if len(text) <= limit:
        return text, True
    if limit <= 0:
        return "", False
    prefix = text[:limit]
    if "\n" in prefix:
        prefix = prefix.rsplit("\n", 1)[0]
    else:
        prefix = ""
    return prefix.rstrip(), False


def _bounded_pr_markdown(
    pr_data: Dict[str, Any],
    *,
    max_chars: int,
    compact_above_chars: int,
) -> tuple[str, Dict[str, Any]]:
    """Pack PR intent and fair changed-region excerpts under one hard bound.

    The full sanitized ``pr_data`` remains available to Route, the exact-head
    changed-delta projection, and bounded PFR. This representation prevents a
    very large rendered Markdown string from becoming a terminal product
    outcome merely because formatting expanded already-bounded source data.
    """

    threshold = max(1, int(compact_above_chars))
    limit = min(max(1, int(max_chars)), threshold)
    full = json_to_markdown(pr_data)
    source_chars = len(full)
    changes = [
        item
        for item in pr_data.get("file_changes") or []
        if isinstance(item, dict)
    ]
    if source_chars <= threshold:
        return full, {
            "source_pr_details_chars": source_chars,
            "model_pr_details_chars": source_chars,
            "pr_details_compacted": False,
            "pr_details_source_file_count": len(changes),
            "pr_details_retained_file_count": len(changes),
        }

    metadata = (
        pr_data.get("pr_metadata")
        if isinstance(pr_data.get("pr_metadata"), dict)
        else {}
    )
    # This is an attention representation, not the retrieval boundary. Keep
    # source order and the same file-count ceiling as changed_delta_for_deep;
    # omitted files remain explicit and available to exact-head PFR.
    retained = changes[:80]
    description, description_complete = _complete_line_excerpt(
        metadata.get("description") or metadata.get("body") or "",
        min(12_000, max(1_000, limit // 10)),
    )
    title = _bounded_text(metadata.get("title") or "Untitled", 500)
    number = _bounded_text(metadata.get("number") or "?", 40)

    def file_shell(change: Mapping[str, Any]) -> str:
        return (
            f"### {_bounded_text(change.get('file_path') or 'Unknown file', 500)}\n"
            f"**Change Type:** {_bounded_text(change.get('change_type') or change.get('status') or 'Unknown', 40)}\n"
            f"**Additions:** {_github_count(change.get('additions')) or 0}\n"
            f"**Deletions:** {_github_count(change.get('deletions')) or 0}\n"
        )

    def render(per_diff_chars: int, selected: List[Dict[str, Any]]) -> str:
        omitted = len(changes) - len(selected)
        lines = [
            f"# Pull Request #{number}: {title}",
            "",
            "## Description",
            description or "No description provided",
        ]
        if not description_complete:
            lines.append(
                "[Description truncated by the bounded input projection.]"
            )
        lines.extend(
            [
                "",
                "## Bounded changed-region view",
                (
                    "This view preserves fair exact-line excerpts for routing "
                    "and planning. Partial or omitted regions remain explicit "
                    "and eligible for exact-head PFR retrieval."
                ),
                "",
                "## File Changes",
            ]
        )
        for change in selected:
            lines.extend([file_shell(change).rstrip(), ""])
            raw_diff = change.get("diff") or ""
            excerpt, complete = _complete_line_excerpt(raw_diff, per_diff_chars)
            if str(raw_diff).lstrip().startswith("[SKIPPED]"):
                coverage = "unavailable"
            else:
                coverage = "complete" if complete else "partial"
            lines.append(f"**Diff coverage:** {coverage}")
            if excerpt:
                lines.extend(["", "```diff", excerpt, "```"])
            lines.append("")
        if omitted:
            lines.append(
                f"{omitted} additional changed file(s) are omitted from this "
                "bounded view and remain available to exact-head retrieval."
            )
        return "\n".join(lines).rstrip() + "\n"

    # First make the path/coverage shell fit, preserving source order. This
    # branch is relevant only for extreme file counts or unusually long paths.
    packed = render(0, retained)
    while len(packed) > limit and retained:
        retained.pop()
        packed = render(0, retained)
    if retained:
        # Rendering adds fences and coverage labels around every excerpt, so
        # choose the largest fitting fair per-file allowance by exact bounded
        # search instead of estimating formatting overhead.
        low = 0
        high = min(6_000, limit)
        best = packed
        while low <= high:
            per_diff = (low + high) // 2
            candidate = render(per_diff, retained)
            if len(candidate) <= limit:
                best = candidate
                low = per_diff + 1
            else:
                high = per_diff - 1
        packed = best
    if len(packed) > limit:
        # A tiny configured limit may not fit even one file shell. Preserve a
        # truthful minimal carrier instead of cropping source or exceeding the
        # caller-owned bound.
        packed = (
            f"# Pull Request #{number}: {title}\n\n"
            "## Bounded changed-region view\n"
            f"This pull request changes {len(changes)} file(s). Detailed "
            "regions remain available to exact-head Route/PFR retrieval.\n"
        )[:limit]

    return packed, {
        "source_pr_details_chars": source_chars,
        "model_pr_details_chars": len(packed),
        "pr_details_compacted": True,
        "pr_details_source_file_count": len(changes),
        "pr_details_retained_file_count": len(retained),
    }


def fetch_pr_details(
    runtime: GitHubRuntime,
    repo_full_name: str,
    pr_number: int,
    *,
    max_chars: Optional[int] = None,
) -> Tuple[Dict[str, Any], str]:
    raw_pr_content = runtime.get_pr_content(repo_full_name, int(pr_number), context_lines=10, force_update=True)
    repo_description = _extract_repo_description(runtime, repo_full_name)
    sanitized = sanitize_pr_content_for_review(raw_pr_content or {}, repo_description=repo_description)
    retrieval_meta = (
        deepcopy(sanitized.get("_retrieval_meta"))
        if isinstance(sanitized.get("_retrieval_meta"), dict)
        else {}
    )
    operational_ci = (
        deepcopy(sanitized.get("ci_cd_results"))
        if isinstance(sanitized.get("ci_cd_results"), dict)
        else {}
    )
    operational_ci["_retrieval_meta"] = {
        key: value
        for key, value in retrieval_meta.items()
        if str(key).startswith("ci_")
    }
    operational_facts = {
        "ci_cd_results": operational_ci,
        "retrieval_meta": retrieval_meta,
    }
    trimmed = trim_pr_data(
        sanitized,
        max_size=(
            config.PR_DETAILS_MAX_CHARS
            if max_chars is None
            else int(max_chars)
        ),
    )
    trimmed["_operational_facts"] = operational_facts
    details, packing = _bounded_pr_markdown(
        trimmed,
        max_chars=(
            config.PR_DETAILS_MAX_CHARS
            if max_chars is None
            else int(max_chars)
        ),
        compact_above_chars=config.LARGE_PR_MAX_CHARS,
    )
    ingest_meta = dict(trimmed.get("_ingest_meta") or {})
    ingest_meta.update(packing)
    trimmed["_ingest_meta"] = ingest_meta
    return trimmed, details


def extract_pr_head_sha(pr_content: Dict[str, Any]) -> str:
    """Read the current head SHA from the typed PR metadata when available."""
    metadata = (pr_content or {}).get("pr_metadata") or {}
    for key in ("head_sha", "head_oid", "headRefOid"):
        value = metadata.get(key) if isinstance(metadata, dict) else None
        if value:
            return str(value)
    return ""


def has_existing_llamapreview_review(pr_content: Dict[str, Any]) -> bool:
    ingest_meta = pr_content.get("_ingest_meta") or {}
    if ingest_meta.get("raw_llamapreview_review_present"):
        return True
    return _contains_llamapreview_review(pr_content)
