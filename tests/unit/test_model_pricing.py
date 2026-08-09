import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.model_pricing import (
    MODEL_PRICING_SCHEMA,
    PricingValidationError,
    load_pricing_file,
    normalize_pricing_table,
    price_call_records,
    sum_cost_receipts,
)


OFFICIAL_TEST_PRICING = {
    "schema_version": MODEL_PRICING_SCHEMA,
    "source_currency": "CNY",
    "reporting_currency": "USD",
    "authoritative_source": {
        "url": "https://api-docs.deepseek.com/quick_start/pricing/",
        "as_of": "2026-07-31",
    },
    "fx_conversion": {
        "source_currency": "CNY",
        "reporting_currency": "USD",
        "reporting_currency_per_source_currency": "0.1477958447423402847169052285",
        "euro_reference_rates": {
            "usd_per_eur": "1.1389",
            "cny_per_eur": "7.7059",
        },
        "derivation_formula": "usd_per_cny=usd_per_eur/cny_per_eur",
        "authoritative_source": {
            "url": "https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html",
            "as_of": "2026-07-27",
        },
    },
    "models": {
        "deepseek-v4-flash": {
            "cache_hit_input_per_million_source_currency": "0.02",
            "cache_miss_input_per_million_source_currency": "1",
            "output_per_million_source_currency": "2",
        },
        "deepseek-v4-pro": {
            "cache_hit_input_per_million_source_currency": "0.025",
            "cache_miss_input_per_million_source_currency": "3",
            "output_per_million_source_currency": "6",
        },
    },
}


def call(
    *,
    call_id: str,
    model: str,
    phase: str,
    cache_hit: int = 0,
    cache_miss: int = 1_000_000,
    output: int = 1_000_000,
    transport_attempt_count: int = 1,
):
    prompt = cache_hit + cache_miss
    return {
        "call_id": call_id,
        "model": model,
        "phase": phase,
        "status": "completed",
        "usage_state": "reported",
        "transport_attempt_count": transport_attempt_count,
        "usage": {
            "prompt_tokens": prompt,
            "prompt_cache_hit_tokens": cache_hit,
            "prompt_cache_miss_tokens": cache_miss,
            "completion_tokens": output,
            "total_tokens": prompt + output,
        },
    }


