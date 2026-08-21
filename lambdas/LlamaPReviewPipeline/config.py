"""Centralized configuration for the LlamaPReview 2026 pipeline."""

from __future__ import annotations

import os
from typing import Iterable, List


def env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None else value


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be an explicit boolean, got {raw!r}")


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def env_list(name: str, default: Iterable[str] | None = None) -> List[str]:
    raw = os.environ.get(name)
    if raw is None:
        return list(default or [])
    return [part.strip() for part in raw.split(",") if part.strip()]


DYNAMODB_PIPELINE_TABLE = env("DYNAMODB_PIPELINE_TABLE")
CONTEXT_S3_BUCKET = env("CONTEXT_S3_BUCKET", "")
RUN_ARTIFACT_BUCKET = env("RUN_ARTIFACT_BUCKET", CONTEXT_S3_BUCKET)
PUBLICATION_ARTIFACT_BUCKET = env(
    "PUBLICATION_ARTIFACT_BUCKET",
    RUN_ARTIFACT_BUCKET,
)
RUN_ARTIFACT_PREFIX = env("RUN_ARTIFACT_PREFIX", "pipeline")
RUN_ARTIFACT_SCHEMA_VERSION = env("RUN_ARTIFACT_SCHEMA_VERSION", "1")

GITHUB_APP_ID = env("GITHUB_APP_ID")
GITHUB_PRIVATE_KEY = env("GITHUB_PRIVATE_KEY")
GITHUB_WEBHOOK_SECRET = env("GITHUB_WEBHOOK_SECRET")

