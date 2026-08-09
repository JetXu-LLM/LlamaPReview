"""Adversarial integrity checks for canonical provider-ledger partitions."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import unittest

from lambdas.LlamaPReviewPipeline.provider_accounting import (
    reconcile_provider_accounting,
    sha256_value,
)


def _call() -> dict[str, object]:
    operation = {
        "run_id": "provider-accounting-integrity",
        "head_sha": "a" * 40,
        "pipeline_phase": "review",
        "pipeline_attempt": 1,
        "phase": "deep_judgment",
        "call_index": 1,
    }
    operation_id = sha256_value(operation)
    return {
        **operation,
        "schema_version": 2,
        "operation_id": operation_id,
        "call_id": sha256_value(
            {
                "operation_id": operation_id,
                "transport_attempt_index": 1,
            }
        ),
        "model": "deepseek-v4-pro",
        "logical_model": "deepseek-v4-pro",
        "billed_model": "deepseek-v4-flash",
        "status": "completed",
        "usage_state": "reported",
        "transport_attempt_index": 1,
        "transport_dispatch_count": 1,
        "transport_attempt_count": 1,
        "usage": {
            "prompt_tokens": 80,
            "completion_tokens": 20,
            "total_tokens": 100,
        },
    }


def _artifact() -> dict[str, object]:
    call = _call()
    usage = deepcopy(call["usage"])
    return {
        "deepseek_all_attempt_model_phases": [call],
        "deepseek_model_phases": [deepcopy(call)],
        "deepseek_discarded_model_phases": [],
        "deepseek_usage_total": usage,
        "deepseek_winning_usage_total": deepcopy(usage),
        "deepseek_discarded_usage_total": {},
        "deepseek_usage_accounting": {
            "schema_version": 2,
            "all_call_count": 1,
            "winning_call_count": 1,
            "discarded_call_count": 0,
            "transport_operation_count": 1,
            "unreported_usage_call_count": 0,
            "complete_numeric_usage": True,
            "usage_merge_conflicts": [],
        },
    }


def _dynamodb_numbers(value):
    if type(value) is int:
        return Decimal(value)
    if isinstance(value, list):
        return [_dynamodb_numbers(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _dynamodb_numbers(item)
            for key, item in value.items()
        }
    return value


class ProviderAccountingIntegrityTests(unittest.TestCase):
    def _receipt(self, artifact):
        return reconcile_provider_accounting(
            artifact,
            expected_transport_model_override="deepseek-v4-flash",
        )

    def test_exact_partition_copy_is_valid(self):
        receipt = self._receipt(_artifact())
        self.assertTrue(receipt["valid"], receipt["errors"])

    def test_dynamodb_decimal_protocol_integers_are_valid(self):
        artifact = _dynamodb_numbers(_artifact())

        receipt = self._receipt(artifact)

        self.assertTrue(receipt["valid"], receipt["errors"])

    def test_fractional_dynamodb_protocol_number_is_rejected(self):
        artifact = _artifact()
        artifact["deepseek_all_attempt_model_phases"][0][
            "transport_attempt_index"
        ] = Decimal("1.5")

        receipt = self._receipt(artifact)

        self.assertFalse(receipt["valid"])
        self.assertIn(
            "provider_transport_attempt_sequence_invalid",
            receipt["errors"],
        )

    def test_winning_partition_cannot_rewrite_canonical_call(self):
        mutations = {
            "model": "fabricated-model",
            "billed_model": "fabricated-transport",
            "pipeline_phase": "context",
            "phase": "final_presentation",
            "pipeline_attempt": 99,
            "call_index": 99,
            "status": "dispatching",
            "operation_id": "f" * 64,
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                artifact = _artifact()
                artifact["deepseek_model_phases"][0][field] = value
                if field == "usage":
                    artifact["deepseek_winning_usage_total"] = deepcopy(value)
                receipt = self._receipt(artifact)
                self.assertFalse(receipt["valid"])
                self.assertIn(
                    "winning_call_record_mismatch",
                    receipt["errors"],
                )

    def test_discarded_partition_cannot_rewrite_canonical_call(self):
        artifact = _artifact()
        canonical = artifact["deepseek_all_attempt_model_phases"][0]
        artifact["deepseek_model_phases"] = []
        artifact["deepseek_discarded_model_phases"] = [deepcopy(canonical)]
        artifact["deepseek_winning_usage_total"] = {}
        artifact["deepseek_discarded_usage_total"] = deepcopy(
            canonical["usage"]
        )
        accounting = artifact["deepseek_usage_accounting"]
        accounting["winning_call_count"] = 0
        accounting["discarded_call_count"] = 1
        valid = self._receipt(artifact)
        self.assertTrue(valid["valid"], valid["errors"])

        artifact["deepseek_discarded_model_phases"][0]["model"] = (
            "fabricated-model"
        )
        receipt = self._receipt(artifact)
        self.assertFalse(receipt["valid"])
        self.assertIn("discarded_call_record_mismatch", receipt["errors"])

    def test_transport_attempt_sequence_cannot_start_after_one(self):
        artifact = _artifact()
        for partition in (
            "deepseek_all_attempt_model_phases",
            "deepseek_model_phases",
        ):
            record = artifact[partition][0]
            record["transport_attempt_index"] = 2
            record["call_id"] = sha256_value(
                {
                    "operation_id": record["operation_id"],
                    "transport_attempt_index": 2,
                }
            )
        receipt = self._receipt(artifact)
        self.assertFalse(receipt["valid"])
        self.assertIn(
            "provider_transport_attempt_sequence_invalid",
            receipt["errors"],
        )


if __name__ == "__main__":
    unittest.main()
