"""Reservation configuration and deterministic business rules."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Iterable


class TaskState(str, Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    WAITING_LOGIN = "waiting_login"
    RUNNING = "running"
    SUBMITTING = "submitting"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    TIMED_OUT = "timed_out"
    STOPPED = "stopped"
    FAILED = "failed"


TERMINAL_STATES = {
    TaskState.SUCCEEDED,
    TaskState.TIMED_OUT,
    TaskState.STOPPED,
    TaskState.FAILED,
}

_TRANSITIONS = {
    TaskState.DRAFT: {TaskState.SCHEDULED, TaskState.STOPPED},
    TaskState.SCHEDULED: {
        TaskState.RUNNING,
        TaskState.WAITING_LOGIN,
        TaskState.STOPPED,
        TaskState.TIMED_OUT,
        TaskState.FAILED,
    },
    TaskState.WAITING_LOGIN: {
        TaskState.SCHEDULED,
        TaskState.RUNNING,
        TaskState.STOPPED,
        TaskState.TIMED_OUT,
    },
    TaskState.RUNNING: {
        TaskState.SUBMITTING,
        TaskState.WAITING_LOGIN,
        TaskState.STOPPED,
        TaskState.TIMED_OUT,
        TaskState.FAILED,
    },
    TaskState.SUBMITTING: {
        TaskState.VERIFYING,
        TaskState.RUNNING,
        TaskState.FAILED,
    },
    TaskState.VERIFYING: {
        TaskState.SUCCEEDED,
        TaskState.RUNNING,
        TaskState.STOPPED,
        TaskState.FAILED,
    },
    TaskState.SUCCEEDED: set(),
    TaskState.TIMED_OUT: {TaskState.SCHEDULED},
    TaskState.STOPPED: {TaskState.SCHEDULED},
    TaskState.FAILED: {TaskState.SCHEDULED},
}


def validate_transition(current: TaskState, target: TaskState) -> None:
    if current == target:
        return
    if target not in _TRANSITIONS[current]:
        raise ValueError(
            f"invalid task state transition: {current.value} -> {target.value}"
        )


class ReservationOutcome(str, Enum):
    SUCCESS = "success"
    NO_SEAT = "no_seat"
    CANDIDATE_FOUND = "candidate_found"
    ALREADY_RESERVED = "already_reserved"
    LOGIN_REQUIRED = "login_required"
    AMBIGUOUS = "ambiguous"
    FAILURE = "failure"

    @property
    def retryable(self) -> bool:
        return self in {
            ReservationOutcome.NO_SEAT,
            ReservationOutcome.CANDIDATE_FOUND,
        }

    @property
    def task_state(self) -> TaskState:
        return {
            ReservationOutcome.SUCCESS: TaskState.SUCCEEDED,
            ReservationOutcome.NO_SEAT: TaskState.RUNNING,
            ReservationOutcome.CANDIDATE_FOUND: TaskState.RUNNING,
            ReservationOutcome.ALREADY_RESERVED: TaskState.STOPPED,
            ReservationOutcome.LOGIN_REQUIRED: TaskState.WAITING_LOGIN,
            ReservationOutcome.AMBIGUOUS: TaskState.FAILED,
            ReservationOutcome.FAILURE: TaskState.FAILED,
        }[self]


@dataclass(frozen=True)
class SeatRule:
    priority: int
    start: int | None = None
    end: int | None = None
    included: frozenset[int] = frozenset()
    excluded: frozenset[int] = frozenset()
    accept_any: bool = False
    order: str = "asc"

    def __post_init__(self) -> None:
        if self.priority < 0:
            raise ValueError("priority must be non-negative")
        if not self.accept_any:
            if self.start is None or self.end is None:
                raise ValueError("seat range bounds are required")
            if self.start <= 0 or self.end < self.start:
                raise ValueError("seat range must have positive ordered bounds")
        if self.order not in {"asc", "desc", "random"}:
            raise ValueError("order must be asc, desc, or random")

    def candidates(self, available: set[int]) -> list[int]:
        if self.accept_any:
            allowed = available - self.excluded
        else:
            assert self.start is not None and self.end is not None
            within = {seat for seat in available if self.start <= seat <= self.end}
            allowed = (within | (self.included & available)) - self.excluded
        candidates = sorted(allowed, reverse=self.order == "desc")
        if self.order == "random":
            random.shuffle(candidates)
        return candidates


# Backward-compatible name used by the original feasibility probe.
SeatRange = SeatRule


@dataclass(frozen=True)
class ReservationConfig:
    name: str
    venue: str
    floor: str
    area: str
    reservation_date: date
    time_slot: str
    starts_at: datetime
    stops_at: datetime
    seat_rules: tuple[SeatRule, ...]
    refresh_min_seconds: float = 8.0
    refresh_max_seconds: float = 15.0
    max_consecutive_errors: int = 5
    observation_mode: bool = True
    notify_success: bool = True
    notify_timeout: bool = True
    notify_error: bool = True

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("task name is required")
        if not self.venue.strip() or not self.area.strip():
            raise ValueError("venue and area are required")
        if not self.time_slot.strip() or "-" not in self.time_slot:
            raise ValueError("time slot must use HH:MM-HH:MM")
        if self.stops_at <= self.starts_at:
            raise ValueError("stop time must be later than start time")
        if not self.seat_rules:
            raise ValueError("at least one seat rule is required")
        if self.refresh_min_seconds < 3:
            raise ValueError("refresh minimum must be at least 3 seconds")
        if self.refresh_max_seconds < self.refresh_min_seconds:
            raise ValueError("refresh maximum must not be below minimum")
        if self.max_consecutive_errors <= 0:
            raise ValueError("maximum consecutive errors must be positive")


def choose_seat(
    available: Iterable[int],
    rules: Iterable[SeatRule],
) -> int | None:
    available_set = set(available)
    for rule in sorted(rules, key=lambda item: item.priority):
        candidates = rule.candidates(available_set)
        if candidates:
            return candidates[0]
    return None


def classify_result_message(message: str) -> ReservationOutcome:
    normalized = "".join(message.split())
    if any(text in normalized for text in ("预约成功", "操作成功")):
        return ReservationOutcome.SUCCESS
    if any(
        text in normalized
        for text in ("已存在预约", "不可重复预约", "已有预约")
    ):
        return ReservationOutcome.ALREADY_RESERVED
    if any(
        text in normalized
        for text in ("暂无空闲座位", "没有空闲座位", "空闲0")
    ):
        return ReservationOutcome.NO_SEAT
    if not normalized:
        return ReservationOutcome.AMBIGUOUS
    return ReservationOutcome.FAILURE
