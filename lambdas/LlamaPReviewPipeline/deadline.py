"""Monotonic phase deadlines that preserve time for terminal state writes."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import config


class DeadlineExceeded(RuntimeError):
    """Raised before a phase consumes the time reserved for durable state writes.

    This intentionally does not inherit from ``TimeoutError`` or ``OSError``.
    Callers can therefore distinguish a pipeline wall-clock budget from socket,
    HTTP, and operating-system failures without relying on message text.
    """

    def __init__(self, stage: str, *, remaining_seconds: float = 0.0):
        self.stage = stage
        self.remaining_seconds = max(0.0, float(remaining_seconds))
        super().__init__(
            f"Pipeline deadline exhausted at {stage}; "
            f"{self.remaining_seconds:.3f}s usable time remains"
        )


@dataclass(frozen=True)
class Deadline:
    """A phase deadline derived from Lambda and local wall-clock limits."""

    started_at: float
    hard_deadline_at: float
    reserve_seconds: float
    _clock: Callable[[], float] = time.monotonic

    @classmethod
    def from_lambda_context(
        cls,
        lambda_context: Optional[Any],
        *,
        phase_limit_seconds: float,
        reserve_seconds: float = config.PIPELINE_STATE_WRITE_RESERVE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> "Deadline":
        now = float(clock())
        limits = [max(0.0, float(phase_limit_seconds))]
        getter = getattr(lambda_context, "get_remaining_time_in_millis", None)
        if callable(getter):
            limits.append(max(0.0, float(getter()) / 1000.0))
        return cls(
            started_at=now,
            hard_deadline_at=now + min(limits),
            reserve_seconds=max(0.0, float(reserve_seconds)),
            _clock=clock,
        )

    @classmethod
    def for_seconds(
        cls,
        seconds: float,
        *,
        reserve_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> "Deadline":
        return cls.from_lambda_context(
            None,
            phase_limit_seconds=seconds,
            reserve_seconds=reserve_seconds,
            clock=clock,
        )

    def hard_remaining_seconds(self) -> float:
        return max(0.0, self.hard_deadline_at - float(self._clock()))

    def remaining_seconds(self) -> float:
        """Return usable time, excluding the state-write reserve."""
        return max(0.0, self.hard_remaining_seconds() - self.reserve_seconds)

    def elapsed_seconds(self) -> float:
        return max(0.0, float(self._clock()) - self.started_at)

    def check(self, stage: str, *, minimum_seconds: float = 0.0) -> float:
        remaining = self.remaining_seconds()
        if remaining <= max(0.0, float(minimum_seconds)):
            raise DeadlineExceeded(stage, remaining_seconds=remaining)
        return remaining

    def timeout_for(
        self,
        requested_seconds: float,
        *,
        stage: str,
        minimum_seconds: float = 0.1,
    ) -> float:
        """Clamp one blocking operation to the phase's usable time."""
        remaining = self.check(stage, minimum_seconds=minimum_seconds)
        timeout = min(max(0.0, float(requested_seconds)), remaining)
        if timeout < minimum_seconds:
            raise DeadlineExceeded(stage, remaining_seconds=remaining)
        return timeout

    def snapshot(self) -> dict[str, float]:
        return {
            "elapsed_seconds": round(self.elapsed_seconds(), 3),
            "remaining_seconds": round(self.remaining_seconds(), 3),
            "hard_remaining_seconds": round(self.hard_remaining_seconds(), 3),
            "state_write_reserve_seconds": round(self.reserve_seconds, 3),
        }
