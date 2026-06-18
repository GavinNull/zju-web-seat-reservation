"""Bounded attempt timing independent from a concrete scheduler backend."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class AttemptPolicy:
    starts_at: datetime
    stops_at: datetime
    minimum_delay: float
    maximum_delay: float
    maximum_attempts: int

    def __post_init__(self) -> None:
        if self.stops_at <= self.starts_at:
            raise ValueError("stop time must be later than start time")
        if self.minimum_delay < 8:
            raise ValueError("minimum delay must be at least 8 seconds")
        if self.maximum_delay < self.minimum_delay:
            raise ValueError("maximum delay must not be below minimum")
        if self.maximum_attempts <= 0:
            raise ValueError("maximum attempts must be positive")

    def can_attempt(self, now: datetime, attempts: int) -> bool:
        return (
            self.starts_at <= now < self.stops_at
            and 0 <= attempts < self.maximum_attempts
        )

    def next_delay(self, consecutive_failures: int, random_unit: float) -> float:
        jitter = min(1.0, max(0.0, random_unit))
        base = self.minimum_delay + (
            self.maximum_delay - self.minimum_delay
        ) * jitter
        backoff = self.minimum_delay * (2 ** max(0, consecutive_failures))
        return min(self.maximum_delay, max(base, backoff))
