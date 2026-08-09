"""Truth-safe public notice for deterministic skip outcomes."""

from __future__ import annotations

def skipped_review_notice(reason: str) -> str:
    """Explain a policy skip without implying an engineering judgment."""

    return f"""### LlamaPReview — Review skipped

{reason}

No model-driven code review was run; this result does not assess whether the PR is ready to merge."""
