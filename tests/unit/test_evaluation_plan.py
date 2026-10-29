"""Offline-only evaluation planning contracts; no provider or product writes."""

import ast
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import validate_evaluation_plan as validator


class EvaluationPlanTests(unittest.TestCase):
    def setUp(self):
        self.plan = validator.load_plan()

    def assert_invalid(self, plan, message):
        with self.assertRaisesRegex(validator.EvaluationPlanError, message):
            validator.validate_plan(plan)

    def test_example_is_bounded_but_never_authorizes_execution(self):
        receipt = validator.validate_plan(self.plan)
        self.assertEqual(receipt["planned_call_ceiling"], 141)
        self.assertIsNone(receipt["http_attempt_ceiling"])
        self.assertEqual(receipt["unresolved_case_placeholders"], 12)
        self.assertEqual(receipt["provider_calls_made"], 0)
        self.assertEqual(receipt["github_product_writes"], 0)
        self.assertEqual(receipt["aws_product_writes"], 0)
        for key in (
            "execution_permitted",
            "source_binding_complete",
            "quality_evaluated",
            "public_source_visibility_verified",
            "account_credit_verified",
        ):
            self.assertIs(receipt[key], False)

    def test_plan_hash_is_stable_and_binds_changes(self):
        receipt = validator.validate_plan(self.plan)
        reordered = json.loads(json.dumps(self.plan, sort_keys=True))
        self.assertEqual(receipt["plan_sha256"], validator.validate_plan(reordered)["plan_sha256"])
        self.plan["limits"]["max_planned_provider_calls"] += 1
        self.assertNotEqual(
            receipt["plan_sha256"], validator.validate_plan(self.plan)["plan_sha256"]
        )

    def test_live_authority_cannot_be_encoded_in_the_plan(self):
        for key, value in (
            ("mode", "live"),
            ("approved_provider_calls", 1),
            ("approved_provider_calls", False),
            ("approved_budget_usd", "0.01"),
            ("github_product_writes", True),
            ("aws_product_writes", True),
        ):
            with self.subTest(key=key, value=value):
                plan = copy.deepcopy(self.plan)
                plan["execution"][key] = value
                self.assert_invalid(plan, "authority")
        self.plan["status"] = "approved"
        self.assert_invalid(self.plan, "unapproved")

    def test_models_and_efforts_are_explicitly_allowed(self):
        for model, effort in (
            ("gpt-6.1-sol", "high"),
            ("gpt-6.1-luna", "max"),
            ("gpt-6-luna", "high"),
            ("gpt-6-luna", "low"),
            ("gpt-6.1-terra", "none"),
            ("openai/gpt-6-luna", "max"),
        ):
            with self.subTest(model=model, effort=effort):
                plan = copy.deepcopy(self.plan)
                plan["candidates"][0].update(model=model, effort=effort)
                self.assert_invalid(plan, "unsupported")

    def test_conditional_efforts_cannot_be_initial(self):
        for effort in ("low", "xhigh", "max"):
            plan = copy.deepcopy(self.plan)
            plan["candidates"][2]["effort"] = effort
            self.assert_invalid(plan, "conditional")
        self.plan["batches"][0]["candidates"].append("terra_xhigh")
        self.assert_invalid(self.plan, "conditional candidates")

    def test_unknown_duplicate_and_unresolved_ids_are_rejected(self):
        for section in ("cases", "candidates", "batches"):
            plan = copy.deepcopy(self.plan)
            plan[section].append(copy.deepcopy(plan[section][0]))
            self.assert_invalid(plan, "duplicate identifier")
        for section in ("cases", "candidates"):
            plan = copy.deepcopy(self.plan)
            plan["batches"][0][section].append("missing")
            self.assert_invalid(plan, "unresolved")
            plan["batches"][0][section] = [plan["batches"][0][section][0]] * 2
            self.assert_invalid(plan, "unique strings")

    def test_boolean_zero_negative_and_unbounded_limits_are_rejected(self):
        for value in (True, False, 0, -1, None, 1.5, "100"):
            plan = copy.deepcopy(self.plan)
            plan["limits"]["max_planned_provider_calls"] = value
            self.assert_invalid(plan, "positive integer")
        self.plan["limits"]["max_planned_provider_calls"] = 140
        self.assert_invalid(self.plan, "ceiling")

    def test_phase_calls_and_tiers_preserve_existing_path(self):
        self.plan["batches"][0]["max_calls_per_case_candidate"] = 3
        self.assert_invalid(self.plan, "phase call cap")
        self.plan = validator.load_plan()
        self.plan["batches"][0]["tiers"] = ["low"]
        self.assert_invalid(self.plan, "Route is unrouted")
        self.plan = validator.load_plan()
        self.plan["batches"][2]["tiers"] = ["low"]
        self.assert_invalid(self.plan, "low path")
        self.plan = validator.load_plan()
        self.plan["batches"][0]["phase"] = "second_reviewer"
        self.assert_invalid(self.plan, "unsupported")

    def test_confirmation_inputs_cannot_be_tuning_inputs(self):
        self.plan["batches"][0]["cases"] = ["confirm_ci_wiring"]
        self.assert_invalid(self.plan, "confirmation")
        self.plan["batches"][0]["requires"] = "confirmation_only"
        self.assertTrue(validator.validate_plan(self.plan)["structurally_valid"])
        self.plan["batches"][0]["cases"] = ["low_clean"]
        self.assert_invalid(self.plan, "confirmation")

    def test_public_snapshot_requires_exact_bindings_but_is_not_verified(self):
        item = self.plan["cases"][0]
        item.update(
            scope="public_snapshot",
            repository_url="https://github.com/example/synthetic-fixture",
            head_sha="a" * 40,
            input_sha256="b" * 64,
        )
        self.plan["baseline"].update(candidate_source_commit="c" * 40, sdk_wheel_sha256="d" * 64)
        receipt = validator.validate_plan(self.plan)
        self.assertEqual(receipt["unresolved_case_placeholders"], 11)
        self.assertTrue(receipt["source_binding_complete"])
        self.assertFalse(receipt["public_source_visibility_verified"])
        self.assertFalse(receipt["execution_permitted"])
        for key, value in (
            ("head_sha", "main"),
            ("input_sha256", None),
            ("repository_url", "https://example.com/example/repo"),
            ("repository_url", "https://user:password@github.com/example/repo"),
            ("repository_url", "https://github.com/example/repo?token=redacted"),
        ):
            plan = copy.deepcopy(self.plan)
            plan["cases"][0][key] = value
            self.assert_invalid(plan, "case")

    def test_placeholder_cannot_claim_to_be_a_frozen_case(self):
        self.plan["cases"][0]["head_sha"] = "a" * 40
        self.assert_invalid(self.plan, "placeholders")

    def test_no_fabricated_traffic_grant_or_quality_claims(self):
        for key, value in (
            ("production_identity", "deployed"),
            ("traffic_window_utc", "some period"),
            ("observed_monthly_admitted_runs", 100),
            ("credit_grant_verified", True),
        ):
            plan = copy.deepcopy(self.plan)
            plan["baseline"][key] = value
            self.assert_invalid(plan, "private receipt")
        for key, value in (
            ("production_profile_selected", True),
            ("quality_claim", "improved"),
            ("monthly_projection_usd", "100.00"),
        ):
            plan = copy.deepcopy(self.plan)
            plan["selection"][key] = value
            self.assert_invalid(plan, "measured results")

    def test_unknown_fields_are_rejected(self):
        self.plan["execution"]["api_key"] = "not-a-real-secret"
        self.assert_invalid(self.plan, "unexpected or missing")

    def test_loader_rejects_ambiguous_and_oversized_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            for text, expected in (
                ('{"status":"unapproved","status":"approved"}', "duplicate"),
                ('{"limit": NaN}', "non-finite"),
                ('{"limit": Infinity}', "non-finite"),
                (" " * (validator.MAX_PLAN_BYTES + 1), "size bound"),
            ):
                path.write_text(text)
                with self.assertRaisesRegex(validator.EvaluationPlanError, expected):
                    validator.load_plan(path)

    def test_cli_prints_only_offline_receipt(self):
        output = io.StringIO()
        with mock.patch("socket.socket", side_effect=AssertionError("network forbidden")):
            with contextlib.redirect_stdout(output):
                self.assertEqual(validator.main([]), 0)
        receipt = json.loads(output.getvalue())
        self.assertFalse(receipt["execution_permitted"])
        self.assertNotIn("repository_url", output.getvalue())

    def test_validator_has_no_runtime_network_or_subprocess_dependencies(self):
        source = ast.parse(Path(validator.__file__).read_text())
        imports = set()
        for node in ast.walk(source):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module.split(".")[0])
        self.assertEqual(imports, {"__future__", "argparse", "hashlib", "json", "pathlib", "re"})


if __name__ == "__main__":
    unittest.main()
