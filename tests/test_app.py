import threading
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


from fastapi.testclient import TestClient
from seat_assistant.app import create_app, _login_worker, _resume_waiting_login_tasks
from seat_assistant.app import LOGIN_POLL_SECONDS, LOGIN_TIMEOUT_SECONDS
from seat_assistant.domain import (
    ReservationConfig,
    ReservationOutcome,
    SeatRule,
    TaskState,
)
from seat_assistant.storage import Repository


TASK = {
    "name": "Morning",
    "venue": "基础馆",
    "floor": "负一层",
    "area": "负一层书库",
    "reservation_date": "2026-06-15",
    "time_slot": "08:00-22:00",
    "starts_at": "2026-06-14T07:59:50+08:00",
    "stops_at": "2026-06-14T08:10:00+08:00",
    "refresh_min_seconds": 8,
    "refresh_max_seconds": 15,
    "max_consecutive_errors": 5,
    "observation_mode": True,
    "seat_rules": [
        {
            "priority": 1,
            "start": 80,
            "end": 100,
            "included": [12],
            "excluded": [91],
            "order": "asc",
        }
    ],
}


def _test_config() -> ReservationConfig:
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
    )


class AppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        app = create_app(
            data_directory=Path(self.directory.name),
            access_token="test-token",
            enable_scheduler=False,
        )
        self.client = TestClient(app)
        self.client.__enter__()
        self.headers = {"Authorization": "Bearer test-token"}

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def test_health_is_public_but_dashboard_requires_token(self) -> None:
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        self.assertEqual(self.client.get("/api/dashboard").status_code, 401)
        response = self.client.get("/api/dashboard", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["tasks"], [])

    def test_page_assets_disable_browser_cache(self) -> None:
        page = self.client.get("/")
        script = self.client.get("/static/app.js")

        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertEqual(script.headers["cache-control"], "no-store")

    def test_task_crud_and_start_stop(self) -> None:
        created = self.client.post(
            "/api/tasks", json=TASK, headers=self.headers
        )
        self.assertEqual(created.status_code, 201, created.text)
        task_id = created.json()["id"]

        started = self.client.post(
            f"/api/tasks/{task_id}/start", headers=self.headers
        )
        self.assertEqual(started.json()["state"], "scheduled")

        stopped = self.client.post(
            f"/api/tasks/{task_id}/stop", headers=self.headers
        )
        self.assertEqual(stopped.json()["state"], "stopped")

        deleted = self.client.delete(
            f"/api/tasks/{task_id}", headers=self.headers
        )
        self.assertEqual(deleted.status_code, 204)

    def test_start_reactivates_timed_out_task(self) -> None:
        created = self.client.post(
            "/api/tasks", json=TASK, headers=self.headers
        )
        task_id = created.json()["id"]
        repository = self.client.app.state.repository
        repository.set_task_state(task_id, TaskState.SCHEDULED)
        repository.set_task_state(task_id, TaskState.TIMED_OUT)

        started = self.client.post(
            f"/api/tasks/{task_id}/start", headers=self.headers
        )

        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(started.json()["state"], "scheduled")

    def test_dashboard_includes_detection_status(self) -> None:
        created = self.client.post(
            "/api/tasks", json=TASK, headers=self.headers
        )
        task = created.json()
        self.assertIn("detection", task)
        self.assertIsNone(task["detection"]["last_run"])
        self.assertIsNone(task["detection"]["next_check_at"])

    def test_app_startup_marks_interrupted_runs_finished(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            repository = Repository(path / "assistant.db")
            task_id = repository.create_task(_test_config())
            repository.start_run(task_id)
            repository.close()

            app = create_app(
                data_directory=path,
                access_token="test-token",
                enable_scheduler=False,
            )
            with TestClient(app) as client:
                dashboard = client.get(
                    "/api/dashboard",
                    headers={"Authorization": "Bearer test-token"},
                )

            latest = dashboard.json()["tasks"][0]["detection"]["last_run"]
            self.assertEqual(latest["result"], "failure")
            self.assertEqual(latest["last_error"], "interrupted by service restart")
            self.assertIsNotNone(latest["finished_at"])

    def test_task_payload_explains_submission_policy_from_ui_choices(self) -> None:
        direct_task = {**TASK, "observation_mode": False}
        created = self.client.post(
            "/api/tasks", json=direct_task, headers=self.headers
        )
        self.assertEqual(created.status_code, 201, created.text)

        disabled_policy = created.json()["submission_policy"]
        self.assertFalse(disabled_policy["will_submit"])
        self.assertIn("自动提交总开关未开启", disabled_policy["blockers"])

        enabled = self.client.put(
            "/api/settings/system",
            json={
                "submission_enabled": True,
                "confirmation": "确认自动预约",
            },
            headers=self.headers,
        )
        self.assertEqual(enabled.status_code, 200)

        dashboard = self.client.get("/api/dashboard", headers=self.headers)
        policy = dashboard.json()["tasks"][0]["submission_policy"]
        self.assertTrue(policy["will_submit"])
        self.assertEqual(policy["blockers"], [])

    def test_run_once_uses_background_browser(self) -> None:
        created = self.client.post(
            "/api/tasks", json=TASK, headers=self.headers
        )
        task_id = created.json()["id"]
        result = SimpleNamespace(
            outcome=ReservationOutcome.CANDIDATE_FOUND,
            seat=None,
            message="ok",
        )

        with patch("seat_assistant.app.PlaywrightAdapter") as adapter_class, patch(
            "seat_assistant.app.ExecutionEngine"
        ) as engine_class:
            engine_class.return_value.run_once.return_value = result
            response = self.client.post(
                f"/api/tasks/{task_id}/run-once", headers=self.headers
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(adapter_class.call_args.kwargs["headless"])
        self.assertTrue(adapter_class.call_args.kwargs["background_window"])

    def test_connected_account_marks_not_connected_without_visible_retry(self) -> None:
        created = self.client.post(
            "/api/tasks", json=TASK, headers=self.headers
        )
        task_id = created.json()["id"]
        self.client.app.state.repository.set_setting(
            "account_status", "connected"
        )
        result = SimpleNamespace(
            outcome=ReservationOutcome.LOGIN_REQUIRED,
            seat=None,
            message="login required",
        )

        with patch("seat_assistant.app.PlaywrightAdapter") as adapter_class, patch(
            "seat_assistant.app.ExecutionEngine"
        ) as engine_class:
            engine_class.return_value.run_once.return_value = result
            response = self.client.post(
                f"/api/tasks/{task_id}/run-once", headers=self.headers
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(engine_class.return_value.run_once.call_count, 1)
        self.assertEqual(
            [call.kwargs["headless"] for call in adapter_class.call_args_list],
            [False],
        )
        self.assertEqual(
            [call.kwargs["background_window"] for call in adapter_class.call_args_list],
            [True],
        )
        dashboard = self.client.get("/api/dashboard", headers=self.headers)
        self.assertEqual(dashboard.json()["account_status"], "not_connected")

    def test_reservation_detection_never_opens_visible_retry_browser(self) -> None:
        created = self.client.post(
            "/api/tasks", json=TASK, headers=self.headers
        )
        task_id = created.json()["id"]
        self.client.app.state.repository.set_setting(
            "account_status", "connected"
        )
        result = SimpleNamespace(
            outcome=ReservationOutcome.LOGIN_REQUIRED,
            seat=None,
            message="login required",
        )

        with patch("seat_assistant.app.PlaywrightAdapter") as adapter_class, patch(
            "seat_assistant.app.ExecutionEngine"
        ) as engine_class:
            engine_class.return_value.run_once.return_value = result
            response = self.client.post(
                f"/api/tasks/{task_id}/run-once", headers=self.headers
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(engine_class.return_value.run_once.call_count, 1)
        self.assertEqual(
            [call.kwargs["headless"] for call in adapter_class.call_args_list],
            [False],
        )
        self.assertEqual(
            [call.kwargs["background_window"] for call in adapter_class.call_args_list],
            [True],
        )

    def test_run_task_skips_when_another_detection_is_active(self) -> None:
        class BlockingAdapter:
            started = threading.Event()
            release = threading.Event()

            def close(self) -> None:
                pass

            def check_login(self) -> bool:
                self.started.set()
                self.release.wait(timeout=5)
                return False

            def scan(self, config):
                raise AssertionError("scan should not run")

            def submit(self, config, seat):
                raise AssertionError("submit should not run")

            def verify_current_reservation(self, config, seat):
                raise AssertionError("verify should not run")

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                data_directory=Path(directory),
                access_token="test-token",
                enable_scheduler=False,
                adapter_factory=BlockingAdapter,
            )
            with TestClient(app) as client:
                headers = {"Authorization": "Bearer test-token"}
                created = client.post(
                    "/api/tasks", json=TASK, headers=headers
                )
                task_id = created.json()["id"]
                first_result = {}
                first = threading.Thread(
                    target=lambda: first_result.update(app.state.run_task(task_id))
                )
                first.start()
                self.assertTrue(BlockingAdapter.started.wait(timeout=2))

                second = app.state.run_task(task_id)

                BlockingAdapter.release.set()
                first.join(timeout=5)

        self.assertEqual(second["outcome"], "in_progress")
        self.assertEqual(first_result["outcome"], "login_required")

    def test_account_login_uses_visible_browser(self) -> None:
        with patch("seat_assistant.app._login_worker") as worker, patch(
            "seat_assistant.app.PlaywrightAdapter"
        ) as adapter_class:
            response = self.client.post(
                "/api/account/login", headers=self.headers
            )
            adapter_factory = worker.call_args.args[1]
            adapter_factory()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(adapter_class.call_args.kwargs["headless"])
        self.assertFalse(adapter_class.call_args.kwargs["background_window"])

    def test_invalid_task_returns_validation_message(self) -> None:
        invalid = {**TASK, "refresh_min_seconds": 1}
        response = self.client.post(
            "/api/tasks", json=invalid, headers=self.headers
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("at least 8 seconds", response.text)

    def test_smtp_settings_apis_are_removed(self) -> None:
        self.assertEqual(
            self.client.get("/api/settings/smtp", headers=self.headers).status_code,
            404,
        )
        self.assertEqual(
            self.client.put(
                "/api/settings/smtp", json={}, headers=self.headers
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                "/api/settings/smtp/test", headers=self.headers
            ).status_code,
            404,
        )

    def test_run_once_sends_desktop_notification_on_success(self) -> None:
        class FakeNotifier:
            def __init__(self) -> None:
                self.successes = []

            def notify_success(self, task, seat: int | None) -> None:
                self.successes.append((task.config.name, seat))

            def notify_timeout(self, task) -> None:
                raise AssertionError("timeout notification was not expected")

            def notify_error(self, task, message: str) -> None:
                raise AssertionError("error notification was not expected")

        class FakeAdapter:
            def close(self) -> None:
                pass

            def check_login(self) -> bool:
                return True

            def scan(self, config):
                from seat_assistant.engine import ScanResult

                return ScanResult((12,))

            def submit(self, config, seat):
                return ReservationOutcome.SUCCESS

            def verify_current_reservation(self, config, seat):
                return True

        notifier = FakeNotifier()
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                data_directory=Path(directory),
                access_token="test-token",
                enable_scheduler=False,
                adapter_factory=FakeAdapter,
                desktop_notifier=notifier,
            )
            with TestClient(app) as client:
                headers = {"Authorization": "Bearer test-token"}
                client.put(
                    "/api/settings/system",
                    json={
                        "submission_enabled": True,
                        "confirmation": "确认自动预约",
                    },
                    headers=headers,
                )
                created = client.post(
                    "/api/tasks",
                    json={**TASK, "observation_mode": False},
                    headers=headers,
                )
                task_id = created.json()["id"]
                response = client.post(
                    f"/api/tasks/{task_id}/run-once", headers=headers
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(notifier.successes, [("Morning", 12)])

    def test_submission_setting_requires_exact_confirmation_phrase(self) -> None:
        initial = self.client.get(
            "/api/settings/system", headers=self.headers
        )
        self.assertFalse(initial.json()["submission_enabled"])

        rejected = self.client.put(
            "/api/settings/system",
            json={
                "submission_enabled": True,
                "confirmation": "我确认",
            },
            headers=self.headers,
        )
        self.assertEqual(rejected.status_code, 422)

        enabled = self.client.put(
            "/api/settings/system",
            json={
                "submission_enabled": True,
                "confirmation": "确认自动预约",
            },
            headers=self.headers,
        )
        self.assertTrue(enabled.json()["submission_enabled"])
        dashboard = self.client.get(
            "/api/dashboard", headers=self.headers
        )
        self.assertTrue(dashboard.json()["submission_enabled"])

        disabled = self.client.put(
            "/api/settings/system",
            json={"submission_enabled": False},
            headers=self.headers,
        )
        self.assertFalse(disabled.json()["submission_enabled"])

    def test_task_preserves_multiple_complete_seat_rules(self) -> None:
        task = {
            **TASK,
            "notify_success": False,
            "notify_timeout": True,
            "notify_error": False,
            "seat_rules": [
                {
                    "priority": 1,
                    "start": 80,
                    "end": 100,
                    "included": [12, 15],
                    "excluded": [91],
                    "accept_any": False,
                    "order": "desc",
                },
                {
                    "priority": 2,
                    "start": None,
                    "end": None,
                    "included": [],
                    "excluded": [1, 2],
                    "accept_any": True,
                    "order": "random",
                },
            ],
        }
        response = self.client.post(
            "/api/tasks", json=task, headers=self.headers
        )
        self.assertEqual(response.status_code, 201, response.text)
        config = response.json()["config"]
        self.assertEqual(len(config["seat_rules"]), 2)
        self.assertEqual(config["seat_rules"][0]["included"], [12, 15])
        self.assertTrue(config["seat_rules"][1]["accept_any"])
        self.assertFalse(config["notify_success"])

    def test_login_poll_uses_twenty_second_interval(self) -> None:
        self.assertEqual(LOGIN_POLL_SECONDS, 20)
        self.assertEqual(LOGIN_TIMEOUT_SECONDS, 600)

    def test_successful_login_resumes_waiting_login_tasks(self) -> None:
        created = self.client.post(
            "/api/tasks", json=TASK, headers=self.headers
        )
        task_id = created.json()["id"]
        repository = self.client.app.state.repository
        repository.set_task_state(task_id, TaskState.SCHEDULED)
        repository.set_task_state(task_id, TaskState.WAITING_LOGIN)

        _resume_waiting_login_tasks(repository)

        self.assertEqual(
            repository.get_task(task_id).state, TaskState.SCHEDULED
        )

    def test_login_worker_records_error_details(self) -> None:
        class BrokenAdapter:
            def open_for_login(self) -> None:
                raise RuntimeError("profile is already in use")

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "assistant.db")
            try:
                _login_worker(repository, lambda: BrokenAdapter())

                self.assertIn(
                    "profile is already in use",
                    repository.get_setting("account_status"),
                )
            finally:
                repository.close()

if __name__ == "__main__":
    unittest.main()
