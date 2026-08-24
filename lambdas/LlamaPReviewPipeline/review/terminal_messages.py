"""Truth-safe public notice for deterministic skip outcomes."""

from __future__ import annotations


REVIEW_UNAVAILABLE_NOTICE = """### LlamaPReview — Review unavailable

LlamaPReview couldn't complete a reliable review of this pull request at this commit. No recommendation was made about whether it should be merged."""

def skipped_review_notice(reason: str) -> str:
    """Explain a policy skip without implying an engineering judgment."""

    return f"""### LlamaPReview — Review skipped

{reason}

No model-driven code review was run; this result does not assess whether the PR is ready to merge."""