DEEPSEEK_API_KEY = env("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL = env("DEEPSEEK_MODEL", "deepseek-v4-pro")
# The billed transport model may differ from the logical review tier.  Keep
# both identities in provider accounting; an explicit empty value restores
# direct logical-model dispatch.
DEEPSEEK_TRANSPORT_MODEL_OVERRIDE = env(
    "DEEPSEEK_TRANSPORT_MODEL_OVERRIDE",
    "deepseek-v4-flash",
)
DEEPSEEK_EFFORT = env("DEEPSEEK_REASONING_EFFORT", "max")
DEEPSEEK_TRACE_MODE = env("DEEPSEEK_TRACE_MODE", "summary")
DEEPSEEK_TRACE_DIR = env("DEEPSEEK_TRACE_DIR", "")
DEEPSEEK_TRACE_S3_BUCKET = env("DEEPSEEK_TRACE_S3_BUCKET", RUN_ARTIFACT_BUCKET)
DEEPSEEK_TRACE_CHUNK_CHARS = env_int("DEEPSEEK_TRACE_CHUNK_CHARS", 45_000)

PFR_MODEL = DEEPSEEK_MODEL
PFR_EFFORT = DEEPSEEK_EFFORT
REVIEW_MODEL = DEEPSEEK_MODEL
REVIEW_EFFORT = DEEPSEEK_EFFORT
ANALYZER_MODEL = env("ANALYZER_MODEL", "deepseek-v4-flash")
ANALYZER_EFFORT = env("ANALYZER_EFFORT", "high")
LOW_REVIEW_MODEL = env("LOW_REVIEW_MODEL", "deepseek-v4-flash")
LOW_REVIEW_EFFORT = env("LOW_REVIEW_EFFORT", "high")
PFR_NORMAL_MODEL = env("PFR_NORMAL_MODEL", "deepseek-v4-flash")
PFR_NORMAL_EFFORT = env("PFR_NORMAL_EFFORT", "high")
NORMAL_REVIEW_MODEL = env("NORMAL_REVIEW_MODEL", "deepseek-v4-pro")
NORMAL_REVIEW_EFFORT = env("NORMAL_REVIEW_EFFORT", "high")

DEEPSEEK_TIMEOUT_SECONDS = env_int("DEEPSEEK_TIMEOUT_SECONDS", 460)
PFR_HIGH_TIME_BUDGET_SECONDS = env_int(
    "PFR_HIGH_TIME_BUDGET_SECONDS",
    780,
)
PIPELINE_STATE_WRITE_RESERVE_SECONDS = env_int(
    "PIPELINE_STATE_WRITE_RESERVE_SECONDS", 30
)
PIPELINE_CONTEXT_PHASE_MAX_SECONDS = env_int(
    "PIPELINE_CONTEXT_PHASE_MAX_SECONDS", 780
)
PIPELINE_REVIEW_PHASE_MAX_SECONDS = env_int(
    "PIPELINE_REVIEW_PHASE_MAX_SECONDS", 780
)
PFR_HIGH_SOFT_TIME_BUDGET_SECONDS = env_int("PFR_HIGH_SOFT_TIME_BUDGET_SECONDS", 420)
PFR_NORMAL_SOFT_TIME_BUDGET_SECONDS = env_int("PFR_NORMAL_SOFT_TIME_BUDGET_SECONDS", 180)
PFR_HIGH_MAX_TOOL_ROUNDS = env_int("PFR_HIGH_MAX_TOOL_ROUNDS", 8)
PFR_HIGH_TOKEN_BUDGET = env_int("PFR_HIGH_TOKEN_BUDGET", 750_000)
MAX_FILE_SIZE = env_int("MAX_FILE_SIZE", 50_000)
PR_DETAILS_MAX_CHARS = env_int("PR_DETAILS_MAX_CHARS", 250_000)
LARGE_PR_MAX_CHARS = env_int("LARGE_PR_MAX_CHARS", 600_000)
PFR_HIGH_MAX_CONTEXT_CHARS = env_int(
    "PFR_HIGH_MAX_CONTEXT_CHARS",
    600_000,
)
REVIEW_INPUT_MAX_CHARS = env_int("REVIEW_INPUT_MAX_CHARS", 850_000)
PFR_NORMAL_TIME_BUDGET_SECONDS = env_int(
    "PFR_NORMAL_TIME_BUDGET_SECONDS",
    240,
)
PFR_NORMAL_MAX_TOOL_ROUNDS = env_int("PFR_NORMAL_MAX_TOOL_ROUNDS", 3)
PFR_NORMAL_TOKEN_BUDGET = env_int("PFR_NORMAL_TOKEN_BUDGET", 200_000)
PFR_NORMAL_MAX_CONTEXT_CHARS = env_int(
    "PFR_NORMAL_MAX_CONTEXT_CHARS",
    150_000,
)
MAX_TOOL_TRACE_EVENTS = env_int("MAX_TOOL_TRACE_EVENTS", 25)
MAX_TOOL_TRACE_CHARS = env_int("MAX_TOOL_TRACE_CHARS", 12_000)
MAX_CONTEXT_ITEM_BYTES = env_int("MAX_CONTEXT_ITEM_BYTES", 300_000)
MAX_DYNAMODB_WIRE_BYTES = env_int("MAX_DYNAMODB_WIRE_BYTES", 320_000)

REVIEW_DEEP_THINKING_MAX_TOKENS = env_int("REVIEW_DEEP_THINKING_MAX_TOKENS", 64_000)
REVIEW_FINAL_OUTPUT_MAX_TOKENS = env_int("REVIEW_FINAL_OUTPUT_MAX_TOKENS", 18_000)
# The review phase runs in its own 900-second Lambda invocation.  Keep a
# 30-second durable-state reserve and a second safety margin, but do not impose
# the former 240-second DeepSeek wall cap: a healthy queued response can exceed
# it without being a provider or model failure.  The shared stage/deadline still
# bounds Deep judgment plus Final serialization.
REVIEW_STAGE_TIMEOUT_SECONDS = env_int("REVIEW_STAGE_TIMEOUT_SECONDS", 720)
REVIEW_DEEP_THINKING_TIMEOUT_SECONDS = env_int("REVIEW_DEEP_THINKING_TIMEOUT_SECONDS", 540)
REVIEW_FINAL_OUTPUT_TIMEOUT_SECONDS = env_int("REVIEW_FINAL_OUTPUT_TIMEOUT_SECONDS", 240)
PFR_MAX_PLAN_QUESTIONS = env_int("PFR_MAX_PLAN_QUESTIONS", 8)
PFR_PLAN_DIGEST_MAX_CHARS = env_int("PFR_PLAN_DIGEST_MAX_CHARS", 30_000)
PFR_MAX_RECONCILE_ROUNDS = env_int("PFR_MAX_RECONCILE_ROUNDS", 2)
# Repair must resend the full compact reconcile object, but these are
# code-owned safety caps rather than per-deployment tuning knobs. Lambda's 4KB
# environment budget is already consumed by runtime credentials/config; adding
# low-value one-off variables makes otherwise safe deployments impossible.
PFR_RECONCILE_REPAIR_MAX_TOKENS = 12_000
PFR_RECONCILE_REPAIR_TIMEOUT_SECONDS = 120
PFR_MAX_SEARCH_CALLS = env_int("PFR_MAX_SEARCH_CALLS", 10)
PFR_MAX_READ_CALLS = env_int("PFR_MAX_READ_CALLS", 12)
PFR_NORMAL_MAX_SEARCH_CALLS = env_int("PFR_NORMAL_MAX_SEARCH_CALLS", 6)
PFR_NORMAL_MAX_READ_CALLS = env_int("PFR_NORMAL_MAX_READ_CALLS", 6)

DRY_RUN = env_bool("DRY_RUN", False)
PERSIST_REVIEW_ARTIFACT = env_bool("PERSIST_REVIEW_ARTIFACT", True)

TTL_DAYS = env_int("PIPELINE_TTL_DAYS", 30)
MAX_ATTEMPTS = env_int("PIPELINE_MAX_ATTEMPTS", 3)

# One compact ``key=value;key=value`` string rather than a variable per bound,
# because Lambda's 4KB environment budget is already nearly consumed. Empty
# keeps the reviewed defaults; ``off`` disables the bounds entirely.
PIPELINE_CAPACITY_POLICY = env("PIPELINE_CAPACITY_POLICY", "")
