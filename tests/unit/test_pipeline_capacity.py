import datetime
import unittest

from lambdas.LlamaPReviewPipeline import pipeline_capacity
from tests.unit.fakes import FakeTable


WINDOW_START = datetime.datetime(2026, 8, 20, 9, 0, tzinfo=datetime.timezone.utc)
NEXT_WINDOW = datetime.datetime(2026, 8, 21, 0, 30, tzinfo=datetime.timezone.utc)
HEAD = "a" * 40


def policy(**overrides):
    base = {"repo_daily": 3, "global_daily": 100, "successor_enabled": True}
    base.update(overrides)
    return pipeline_capacity.CapacityPolicy(**base)


class CapacityPolicyParsingTests(unittest.TestCase):
    def test_empty_policy_keeps_the_hosted_defaults(self):
        parsed = pipeline_capacity.parse_policy("")

        self.assertEqual(parsed.repo_daily, pipeline_capacity.DEFAULT_REPO_DAILY)
        self.assertEqual(parsed.global_daily, pipeline_capacity.DEFAULT_GLOBAL_DAILY)
        self.assertTrue(parsed.successor_enabled)

    def test_operator_can_retune_each_bound_and_succession(self):
        parsed = pipeline_capacity.parse_policy(
            "repo_daily=5;global_daily=42;successor=off"
        )

        self.assertEqual(parsed.repo_daily, 5)
        self.assertEqual(parsed.global_daily, 42)
        self.assertFalse(parsed.successor_enabled)

    def test_policy_can_be_disabled_without_disabling_succession(self):
        parsed = pipeline_capacity.parse_policy("off")

        self.assertFalse(parsed.enabled)
        self.assertTrue(parsed.successor_enabled)

    def test_explicit_invalid_values_fail_closed(self):
        invalid = (
            "repo_daily=",
            "global_daily=nonsense",
            "repo_daily=-1",
            "successor=maybe",
            "unknown=1",
            "repo_daily=3;repo_daily=4",
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                pipeline_capacity.parse_policy(raw)

    def test_single_sentinel_policy_requires_a_safe_global_bound(self):
        unsafe = (
            "repo_daily=3;global_daily=0",
            "repo_daily=513;global_daily=100",
            f"repo_daily={'9' * 39};global_daily=100",
            "repo_daily=3;global_daily=513",
            "repo_daily=0;global_daily=513",
        )
        for raw in unsafe:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                pipeline_capacity.parse_policy(raw)

        bounded = pipeline_capacity.parse_policy(
            "repo_daily=0;global_daily=512"
        )
        self.assertEqual(bounded.repo_daily, 0)
        self.assertEqual(bounded.global_daily, pipeline_capacity.MAX_GLOBAL_DAILY)

        with self.assertRaises(ValueError):
            pipeline_capacity.CapacityPolicy(repo_daily=3, global_daily=0)
        with self.assertRaises(ValueError):
            pipeline_capacity.CapacityPolicy(repo_daily=513, global_daily=100)
        with self.assertRaises(ValueError):
            pipeline_capacity.CapacityPolicy(repo_daily=0, global_daily=513)


class CapacityAdmissionIdentityTests(unittest.TestCase):
    def test_every_exact_run_field_participates_in_the_admission_id(self):
        base = pipeline_capacity.capacity_admission_id(
            "owner/repo", 7, "run-7", HEAD, is_successor=False
        )
        variants = {
            pipeline_capacity.capacity_admission_id(
                "other/repo", 7, "run-7", HEAD, is_successor=False
            ),
            pipeline_capacity.capacity_admission_id(
                "owner/repo", 8, "run-7", HEAD, is_successor=False
            ),
            pipeline_capacity.capacity_admission_id(
                "owner/repo", 7, "run-8", HEAD, is_successor=False
            ),
            pipeline_capacity.capacity_admission_id(
                "owner/repo", 7, "run-7", "b" * 40, is_successor=False
            ),
            pipeline_capacity.capacity_admission_id(
                "owner/repo", 7, "run-7", HEAD, is_successor=True
            ),
        }

        self.assertEqual(len(variants), 5)
        self.assertNotIn(base, variants)


class CapacityConsumptionTests(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable("pipeline")

    def consume(
        self,
        repo="owner/repo",
        *,
        pr_number=7,
        run_id="run-7",
        head_sha=HEAD,
        now=WINDOW_START,
        active_policy=None,
        **kwargs,
    ):
        return pipeline_capacity.consume(
            repo,
            pr_number,
            run_id,
            head_sha,
            table=self.table,
            policy=active_policy or policy(),
            now=now,
            **kwargs,
        )

    def sentinel(self, now=WINDOW_START):
        window = now.strftime("%Y-%m-%d")
        key = tuple(sorted(pipeline_capacity._sentinel_key(window).items()))
        return self.table.items[key]

    def test_one_atomic_update_charges_repo_and_global_and_records_admission(self):
        decision = self.consume()

        self.assertTrue(decision.allowed)
        self.assertEqual(len(self.table.update_calls), 1)
        update = self.table.update_calls[0]
        self.assertIn("#repo_count :one", update["UpdateExpression"])
        self.assertIn("#global_count :one", update["UpdateExpression"])
        self.assertIn(
            decision.admission_id,
            self.sentinel()["capacity_admission_ids"],
        )

    def test_same_run_retry_is_allowed_without_a_second_charge(self):
        first = self.consume()
        retry = self.consume()
        item = self.sentinel()

        self.assertTrue(first.allowed)
        self.assertTrue(retry.allowed)
        self.assertEqual(first.admission_id, retry.admission_id)
        self.assertEqual(
            item[pipeline_capacity._repo_counter_attribute("owner/repo")],
            1,
        )
        self.assertEqual(item["capacity_global_count"], 1)

    def test_distinct_runs_within_the_bound_are_admitted(self):
        decisions = [
            self.consume(pr_number=index, run_id=f"run-{index}")
            for index in (1, 2, 3)
        ]

        self.assertTrue(all(decision.allowed for decision in decisions))
        self.assertEqual(decisions[-1].used, 3)

    def test_the_run_after_the_repo_bound_is_blocked(self):
        for index in (1, 2, 3):
            self.consume(pr_number=index, run_id=f"run-{index}")

        decision = self.consume(pr_number=4, run_id="run-4")

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.block_reason, pipeline_capacity.BLOCK_REPO_DAILY)
        self.assertEqual(decision.used, 3)

    def test_each_repository_holds_a_separate_bound_on_one_daily_sentinel(self):
        for index in (1, 2, 3):
            self.consume("owner/busy", pr_number=index, run_id=f"busy-{index}")
        quiet = self.consume("owner/quiet", pr_number=1, run_id="quiet-1")

        self.assertTrue(quiet.allowed)
        self.assertEqual(len(self.table.items), 1)
        self.assertEqual(
            self.sentinel()["repo"],
            f"{pipeline_capacity.CAPACITY_SENTINEL_PREFIX}2026-08-20",
        )

    def test_a_new_utc_day_uses_a_new_sentinel(self):
        for index in (1, 2, 3):
            self.consume(pr_number=index, run_id=f"run-{index}")

        decision = self.consume(
            pr_number=4,
            run_id="run-4",
            now=NEXT_WINDOW,
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.used, 1)
        self.assertEqual(len(self.table.items), 2)

    def test_same_exact_run_is_charged_once_in_each_utc_day(self):
        first_day = self.consume()
        second_day = self.consume(now=NEXT_WINDOW)

        self.assertTrue(first_day.allowed)
        self.assertTrue(second_day.allowed)
        self.assertEqual(first_day.admission_id, second_day.admission_id)
        self.assertEqual(len(self.table.items), 2)
        self.assertEqual(
            self.sentinel(WINDOW_START)[
                pipeline_capacity._repo_counter_attribute("owner/repo")
            ],
            1,
        )
        self.assertEqual(
            self.sentinel(NEXT_WINDOW)[
                pipeline_capacity._repo_counter_attribute("owner/repo")
            ],
            1,
        )

    def test_notice_owner_is_bound_to_the_first_blocked_admission_and_its_retry(self):
        for index in (1, 2, 3):
            self.consume(pr_number=index, run_id=f"run-{index}")

        first = self.consume(pr_number=4, run_id="run-4")
        retry = self.consume(pr_number=4, run_id="run-4")
        later = self.consume(pr_number=5, run_id="run-5")

        self.assertTrue(first.should_notify)
        self.assertTrue(retry.should_notify)
        self.assertEqual(first.admission_id, retry.admission_id)
        self.assertFalse(later.should_notify)
        owner_attr = pipeline_capacity._notice_owner_attribute("owner/repo")
        self.assertEqual(self.sentinel()[owner_attr], first.admission_id)

    def test_a_blocked_successor_does_not_take_the_notice_owner(self):
        for index in (1, 2, 3):
            self.consume(pr_number=index, run_id=f"run-{index}")

        successor = self.consume(
            pr_number=4,
            run_id="successor-4",
            is_successor=True,
        )
        ordinary = self.consume(pr_number=5, run_id="run-5")

        self.assertFalse(successor.should_notify)
        self.assertTrue(ordinary.should_notify)

    def test_global_rejection_does_not_pollute_the_repository_counter(self):
        limits = policy(repo_daily=50, global_daily=1)
        self.consume("owner/a", active_policy=limits, run_id="run-a")

        blocked = self.consume(
            "owner/b",
            active_policy=limits,
            run_id="run-b",
        )
        retry = self.consume(
            "owner/b",
            active_policy=limits,
            run_id="run-b",
        )
        item = self.sentinel()

        self.assertFalse(blocked.allowed)
        self.assertFalse(retry.allowed)
        self.assertEqual(blocked.block_reason, pipeline_capacity.BLOCK_GLOBAL_DAILY)
        self.assertNotIn(
            pipeline_capacity._repo_counter_attribute("owner/b"),
            item,
        )
        self.assertNotIn(blocked.admission_id, item["capacity_admission_ids"])
        self.assertEqual(item["capacity_global_count"], 1)

    def test_global_full_rejects_atomically_while_repo_has_one_slot_left(self):
        limits = policy(repo_daily=2, global_daily=2)
        first_target = self.consume(
            "owner/target",
            active_policy=limits,
            run_id="target-1",
        )
        self.consume(
            "owner/other",
            active_policy=limits,
            run_id="other-1",
        )

        blocked = self.consume(
            "owner/target",
            pr_number=8,
            active_policy=limits,
            run_id="target-2",
        )
        item = self.sentinel()

        self.assertTrue(first_target.allowed)
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.block_reason, pipeline_capacity.BLOCK_GLOBAL_DAILY)
        self.assertEqual(
            item[pipeline_capacity._repo_counter_attribute("owner/target")],
            1,
        )
        self.assertEqual(item["capacity_global_count"], 2)
        self.assertNotIn(blocked.admission_id, item["capacity_admission_ids"])

    def test_disabled_policy_admits_without_touching_the_table(self):
        decision = self.consume(
            active_policy=pipeline_capacity.parse_policy("off")
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(self.table.update_calls, [])
        self.assertEqual(self.table.get_calls, [])


class CapacityNoticeTests(unittest.TestCase):
    def test_the_notice_states_the_bound_reset_and_an_alternative(self):
        decision = pipeline_capacity.CapacityDecision(
            allowed=False,
            block_reason=pipeline_capacity.BLOCK_REPO_DAILY,
            should_notify=True,
            used=3,
            limit=3,
            window="2026-08-20",
            resets_at="2026-08-21T00:00:00Z",
            admission_id="a" * 64,
        )

        reason = pipeline_capacity.capacity_notice_reason(decision)

        self.assertIn("3 reviews per UTC day", reason)
        self.assertIn("2026-08-21T00:00:00Z", reason)
        self.assertIn("Self-hosting", reason)
        self.assertIn("Nothing about this pull request was judged", reason)


if __name__ == "__main__":
    unittest.main()
