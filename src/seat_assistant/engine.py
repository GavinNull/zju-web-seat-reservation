"""Single-account reservation execution engine."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol

from .domain import (
    ReservationConfig,
    ReservationOutcome,
    TaskState,
    choose_seat,
)
from .storage import Repository


@dataclass(frozen=True)
class ScanResult:
    available_seats: tuple[int, ...]
    message: str = ""


@dataclass(frozen=True)
class ExecutionResult:
    outcome: ReservationOutcome
    seat: int | None = None
    message: str = ""


class BrowserAdapter(Protocol):
    def check_login(self) -> bool: ...
    def scan(self, config: ReservationConfig) -> ScanResult: ...
    def submit(
        self, config: ReservationConfig, seat: int
    ) -> ReservationOutcome: ...
    def verify_current_reservation(
        self, config: ReservationConfig, seat: int
    ) -> bool: ...


class ExecutionEngine:
    def __init__(
        self,
        repository: Repository,
        adapter: BrowserAdapter,
        submission_enabled: bool = False,
    ):
        self.repository = repository
        self.adapter = adapter
        self.submission_enabled = submission_enabled
        self._account_lock = threading.Lock()

    def run_once(self, task_id: str) -> ExecutionResult:
        if not self._account_lock.acquire(blocking=False):
            return ExecutionResult(
                ReservationOutcome.FAILURE,
                message="another task is using the account",
            )
        run_id: str | None = None
        try:
            task = self.repository.get_task(task_id)
            if task.state in {
                TaskState.DRAFT,
                TaskState.STOPPED,
                TaskState.FAILED,
            }:
                self.repository.set_task_state(task_id, TaskState.SCHEDULED)
            self.repository.set_task_state(task_id, TaskState.RUNNING)
            run_id = self.repository.start_run(task_id)
            self._event(
                task_id,
                "run_started",
                "Started detection",
                {
                    "venue": task.config.venue,
                    "floor": task.config.floor,
                    "area": task.config.area,
                },
            )

            self._event(task_id, "checking_login", "Checking login status")
            if not self.adapter.check_login():
                self._event(task_id, "login_required", "Login is required")
                return self._finish(
                    task_id, run_id, ReservationOutcome.LOGIN_REQUIRED
                )

            self._event(task_id, "scanning", "Scanning seats")
            scan = self.adapter.scan(task.config)
            self._event(
                task_id,
                "scan_complete",
                "Scan complete",
                {
                    "available_count": len(scan.available_seats),
                    "available_seats": list(scan.available_seats[:30]),
                    "message": scan.message,
                },
            )
            seat = choose_seat(scan.available_seats, task.config.seat_rules)
            if seat is None:
                self._event(
                    task_id,
                    "no_matching_seat",
                    "No matching seat found",
                    {"available_count": len(scan.available_seats)},
                )
                return self._finish(
                    task_id, run_id, ReservationOutcome.NO_SEAT, message=scan.message
                )

            self._event(
                task_id,
                "candidate_found",
                "Candidate seat found",
                {"seat": seat},
            )
            if task.config.observation_mode or not self.submission_enabled:
                self._event(
                    task_id,
                    "submission_skipped",
                    "Submission skipped by current settings",
                    {
                        "seat": seat,
                        "observation_mode": task.config.observation_mode,
                        "submission_enabled": self.submission_enabled,
                    },
                )
                return self._finish(
                    task_id,
                    run_id,
                    ReservationOutcome.CANDIDATE_FOUND,
                    seat=seat,
                    message="candidate found; submission is disabled",
                )

            self.repository.set_task_state(task_id, TaskState.SUBMITTING)
            self._event(task_id, "submitting", "Submitting reservation", {"seat": seat})
            outcome = self.adapter.submit(task.config, seat)
            self.repository.set_task_state(task_id, TaskState.VERIFYING)
            self._event(task_id, "verifying", "Verifying reservation", {"seat": seat})
            if outcome is ReservationOutcome.SUCCESS:
                verified = self.adapter.verify_current_reservation(
                    task.config, seat
                )
                if not verified:
                    outcome = ReservationOutcome.AMBIGUOUS
                else:
                    self.repository.record_reservation(
                        task_id, run_id, str(seat), verified=True
                    )
            self._event(
                task_id,
                "finished",
                "Detection finished",
                {"outcome": outcome.value, "seat": seat},
            )
            return self._finish(task_id, run_id, outcome, seat=seat)
        except Exception as error:
            self._event(
                task_id,
                "failed",
                "Detection failed",
                {"error": str(error)},
            )
            if run_id is not None:
                self.repository.finish_run(
                    run_id, ReservationOutcome.FAILURE.value, str(error)
                )
            try:
                self.repository.set_task_state(
                    task_id, TaskState.FAILED, str(error)
                )
            except (KeyError, ValueError):
                pass
            return ExecutionResult(
                ReservationOutcome.FAILURE, message=str(error)
            )
        finally:
            self._account_lock.release()

    def _event(
        self,
        task_id: str,
        stage: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        try:
            self.repository.add_task_event(task_id, stage, message, details)
        except Exception:
            return

    def _finish(
        self,
        task_id: str,
        run_id: str,
        outcome: ReservationOutcome,
        seat: int | None = None,
        message: str = "",
    ) -> ExecutionResult:
        self.repository.finish_run(
            run_id,
            outcome.value,
            message or None,
            seat_number=seat,
        )
        if self.repository.get_task(task_id).state is not TaskState.STOPPED:
            self.repository.set_task_state(task_id, outcome.task_state)
        return ExecutionResult(outcome, seat=seat, message=message)
