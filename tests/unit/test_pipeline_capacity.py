import datetime
import unittest

from lambdas.LlamaPReviewPipeline import pipeline_capacity
from tests.unit.fakes import FakeTable

WINDOW_START = datetime.datetime(2026, 8, 20, 9, 0, tzinfo=datetime.timezone.utc)
NEXT_WINDOW = datetime.datetime(2026, 8, 21, 0, 30, tzinfo=datetime.timezone.utc)


def policy(**overrides):
    base = {"repo_daily": 3, "global_daily": 100, "successor_enabled": True}
    base.update(overrides)
    return pipeline_capacity.CapacityPolicy(**base)


class CapacityPolicyParsingTests(unittest.TestCase):
    def test_empty_policy_keeps_the_reviewed_defaults(self):
        parsed = pipeline_capacity.parse_policy("")

        self.assertEqual(parsed.repo_daily, pipeline_capacity.DEFAULT_REPO_DAILY)
        self.assertEqual(parsed.global_daily, pipeline_capacity.DEFAULT_GLOBAL_DAILY)
        self.assertTrue(parsed.successor_enabled)

    def test_operator_can_retune_each_bound(self):
        parsed = pipeline_capacity.parse_policy(
            "repo_daily=5;global_daily=42;successor=off"
        )

        self.assertEqual(parsed.repo_daily, 5)
        self.assertEqual(parsed.global_daily, 42)
        self.assertFalse(parsed.successor_enabled)

    def test_policy_can_be_disabled_entirely(self):
        parsed = pipeline_capacity.parse_policy("off")

        self.assertFalse(parsed.enabled)

    def test_unparseable_values_fall_back_instead_of_removing_the_bound(self):
        parsed = pipeline_capacity.parse_policy("repo_daily=;global_daily=nonsense")

        self.assertEqual(parsed.repo_daily, pipeline_capacity.DEFAULT_REPO_DAILY)
        self.assertEqual(parsed.global_daily, pipeline_capacity.DEFAULT_GLOBAL_DAILY)


class CapacityConsumptionTests(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable("pipeline")

    def consume(self, repo="owner/repo", *, now=WINDOW_START, **kwargs):
        return pipeline_capacity.consume(
            repo, table=self.table, policy=policy(), now=now, **kwargs
        )

    def test_runs_within_the_bound_are_admitted(self):
        for expected in (1, 2, 3):
            decision = self.consume()

            self.assertTrue(decision.allowed)
            self.assertEqual(decision.used, expected)

    def test_the_run_after_the_bound_is_blocked(self):
        for _ in range(3):
            self.consume()

        decision = self.consume()

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.block_reason, pipeline_capacity.BLOCK_REPO_DAILY)
        self.assertEqual(decision.used, 4)

    def test_each_repository_holds_a_separate_bound(self):
        for _ in range(4):
            self.consume("owner/busy")

        decision = self.consume("owner/quiet")

        self.assertTrue(decision.allowed)

    def test_a_new_utc_day_restores_capacity(self):
        for _ in range(4):
            self.consume()

        decision = self.consume(now=NEXT_WINDOW)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.used, 1)

    def test_only_the_first_blocked_run_speaks_publicly(self):
        for _ in range(3):
            self.consume()

        notices = [self.consume().should_notify for _ in range(4)]

        self.assertEqual(notices, [True, False, False, False])

    def test_the_next_day_may_speak_once_again(self):
        for _ in range(4):
            self.consume()

        for _ in range(3):
            self.consume(now=NEXT_WINDOW)

        self.assertTrue(self.consume(now=NEXT_WINDOW).should_notify)

    def test_a_blocked_successor_never_speaks(self):
        for _ in range(3):
            self.consume()

        decision = self.consume(is_successor=True)

        self.assertFalse(decision.allowed)
        self.assertFalse(decision.should_notify)

    def test_a_silent_successor_does_not_consume_the_public_notice(self):
        for _ in range(3):
            self.consume()
        self.consume(is_successor=True)

        self.assertTrue(self.consume().should_notify)

    def test_the_global_breaker_bounds_total_spend(self):
        limits = policy(repo_daily=50, global_daily=2)
        for index in range(3):
            decision = pipeline_capacity.consume(
                f"owner/repo{index}",
                table=self.table,
                policy=limits,
                now=WINDOW_START,
            )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.block_reason, pipeline_capacity.BLOCK_GLOBAL_DAILY)

    def test_the_global_breaker_is_an_operator_condition_not_a_public_one(self):
        limits = policy(repo_daily=50, global_daily=1)
        pipeline_capacity.consume(
            "owner/a", table=self.table, policy=limits, now=WINDOW_START
        )
        decision = pipeline_capacity.consume(
            "owner/b", table=self.table, policy=limits, now=WINDOW_START
        )

        self.assertFalse(decision.allowed)
        self.assertFalse(decision.should_notify)

    def test_a_blocked_run_does_not_consume_global_capacity(self):
        limits = policy(repo_daily=1, global_daily=100)
        for _ in range(5):
            pipeline_capacity.consume(
                "owner/busy", table=self.table, policy=limits, now=WINDOW_START
            )

        global_row = self.table.items[
            (
                ("pr_number", pipeline_capacity.CAPACITY_PR_NUMBER),
                ("repo", pipeline_capacity.GLOBAL_CAPACITY_REPO),
            )
        ]

        self.assertEqual(global_row["capacity_count"], 1)

    def test_disabled_policy_admits_everything_without_touching_the_table(self):
        decision = pipeline_capacity.consume(
            "owner/repo",
            table=self.table,
            policy=pipeline_capacity.parse_policy("off"),
            now=WINDOW_START,
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(self.table.update_calls, [])

    def test_capacity_uses_a_sentinel_that_cannot_collide_with_a_review(self):
        self.consume()

        keys = {
            key
            for key in self.table.items
            if dict(key)["repo"] == "owner/repo"
        }

        self.assertEqual(
            keys,
            {(("pr_number", pipeline_capacity.CAPACITY_PR_NUMBER), ("repo", "owner/repo"))},
        )
        self.assertLess(pipeline_capacity.CAPACITY_PR_NUMBER, 0)


class CapacityNoticeTests(unittest.TestCase):
    def test_the_notice_states_the_bound_reset_and_an_alternative(self):
        decision = pipeline_capacity.CapacityDecision(
            allowed=False,
            block_reason=pipeline_capacity.BLOCK_REPO_DAILY,
            should_notify=True,
            used=4,
            limit=3,
            window="2026-08-20",
            resets_at="2026-08-21T00:00:00Z",
        )

        reason = pipeline_capacity.capacity_notice_reason(decision)

        self.assertIn("3 reviews per UTC day", reason)
        self.assertIn("2026-08-21T00:00:00Z", reason)
        self.assertIn("Self-hosting", reason)
        self.assertIn("Nothing about this pull request was judged", reason)


if __name__ == "__main__":
    unittest.main()
