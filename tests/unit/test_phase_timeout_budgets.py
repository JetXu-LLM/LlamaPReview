import time
import unittest
from unittest import mock

from tests.unit.fakes import ensure_repo_root_on_path, set_default_env

ensure_repo_root_on_path()
set_default_env()

from lambdas.LlamaPReviewPipeline import config
from lambdas.LlamaPReviewPipeline.deadline import Deadline, DeadlineExceeded
from lambdas.LlamaPReviewPipeline.review.judgment import (
    ReviewGenerationTimeout,
    phase_timeout_seconds,
)


class FrozenClock:
    def __init__(self, now: float = 1_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _deadline(seconds: float, *, reserve: float, clock: FrozenClock) -> Deadline:
    return Deadline.for_seconds(seconds, reserve_seconds=reserve, clock=clock)


class PhaseTimeoutResolutionTests(unittest.TestCase):
    """The resolved phase timeout is the tightest of three independent bounds."""

    def test_final_receives_its_configured_budget_when_nothing_is_tighter(self):
        clock = FrozenClock()
        resolved = phase_timeout_seconds(
            config.REVIEW_FINAL_OUTPUT_TIMEOUT_SECONDS,
            time.monotonic(),
            phase="final_presentation",
            deadline=_deadline(3_600, reserve=30, clock=clock),
        )

        self.assertEqual(resolved, float(config.REVIEW_FINAL_OUTPUT_TIMEOUT_SECONDS))

    def test_deep_receives_its_configured_budget_when_nothing_is_tighter(self):
        clock = FrozenClock()
        resolved = phase_timeout_seconds(
            config.REVIEW_DEEP_THINKING_TIMEOUT_SECONDS,
            time.monotonic(),
            phase="deep_judgment",
            deadline=_deadline(3_600, reserve=30, clock=clock),
        )

        self.assertEqual(
            resolved, float(config.REVIEW_DEEP_THINKING_TIMEOUT_SECONDS)
        )

    def test_the_invocation_deadline_outranks_any_phase_constant(self):
        clock = FrozenClock()
        resolved = phase_timeout_seconds(
            10_000,
            time.monotonic(),
            phase="final_presentation",
            deadline=_deadline(200, reserve=30, clock=clock),
        )

        self.assertEqual(resolved, 170.0)

    def test_the_state_write_reserve_is_never_spent_on_a_model_call(self):
        clock = FrozenClock()
        reserve = float(config.PIPELINE_STATE_WRITE_RESERVE_SECONDS)
        resolved = phase_timeout_seconds(
            10_000,
            time.monotonic(),
            phase="deep_judgment",
            deadline=_deadline(600, reserve=reserve, clock=clock),
        )

        self.assertEqual(resolved, 600.0 - reserve)
        self.assertLess(resolved, 600.0)

    def test_the_review_stage_budget_outranks_a_larger_phase_constant(self):
        with mock.patch.object(config, "REVIEW_STAGE_TIMEOUT_SECONDS", 720):
            resolved = phase_timeout_seconds(
                10_000,
                time.monotonic() - 700.0,
                phase="final_presentation",
                deadline=_deadline(3_600, reserve=30, clock=FrozenClock()),
            )

        self.assertAlmostEqual(resolved, 20.0, delta=1.0)

    def test_an_exhausted_stage_budget_refuses_before_any_model_call(self):
        with mock.patch.object(config, "REVIEW_STAGE_TIMEOUT_SECONDS", 720):
            with self.assertRaises(ReviewGenerationTimeout):
                phase_timeout_seconds(
                    240,
                    time.monotonic() - 721.0,
                    phase="final_presentation",
                    deadline=_deadline(3_600, reserve=30, clock=FrozenClock()),
                )

    def test_an_exhausted_invocation_deadline_refuses_before_any_model_call(self):
        clock = FrozenClock()
        with self.assertRaises(DeadlineExceeded):
            phase_timeout_seconds(
                240,
                time.monotonic(),
                phase="final_presentation",
                deadline=_deadline(30, reserve=30, clock=clock),
            )


class TimeoutBudgetArithmeticTests(unittest.TestCase):
    """The configured phase budgets have to compose inside one invocation."""

    def test_deep_and_final_cannot_together_outlast_the_review_stage(self):
        clock = FrozenClock()
        stage_started = time.monotonic()
        deep = phase_timeout_seconds(
            config.REVIEW_DEEP_THINKING_TIMEOUT_SECONDS,
            stage_started,
            phase="deep_judgment",
            deadline=_deadline(3_600, reserve=30, clock=clock),
        )
        final_after_a_maximal_deep = phase_timeout_seconds(
            config.REVIEW_FINAL_OUTPUT_TIMEOUT_SECONDS,
            stage_started - deep,
            phase="final_presentation",
            deadline=_deadline(3_600, reserve=30, clock=clock),
        )

        self.assertLessEqual(
            deep + final_after_a_maximal_deep,
            float(config.REVIEW_STAGE_TIMEOUT_SECONDS),
        )

    def test_a_maximal_deep_still_leaves_final_a_usable_budget(self):
        clock = FrozenClock()
        stage_started = time.monotonic() - float(
            config.REVIEW_DEEP_THINKING_TIMEOUT_SECONDS
        )
        resolved = phase_timeout_seconds(
            config.REVIEW_FINAL_OUTPUT_TIMEOUT_SECONDS,
            stage_started,
            phase="final_presentation",
            deadline=_deadline(3_600, reserve=30, clock=clock),
        )

        # Production Final completes at 10.3s median and 26.7s at p99, so the
        # budget left after the slowest permitted Deep still has to clear it.
        self.assertGreater(resolved, 60.0)

    def test_the_review_stage_fits_inside_lambda_minus_the_write_reserve(self):
        lambda_timeout = 900.0
        self.assertLessEqual(
            float(config.REVIEW_STAGE_TIMEOUT_SECONDS)
            + float(config.PIPELINE_STATE_WRITE_RESERVE_SECONDS),
            lambda_timeout,
        )


if __name__ == "__main__":
    unittest.main()