class TestModelPricing(unittest.TestCase):
    def test_mixed_flash_and_pro_are_priced_from_cny_and_frozen_ecb_fx(self):
        receipt = price_call_records(
            [
                call(
                    call_id="1" * 64,
                    model="deepseek-v4-flash",
                    phase="route",
                ),
                call(
                    call_id="2" * 64,
                    model="deepseek-v4-pro",
                    phase="deep_judgment",
                ),
            ],
            OFFICIAL_TEST_PRICING,
            expected_usage_total={
                "prompt_tokens": 2_000_000,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 2_000_000,
                "completion_tokens": 2_000_000,
                "total_tokens": 4_000_000,
            },
        )

        self.assertTrue(receipt["complete"], receipt["errors"])
        self.assertEqual(receipt["total_usd"], "1.773550136908083416602862742")
        self.assertEqual(
            receipt["per_model"]["deepseek-v4-flash"]["total_usd"],
            "0.4433875342270208541507156855",
        )
        self.assertEqual(
            receipt["per_model"]["deepseek-v4-pro"]["total_usd"],
            "1.3301626026810625624521470565",
        )
        self.assertEqual(
            receipt["per_phase"]["route"]["total_usd"],
            "0.4433875342270208541507156855",
        )

    def test_all_flash_low_route_never_inherits_pro_pricing(self):
        records = [
            call(
                call_id=str(index) * 64,
                model="deepseek-v4-flash",
                phase=phase,
                cache_miss=100,
                output=50,
            )
            for index, phase in enumerate(
                ("route", "deep_judgment", "final_presentation"),
                start=1,
            )
        ]
        receipt = price_call_records(
            records,
            OFFICIAL_TEST_PRICING,
            expected_usage_total={
                "prompt_tokens": 300,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 300,
                "completion_tokens": 150,
                "total_tokens": 450,
            },
        )

        self.assertTrue(receipt["complete"], receipt["errors"])
        self.assertEqual(set(receipt["per_model"]), {"deepseek-v4-flash"})
        self.assertEqual(receipt["per_model"]["deepseek-v4-flash"]["call_count"], 3)

    def test_schema_v2_prices_the_actual_billed_model_not_logical_tier(self):
        record = call(
            call_id="9" * 64,
            model="deepseek-v4-pro",
            phase="deep_judgment",
        )
        record.update(
            {
                "schema_version": 2,
                "logical_model": "deepseek-v4-pro",
                "billed_model": "deepseek-v4-flash",
                "transport_attempt_index": 1,
                "transport_dispatch_count": 1,
            }
        )

        receipt = price_call_records([record], OFFICIAL_TEST_PRICING)

        self.assertTrue(receipt["complete"], receipt["errors"])
        self.assertEqual(set(receipt["per_model"]), {"deepseek-v4-flash"})
        self.assertEqual(receipt["calls"][0]["logical_model"], "deepseek-v4-pro")
        self.assertEqual(receipt["calls"][0]["billed_model"], "deepseek-v4-flash")

    def test_fx_rate_must_recompute_exactly_from_frozen_ecb_reference_rates(self):
        invalid = copy.deepcopy(OFFICIAL_TEST_PRICING)
        invalid["fx_conversion"][
            "reporting_currency_per_source_currency"
        ] = "0.14"

        with self.assertRaisesRegex(
            PricingValidationError,
            "does not match the frozen euro reference-rate derivation",
        ):
            normalize_pricing_table(invalid)

    def test_unknown_or_empty_model_fails_closed(self):
        empty = price_call_records(
            [call(call_id="1" * 64, model="", phase="route")],
            OFFICIAL_TEST_PRICING,
        )
        unknown = price_call_records(
            [
                call(
                    call_id="2" * 64,
                    model="deepseek-v4-unpriced",
                    phase="route",
                )
            ],
            OFFICIAL_TEST_PRICING,
        )

        self.assertFalse(empty["complete"])
        self.assertIn("call[0].model_missing", empty["errors"])
        self.assertFalse(unknown["complete"])
        self.assertIn(
            "call[0].billed_model_unknown:deepseek-v4-unpriced",
            unknown["errors"],
        )

    def test_retry_conflicting_negative_and_total_mismatch_fail_closed(self):
        retried = price_call_records(
            [
                call(
                    call_id="1" * 64,
                    model="deepseek-v4-flash",
                    phase="route",
                    transport_attempt_count=2,
                )
            ],
            OFFICIAL_TEST_PRICING,
        )
        self.assertIn(
            "call[0].retry_dispatch_usage_unreported",
            retried["errors"],
        )

        retry_record = call(
            call_id="5" * 64,
            model="deepseek-v4-flash",
            phase="route",
        )
        retry_record.update(
            {
                "schema_version": 2,
                "logical_model": "deepseek-v4-flash",
                "billed_model": "deepseek-v4-flash",
                "status": "http_retry",
                "usage_state": "unreported",
                "usage": {},
                "transport_attempt_index": 1,
                "transport_dispatch_count": 1,
            }
        )
        successful_retry = call(
            call_id="6" * 64,
            model="deepseek-v4-flash",
            phase="route",
        )
        successful_retry.update(
            {
                "schema_version": 2,
                "logical_model": "deepseek-v4-flash",
                "billed_model": "deepseek-v4-flash",
                "transport_attempt_index": 2,
                "transport_dispatch_count": 1,
            }
        )
        separate_dispatches = price_call_records(
            [retry_record, successful_retry],
            OFFICIAL_TEST_PRICING,
        )
        self.assertFalse(separate_dispatches["complete"])
        self.assertIn("call[0].usage_unreported", separate_dispatches["errors"])

        conflict_record = call(
            call_id="2" * 64,
            model="deepseek-v4-flash",
            phase="route",
        )
        conflict_record["usage"]["prompt_cache_details"] = {
            "hit_tokens": 1,
            "miss_tokens": 999_999,
        }
        conflicting = price_call_records(
            [conflict_record],
            OFFICIAL_TEST_PRICING,
        )
        self.assertTrue(
            any("conflicting values" in error for error in conflicting["errors"])
        )

        negative_record = call(
            call_id="3" * 64,
            model="deepseek-v4-flash",
            phase="route",
        )
        negative_record["usage"]["completion_tokens"] = -1
        negative = price_call_records([negative_record], OFFICIAL_TEST_PRICING)
        self.assertTrue(
            any("non-negative integer" in error for error in negative["errors"])
        )

        mismatched_record = call(
            call_id="4" * 64,
            model="deepseek-v4-flash",
            phase="route",
        )
        mismatched_record["usage"]["total_tokens"] += 1
        mismatched = price_call_records(
            [mismatched_record],
            OFFICIAL_TEST_PRICING,
        )
        self.assertTrue(
            any("total_tokens" in error for error in mismatched["errors"])
        )

    def test_any_rate_or_source_change_changes_full_table_identity(self):
        baseline = normalize_pricing_table(OFFICIAL_TEST_PRICING)
        changed_rate = copy.deepcopy(OFFICIAL_TEST_PRICING)
        changed_rate["models"]["deepseek-v4-flash"][
            "cache_hit_input_per_million_source_currency"
        ] = "0.03"
        changed_source_date = copy.deepcopy(OFFICIAL_TEST_PRICING)
        changed_source_date["authoritative_source"]["as_of"] = "2026-08-01"
        changed_fx_source_date = copy.deepcopy(OFFICIAL_TEST_PRICING)
        changed_fx_source_date["fx_conversion"]["authoritative_source"][
            "as_of"
        ] = "2026-07-28"

        self.assertEqual(baseline["source_currency"], "CNY")
        self.assertEqual(
            baseline["authoritative_source"],
            {
                "url": "https://api-docs.deepseek.com/quick_start/pricing/",
                "as_of": "2026-07-31",
            },
        )
        self.assertEqual(
            baseline["fx_conversion"]["euro_reference_rates"],
            {"usd_per_eur": "1.1389", "cny_per_eur": "7.7059"},
        )
        self.assertEqual(
            baseline["fx_conversion"]["authoritative_source"],
            {
                "url": "https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html",
                "as_of": "2026-07-27",
            },
        )

        self.assertNotEqual(
            baseline["pricing_identity_sha256"],
            normalize_pricing_table(changed_rate)["pricing_identity_sha256"],
        )
        self.assertNotEqual(
            baseline["pricing_identity_sha256"],
            normalize_pricing_table(changed_source_date)[
                "pricing_identity_sha256"
            ],
        )
        self.assertNotEqual(
            baseline["pricing_identity_sha256"],
            normalize_pricing_table(changed_fx_source_date)[
                "pricing_identity_sha256"
            ],
        )

    def test_cohort_adds_cell_receipts_without_repricing_merged_usage(self):
        flash = price_call_records(
            [
                call(
                    call_id="1" * 64,
                    model="deepseek-v4-flash",
                    phase="route",
                )
            ],
            OFFICIAL_TEST_PRICING,
        )
        pro = price_call_records(
            [
                call(
                    call_id="2" * 64,
                    model="deepseek-v4-pro",
                    phase="deep_judgment",
                )
            ],
            OFFICIAL_TEST_PRICING,
        )
        cohort = sum_cost_receipts(
            [flash, pro],
            OFFICIAL_TEST_PRICING,
            cell_ids=["flash-cell", "pro-cell"],
        )

        self.assertTrue(cohort["complete"], cohort["errors"])
        self.assertEqual(cohort["total_usd"], "1.773550136908083416602862742")
        self.assertEqual(
            cohort["per_cell"],
            [
                {
                    "cell_id": "flash-cell",
                    "call_count": 1,
                    "total_usd": "0.4433875342270208541507156855",
                },
                {
                    "cell_id": "pro-cell",
                    "call_count": 1,
                    "total_usd": "1.3301626026810625624521470565",
                },
            ],
        )

    def test_pricing_file_must_be_absolute_and_hashes_normalized_table(self):
        with self.assertRaisesRegex(PricingValidationError, "absolute"):
            load_pricing_file(Path("pricing.json"))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "pricing.json"
            target.write_text(
                json.dumps(OFFICIAL_TEST_PRICING),
                encoding="utf-8",
            )
            loaded = load_pricing_file(target)

        self.assertEqual(
            loaded["pricing_identity_sha256"],
            normalize_pricing_table(OFFICIAL_TEST_PRICING)[
                "pricing_identity_sha256"
            ],
        )
