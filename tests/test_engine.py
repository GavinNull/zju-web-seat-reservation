import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from seat_assistant.domain import (
    ReservationConfig,
    ReservationOutcome,
    SeatRule,
    TaskState,
)
from seat_assistant.engine import ExecutionEngine, ScanResult
from seat_assistant.storage import Repository


class FakeAdapter:
    def __init__(self, outcome: ReservationOutcome, seats: tuple[int, ...] = (95,)):
        self.outcome = outcome
        self.seats = seats
        self.submitted = False

    def check_login(self) -> bool:
        return True

    def scan(self, _config: ReservationConfig) -> ScanResult:
        return ScanResult(available_seats=self.seats)

    def submit(self, _config: ReservationConfig, _seat: int) -> ReservationOutcome:
        self.submitted = True
        return self.outcome

    def verify_current_reservation(
        self, _config: ReservationConfig, _seat: int
    ) -> bool:
        return self.outcome is ReservationOutcome.SUCCESS


class StopDuringScanAdapter(FakeAdapter):
    def __init__(self, repository: Repository, task_id: str):
        super().__init__(ReservationOutcome.SUCCESS, seats=())
        self.repository = repository
        self.task_id = task_id

    def scan(self, _config: ReservationConfig) -> ScanResult:
        self.repository.set_task_state(self.task_id, TaskState.STOPPED)
        return ScanResult(available_seats=())


def config(observation_mode: bool) -> ReservationConfig:
    return ReservationConfig(
        name="Morning",
        venue="基础馆",
        floor="负一层",
        area="负一层书库",
        reservation_date=date(2026, 6, 15),
        time_slot="08:00-22:00",
        starts_at=datetime(2026, 6, 14, 7, 59, 50),
        stops_at=datetime(2026, 6, 14, 8, 10),
        seat_rules=(SeatRule(priority=1, start=80, end=100),),
        observation_mode=observation_mode,
    )


class ExecutionEngineTests(unittest.TestCase):
    def test_observation_mode_never_submits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "assistant.db")
            task_id = repository.create_task(config(observation_mode=True))
            adapter = FakeAdapter(ReservationOutcome.SUCCESS)

            result = ExecutionEngine(repository, adapter).run_once(task_id)

            self.assertEqual(result.outcome, ReservationOutcome.CANDIDATE_FOUND)
            self.assertFalse(adapter.submitted)
            self.assertEqual(repository.get_task(task_id).state, TaskState.RUNNING)
            repository.close()

    def test_success_requires_double_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "assistant.db")
            task_id = repository.create_task(config(observation_mode=False))
            adapter = FakeAdapter(ReservationOutcome.SUCCESS)

            result = ExecutionEngine(
                repository, adapter, submission_enabled=True
            ).run_once(task_id)

            self.assertEqual(result.outcome, ReservationOutcome.SUCCESS)
            self.assertEqual(repository.get_task(task_id).state, TaskState.SUCCEEDED)
            self.assertEqual(repository.list_reservations()[0]["seat_number"], "95")
            repository.close()

    def test_submission_remains_disabled_without_global_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "assistant.db")
            task_id = repository.create_task(config(observation_mode=False))
            adapter = FakeAdapter(ReservationOutcome.SUCCESS)

            result = ExecutionEngine(repository, adapter).run_once(task_id)

            self.assertEqual(result.outcome, ReservationOutcome.CANDIDATE_FOUND)
            self.assertFalse(adapter.submitted)
            repository.close()

    def test_stopped_task_state_is_not_overwritten_by_finish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "assistant.db")
            task_id = repository.create_task(config(observation_mode=True))
            adapter = StopDuringScanAdapter(repository, task_id)

            result = ExecutionEngine(repository, adapter).run_once(task_id)

            self.assertEqual(result.outcome, ReservationOutcome.NO_SEAT)
            self.assertEqual(repository.get_task(task_id).state, TaskState.STOPPED)
            repository.close()


if __name__ == "__main__":
    unittest.main()
