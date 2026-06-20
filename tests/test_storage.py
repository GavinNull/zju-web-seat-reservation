import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from seat_assistant.domain import ReservationConfig, SeatRule, TaskState
from seat_assistant.storage import Repository


def task_config(name: str = "Morning") -> ReservationConfig:
    return ReservationConfig(
        name=name,
        venue="基础馆",
        floor="负一层",
        area="负一层书库",
        reservation_date=date(2026, 6, 15),
        time_slot="08:00-22:00",
        starts_at=datetime(2026, 6, 14, 7, 59, 50),
        stops_at=datetime(2026, 6, 14, 8, 10),
        seat_rules=(SeatRule(priority=1, start=80, end=100),),
    )


class RepositoryTests(unittest.TestCase):
    def test_crud_and_state_survive_repository_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "assistant.db"
            repository = Repository(path)
            task_id = repository.create_task(task_config())
            repository.set_task_state(task_id, TaskState.SCHEDULED)

            reopened = Repository(path)
            stored = reopened.get_task(task_id)
            self.assertEqual(stored.config.name, "Morning")
            self.assertEqual(stored.state, TaskState.SCHEDULED)
            self.assertEqual(stored.config.seat_rules[0].start, 80)

            reopened.update_task(task_id, task_config("Updated"))
            self.assertEqual(reopened.get_task(task_id).config.name, "Updated")
            self.assertEqual(len(reopened.list_tasks()), 1)

            reopened.delete_task(task_id)
            self.assertEqual(reopened.list_tasks(), [])
            reopened.close()
            repository.close()

    def test_records_runs_reservations_and_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "assistant.db")
            task_id = repository.create_task(task_config())
            run_id = repository.start_run(task_id)
            repository.finish_run(run_id, "succeeded", seat_number=95)
            repository.record_reservation(
                task_id=task_id,
                run_id=run_id,
                seat_number="095",
                verified=True,
            )
            repository.set_setting("account_status", "connected")

            self.assertEqual(repository.get_setting("account_status"), "connected")
            self.assertEqual(repository.list_reservations()[0]["seat_number"], "095")
            latest = repository.get_latest_run(task_id)
            self.assertEqual(latest["result"], "succeeded")
            self.assertEqual(latest["seat_number"], 95)
            self.assertIsNotNone(latest["finished_at"])
            repository.close()

    def test_marks_unfinished_runs_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "assistant.db")
            try:
                task_id = repository.create_task(task_config())
                repository.start_run(task_id)

                count = repository.finish_unfinished_runs(
                    "interrupted by service restart"
                )

                latest = repository.get_latest_run(task_id)
                self.assertEqual(count, 1)
                self.assertEqual(latest["result"], "failure")
                self.assertEqual(
                    latest["last_error"], "interrupted by service restart"
                )
                self.assertIsNotNone(latest["finished_at"])
            finally:
                repository.close()

    def test_records_recent_task_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "assistant.db")
            try:
                task_id = repository.create_task(task_config())

                repository.add_task_event(task_id, "checking_login", "Checking login")
                repository.add_task_event(
                    task_id,
                    "scan_complete",
                    "Found available seats",
                    {"available_count": 2, "candidates": [12, 95]},
                )

                events = repository.list_task_events(task_id)
                self.assertEqual([event["stage"] for event in events], [
                    "scan_complete",
                    "checking_login",
                ])
                self.assertEqual(events[0]["message"], "Found available seats")
                self.assertEqual(events[0]["details"]["available_count"], 2)
                self.assertEqual(events[0]["details"]["candidates"], [12, 95])
            finally:
                repository.close()

    def test_counts_task_events_by_stage_beyond_recent_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "assistant.db")
            try:
                task_id = repository.create_task(task_config())
                for index in range(25):
                    repository.add_task_event(
                        task_id,
                        "scan_complete",
                        "Scan complete",
                        {"index": index},
                    )
                repository.add_task_event(task_id, "checking_login", "Checking login")

                self.assertEqual(
                    repository.count_task_events(task_id, "scan_complete"),
                    25,
                )
                self.assertEqual(
                    repository.count_task_events(task_id, "checking_login"),
                    1,
                )
                self.assertEqual(len(repository.list_task_events(task_id)), 20)
            finally:
                repository.close()


if __name__ == "__main__":
    unittest.main()
