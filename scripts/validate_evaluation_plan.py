#!/usr/bin/env python3
"""Validate a local, unapproved evaluation plan; never execute its planned calls.

This module deliberately imports no runtime, provider, network, or subprocess
code. A valid plan is not an authorization, a completed evaluation, or a claim
that declared public sources, hashes, traffic, or account credits were verified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


DEFAULT_PLAN = Path(__file__).resolve().parents[1] / "config/evaluation_plan.example.json"
SCHEMA = "llamapreview.evaluation-plan/v1"
MAX_PLAN_BYTES = 1_048_576
MODEL_EFFORTS = {
    "gpt-6-luna": {"medium", "max"},
    "gpt-6.1-terra": {"low", "medium", "high", "xhigh", "max"},
}
PHASE_CALL_CAPS = {
    "route": 2,
    "pfr_plan": 1,
    "pfr_reconcile": 3,
    "deep": 1,
    "final": 1,
    "correction": 1,
}
ID_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
REPOSITORY_PATTERN = re.compile(r"https://github\.com/[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+\Z")


class EvaluationPlanError(ValueError):
    """The supplied document is not a bounded, offline planning manifest."""


def _object(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise EvaluationPlanError(f"{label}: unexpected or missing fields")
    return value


def _choice(value, choices, label):
    if not isinstance(value, str) or value not in choices:
        raise EvaluationPlanError(f"{label}: unsupported value")
    return value


def _positive(value, label):
    if type(value) is not int or value <= 0:
        raise EvaluationPlanError(f"{label}: expected a positive integer")
    return value


def _identifier(value, label):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise EvaluationPlanError(f"{label}: invalid identifier")
    return value


def _digest(value, length, label, *, optional=False):
    if value is None and optional:
        return
    if not isinstance(value, str) or not re.fullmatch(rf"[0-9a-f]{{{length}}}", value):
        raise EvaluationPlanError(f"{label}: expected an exact lowercase hexadecimal digest")


def _unique_strings(value, label):
    if not isinstance(value, list) or not value:
        raise EvaluationPlanError(f"{label}: expected a nonempty list")
    if any(not isinstance(item, str) for item in value) or len(set(value)) != len(value):
        raise EvaluationPlanError(f"{label}: expected unique strings")
    return value


def _records(value, label):
    if not isinstance(value, list) or not value:
        raise EvaluationPlanError(f"{label}: expected a nonempty list")
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            raise EvaluationPlanError(f"{label}: expected objects")
        identity = _identifier(item.get("id"), label)
        if identity in seen:
            raise EvaluationPlanError(f"{label}: duplicate identifier")
        seen.add(identity)
    return value


def validate_plan(plan):
    """Return a structural-validation receipt, without reading case artifacts."""
    _object(
        plan,
        (
            "schema_version",
            "status",
            "execution",
            "baseline",
            "limits",
            "candidates",
            "cases",
            "batches",
            "selection",
        ),
        "plan",
    )
    if plan["schema_version"] != SCHEMA or plan["status"] != "unapproved":
        raise EvaluationPlanError("plan: schema or unapproved status required")
    execution = _object(
        plan["execution"],
        (
            "mode",
            "approved_provider_calls",
            "approved_budget_usd",
            "github_product_writes",
            "aws_product_writes",
        ),
        "execution",
    )
    if (
        execution["mode"] != "offline_plan_only"
        or type(execution["approved_provider_calls"]) is not int
        or execution["approved_provider_calls"] != 0
        or execution["approved_budget_usd"] != "0.00"
        or execution["github_product_writes"] is not False
        or execution["aws_product_writes"] is not False
    ):
        raise EvaluationPlanError("execution: all live authority must remain disabled")
    baseline = _object(
        plan["baseline"],
        (
            "candidate_source_commit",
            "sdk_wheel_sha256",
            "production_identity",
            "traffic_window_utc",
            "observed_monthly_admitted_runs",
            "credit_grant_verified",
        ),
        "baseline",
    )
    _digest(baseline["candidate_source_commit"], 40, "candidate source", optional=True)
    _digest(baseline["sdk_wheel_sha256"], 64, "SDK wheel", optional=True)
    if (
        baseline["production_identity"] != "unverified"
        or baseline["traffic_window_utc"] is not None
        or baseline["observed_monthly_admitted_runs"] is not None
        or baseline["credit_grant_verified"] is not False
    ):
        raise EvaluationPlanError(
            "baseline: operational/account claims belong in a private receipt"
        )
    limits = _object(
        plan["limits"],
        (
            "max_planned_provider_calls",
            "max_input_tokens_per_call",
            "max_output_tokens_per_call",
            "max_call_wall_seconds",
        ),
        "limits",
    )
    for key, value in limits.items():
        _positive(value, key)
    candidates = {}
    for item in _records(plan["candidates"], "candidates"):
        _object(item, ("id", "model", "effort", "availability"), "candidate")
        model = _choice(item["model"], MODEL_EFFORTS, "candidate model")
        _choice(item["effort"], MODEL_EFFORTS[model], "candidate effort")
        _choice(item["availability"], {"initial", "conditional"}, "candidate availability")
        if model == "gpt-6.1-terra" and item["effort"] in {"low", "xhigh", "max"}:
            if item["availability"] != "conditional":
                raise EvaluationPlanError(
                    "candidate: Terra low/xhigh/max require conditional screening"
                )
        candidates[item["id"]] = item
    cases = {}
    unresolved = 0
    for item in _records(plan["cases"], "cases"):
        _object(
            item,
            ("id", "scope", "split", "repository_url", "head_sha", "input_sha256", "coverage"),
            "case",
        )
        _choice(item["scope"], {"synthetic_placeholder", "public_snapshot"}, "case scope")
        _choice(item["split"], {"development", "confirmation"}, "case split")
        for tag in _unique_strings(item["coverage"], "case coverage"):
            _identifier(tag, "case coverage")
        if item["scope"] == "synthetic_placeholder":
            if any(item[key] is not None for key in ("repository_url", "head_sha", "input_sha256")):
                raise EvaluationPlanError(
                    "case: synthetic placeholders must not masquerade as frozen inputs"
                )
            unresolved += 1
        else:
            url = item["repository_url"]
            if not isinstance(url, str) or not REPOSITORY_PATTERN.fullmatch(url):
                raise EvaluationPlanError("case: expected a canonical public GitHub repository URL")
            if url.rsplit("/", 1)[-1] in {".", ".."}:
                raise EvaluationPlanError("case: invalid repository path")
            _digest(item["head_sha"], 40, "case head")
            _digest(item["input_sha256"], 64, "case input")
        cases[item["id"]] = item
    planned_calls = 0
    batch_bounds = {}
    for batch in _records(plan["batches"], "batches"):
        _object(
            batch,
            (
                "id",
                "phase",
                "tiers",
                "cases",
                "candidates",
                "max_repeats",
                "max_calls_per_case_candidate",
                "requires",
            ),
            "batch",
        )
        phase = _choice(batch["phase"], PHASE_CALL_CAPS, "batch phase")
        requirement = _choice(
            batch["requires"],
            {"initial_screen", "after_screening", "confirmation_only"},
            "batch requirement",
        )
        tiers = _unique_strings(batch["tiers"], "batch tiers")
        allowed_tiers = {"unrouted"} if phase == "route" else {"low", "normal", "high"}
        if not set(tiers) <= allowed_tiers:
            raise EvaluationPlanError("batch: Route is unrouted; later phases use existing tiers")
        if phase in {"pfr_plan", "pfr_reconcile"} and "low" in tiers:
            raise EvaluationPlanError("batch: do not add PFR work to the low path")
        case_ids = _unique_strings(batch["cases"], "batch cases")
        candidate_ids = _unique_strings(batch["candidates"], "batch candidates")
        if not set(case_ids) <= set(cases) or not set(candidate_ids) <= set(candidates):
            raise EvaluationPlanError("batch: unresolved case or candidate reference")
        for case_id in case_ids:
            if (cases[case_id]["split"] == "confirmation") != (requirement == "confirmation_only"):
                raise EvaluationPlanError("batch: confirmation cases cannot be used for screening")
        if requirement == "initial_screen" and any(
            candidates[candidate_id]["availability"] == "conditional"
            for candidate_id in candidate_ids
        ):
            raise EvaluationPlanError(
                "batch: conditional candidates cannot enter initial screening"
            )
        repeats = _positive(batch["max_repeats"], "batch repeats")
        calls = _positive(batch["max_calls_per_case_candidate"], "batch calls")
        if calls > PHASE_CALL_CAPS[phase]:
            raise EvaluationPlanError("batch: phase call cap exceeded")
        bound = len(case_ids) * len(candidate_ids) * repeats * calls
        planned_calls += bound
        batch_bounds[batch["id"]] = bound
    if planned_calls > limits["max_planned_provider_calls"]:
        raise EvaluationPlanError("limits: planned matrix exceeds its provider-call ceiling")
    selection = _object(
        plan["selection"],
        ("production_profile_selected", "quality_claim", "monthly_projection_usd"),
        "selection",
    )
    if (
        selection["production_profile_selected"] is not False
        or selection["quality_claim"] != "not_evaluated"
        or selection["monthly_projection_usd"] is not None
    ):
        raise EvaluationPlanError("selection: an offline plan cannot assert measured results")
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {
        "schema_version": "llamapreview.evaluation-plan-receipt/v1",
        "plan_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "structurally_valid": True,
        "execution_permitted": False,
        "provider_calls_made": 0,
        "github_product_writes": 0,
        "aws_product_writes": 0,
        "planned_call_ceiling": planned_calls,
        "http_attempt_ceiling": None,
        "batch_call_ceilings": batch_bounds,
        "unresolved_case_placeholders": unresolved,
        "source_binding_complete": all(
            baseline[key] is not None for key in ("candidate_source_commit", "sdk_wheel_sha256")
        ),
        "quality_evaluated": False,
        "public_source_visibility_verified": False,
        "account_credit_verified": False,
    }


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvaluationPlanError("JSON: duplicate object key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise EvaluationPlanError("JSON: non-finite numeric constant")


def load_plan(path=DEFAULT_PLAN):
    """Read only the named JSON document, never referenced repositories/inputs."""
    with Path(path).open("rb") as source:
        data = source.read(MAX_PLAN_BYTES + 1)
    if len(data) > MAX_PLAN_BYTES:
        raise EvaluationPlanError("plan: file exceeds the local size bound")
    return json.loads(
        data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_PLAN)
    args = parser.parse_args(argv)
    try:
        receipt = validate_plan(load_plan(args.manifest))
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        EvaluationPlanError,
        RecursionError,
    ) as exc:
        parser.error(str(exc))
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
