import threading
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
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

    def test_different_tasks_can_run_at_the_same_time(self) -> None:
        class BlockingAdapter:
            started = []
            release = threading.Event()
            lock = threading.Lock()

            def close(self) -> None:
                pass

            def check_login(self) -> bool:
                with self.lock:
                    self.started.append(threading.get_ident())
                self.release.wait(timeout=5)
                return True

            def scan(self, config):
                from seat_assistant.engine import ScanResult

                return ScanResult(())

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
                now = datetime.now().astimezone()
                first = client.post(
                    "/api/tasks",
                    json={
                        **TASK,
                        "name": "One",
                        "area": "负一层书库",
                        "starts_at": now.isoformat(),
                        "stops_at": (now + timedelta(minutes=10)).isoformat(),
                    },
                    headers=headers,
                )
                second = client.post(
                    "/api/tasks",
                    json={
                        **TASK,
                        "name": "Two",
                        "area": "负一层东",
                        "starts_at": now.isoformat(),
                        "stops_at": (now + timedelta(minutes=10)).isoformat(),
                    },
                    headers=headers,
                )
                results = []
                first_thread = threading.Thread(
                    target=lambda: results.append(app.state.run_task(first.json()["id"]))
                )
                second_thread = threading.Thread(
                    target=lambda: results.append(app.state.run_task(second.json()["id"]))
                )

                first_thread.start()
                second_thread.start()
                deadline = time.time() + 2
                while time.time() < deadline and len(BlockingAdapter.started) < 2:
                    time.sleep(0.01)
                BlockingAdapter.release.set()
                first_thread.join(timeout=5)
                second_thread.join(timeout=5)

        self.assertEqual(len(BlockingAdapter.started), 2)
        self.assertEqual(
            sorted(item["outcome"] for item in results), ["no_seat", "no_seat"]
        )

    def test_run_task_reuses_reservation_adapter_between_scans(self) -> None:
        class ReusableAdapter:
            instances = []

            def __init__(self) -> None:
                self.close_count = 0
                self.scan_count = 0
                self.instances.append(self)

            def close(self) -> None:
                self.close_count += 1

            def check_login(self) -> bool:
                return True

            def scan(self, config):
                from seat_assistant.engine import ScanResult

                self.scan_count += 1
                return ScanResult(())

            def submit(self, config, seat):
                raise AssertionError("submit should not run")

            def verify_current_reservation(self, config, seat):
                raise AssertionError("verify should not run")

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                data_directory=Path(directory),
                access_token="test-token",
                enable_scheduler=False,
                adapter_factory=ReusableAdapter,
            )
            with TestClient(app) as client:
                headers = {"Authorization": "Bearer test-token"}
                now = datetime.now().astimezone()
                created = client.post(
                    "/api/tasks",
                    json={
                        **TASK,
                        "starts_at": now.isoformat(),
                        "stops_at": (now + timedelta(minutes=10)).isoformat(),
                    },
                    headers=headers,
                )
                task_id = created.json()["id"]

                first = app.state.run_task(task_id)
                second = app.state.run_task(task_id)

                adapter = ReusableAdapter.instances[0]

        self.assertEqual(first["outcome"], "no_seat")
        self.assertEqual(second["outcome"], "no_seat")
        self.assertEqual(len(ReusableAdapter.instances), 1)
        self.assertEqual(adapter.scan_count, 2)
        self.assertEqual(adapter.close_count, 1)

    def test_run_task_uses_independent_adapter_per_task(self) -> None:
        class AreaAdapter:
            instances = []

            def __init__(self) -> None:
                self.close_count = 0
                self.scanned_areas = []
                self.instances.append(self)

            def close(self) -> None:
                self.close_count += 1

            def check_login(self) -> bool:
                return True

            def scan(self, config):
                from seat_assistant.engine import ScanResult

                self.scanned_areas.append(config.area)
                return ScanResult(())

            def submit(self, config, seat):
                raise AssertionError("submit should not run")

            def verify_current_reservation(self, config, seat):
                raise AssertionError("verify should not run")

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                data_directory=Path(directory),
                access_token="test-token",
                enable_scheduler=False,
                adapter_factory=AreaAdapter,
            )
            with TestClient(app) as client:
                headers = {"Authorization": "Bearer test-token"}
                now = datetime.now().astimezone()
                first = client.post(
                    "/api/tasks",
                    json={
                        **TASK,
                        "name": "Area A",
                        "area": "负一层书库",
                        "starts_at": now.isoformat(),
                        "stops_at": (now + timedelta(minutes=10)).isoformat(),
                    },
                    headers=headers,
                )
                second = client.post(
                    "/api/tasks",
                    json={
                        **TASK,
                        "name": "Area B",
                        "area": "负一层东",
                        "starts_at": now.isoformat(),
                        "stops_at": (now + timedelta(minutes=10)).isoformat(),
                    },
                    headers=headers,
                )

                app.state.run_task(first.json()["id"])
                app.state.run_task(second.json()["id"])

                first_adapter = AreaAdapter.instances[0]
                second_adapter = AreaAdapter.instances[1]

        self.assertEqual(len(AreaAdapter.instances), 2)
        self.assertEqual(first_adapter.scanned_areas, ["负一层书库"])
        self.assertEqual(second_adapter.scanned_areas, ["负一层东"])
        self.assertEqual(first_adapter.close_count, 1)
        self.assertEqual(second_adapter.close_count, 1)

    def test_run_task_uses_independent_browser_profile_per_task(self) -> None:
        now = datetime.now().astimezone()
        result = SimpleNamespace(
            outcome=ReservationOutcome.NO_SEAT,
            seat=None,
            message="",
        )
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                data_directory=Path(directory),
                access_token="test-token",
                enable_scheduler=False,
            )
            with TestClient(app) as client, patch(
                "seat_assistant.app.PlaywrightAdapter"
            ) as adapter_class, patch(
                "seat_assistant.app.ExecutionEngine"
            ) as engine_class:
                engine_class.return_value.run_once.return_value = result
                headers = {"Authorization": "Bearer test-token"}
                first = client.post(
                    "/api/tasks",
                    json={
                        **TASK,
                        "name": "Area A",
                        "area": "负一层书库",
                        "starts_at": now.isoformat(),
                        "stops_at": (now + timedelta(minutes=10)).isoformat(),
                    },
                    headers=headers,
                )
                second = client.post(
                    "/api/tasks",
                    json={
                        **TASK,
                        "name": "Area B",
                        "area": "负一层东",
                        "starts_at": now.isoformat(),
                        "stops_at": (now + timedelta(minutes=10)).isoformat(),
                    },
                    headers=headers,
                )

                app.state.run_task(first.json()["id"])
                app.state.run_task(second.json()["id"])

                profile_directories = [
                    call.kwargs["profile_directory"]
                    for call in adapter_class.call_args_list
                ]

        self.assertEqual(len(profile_directories), 2)
        self.assertNotEqual(profile_directories[0], profile_directories[1])
        self.assertTrue(
            all("reservation-profiles" in str(path) for path in profile_directories)
        )

    def test_stop_task_closes_reservation_adapter(self) -> None:
        class ClosableAdapter:
            instances = []

            def __init__(self) -> None:
                self.close_count = 0
                self.instances.append(self)

            def close(self) -> None:
                self.close_count += 1

            def check_login(self) -> bool:
                return True

            def scan(self, config):
                from seat_assistant.engine import ScanResult

                return ScanResult(())

            def submit(self, config, seat):
                raise AssertionError("submit should not run")

            def verify_current_reservation(self, config, seat):
                raise AssertionError("verify should not run")

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                data_directory=Path(directory),
                access_token="test-token",
                enable_scheduler=False,
                adapter_factory=ClosableAdapter,
            )
            with TestClient(app) as client:
                headers = {"Authorization": "Bearer test-token"}
                now = datetime.now().astimezone()
                created = client.post(
                    "/api/tasks",
                    json={
                        **TASK,
                        "starts_at": now.isoformat(),
                        "stops_at": (now + timedelta(minutes=10)).isoformat(),
                    },
                    headers=headers,
                )
                task_id = created.json()["id"]
                app.state.run_task(task_id)
                adapter = ClosableAdapter.instances[0]

                stopped = client.post(
                    f"/api/tasks/{task_id}/stop", headers=headers
                )

                self.assertEqual(stopped.json()["state"], "stopped")
                self.assertEqual(adapter.close_count, 1)

    def test_delete_task_closes_reservation_adapter(self) -> None:
        class ClosableAdapter:
            instances = []

            def __init__(self) -> None:
                self.close_count = 0
                self.instances.append(self)

            def close(self) -> None:
                self.close_count += 1

            def check_login(self) -> bool:
                return True

            def scan(self, config):
                from seat_assistant.engine import ScanResult

                return ScanResult(())

            def submit(self, config, seat):
                raise AssertionError("submit should not run")

            def verify_current_reservation(self, config, seat):
                raise AssertionError("verify should not run")

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                data_directory=Path(directory),
                access_token="test-token",
                enable_scheduler=False,
                adapter_factory=ClosableAdapter,
            )
            with TestClient(app) as client:
                headers = {"Authorization": "Bearer test-token"}
                now = datetime.now().astimezone()
                created = client.post(
                    "/api/tasks",
                    json={
                        **TASK,
                        "starts_at": now.isoformat(),
                        "stops_at": (now + timedelta(minutes=10)).isoformat(),
                    },
                    headers=headers,
                )
                task_id = created.json()["id"]
                app.state.run_task(task_id)
                adapter = ClosableAdapter.instances[0]

                deleted = client.delete(
                    f"/api/tasks/{task_id}", headers=headers
                )

                self.assertEqual(deleted.status_code, 204)
                self.assertEqual(adapter.close_count, 1)

    def test_reservation_adapter_is_used_from_one_worker_thread(self) -> None:
        class ThreadBoundAdapter:
            instances = []

            def __init__(self) -> None:
                self.close_count = 0
                self.scan_count = 0
                self.worker_thread_ids = []
                self.instances.append(self)

            def close(self) -> None:
                self.close_count += 1

            def _record_thread(self) -> None:
                thread_id = threading.get_ident()
                if self.worker_thread_ids and self.worker_thread_ids[0] != thread_id:
                    raise RuntimeError("adapter used from a different thread")
                self.worker_thread_ids.append(thread_id)

            def check_login(self) -> bool:
                self._record_thread()
                return True

            def scan(self, config):
                from seat_assistant.engine import ScanResult

                self._record_thread()
                self.scan_count += 1
                return ScanResult(())

            def submit(self, config, seat):
                raise AssertionError("submit should not run")

            def verify_current_reservation(self, config, seat):
                raise AssertionError("verify should not run")

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                data_directory=Path(directory),
                access_token="test-token",
                enable_scheduler=False,
                adapter_factory=ThreadBoundAdapter,
            )
            with TestClient(app) as client:
                headers = {"Authorization": "Bearer test-token"}
                now = datetime.now().astimezone()
                created = client.post(
                    "/api/tasks",
                    json={
                        **TASK,
                        "starts_at": now.isoformat(),
                        "stops_at": (now + timedelta(minutes=10)).isoformat(),
                    },
                    headers=headers,
                )
                task_id = created.json()["id"]
                results = []

                for _index in range(2):
                    thread = threading.Thread(
                        target=lambda: results.append(app.state.run_task(task_id))
                    )
                    thread.start()
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())

                adapter = ThreadBoundAdapter.instances[0]

        self.assertEqual([item["outcome"] for item in results], ["no_seat", "no_seat"])
        self.assertEqual(len(ThreadBoundAdapter.instances), 1)
        self.assertEqual(adapter.scan_count, 2)
        self.assertEqual(len(set(adapter.worker_thread_ids)), 1)
        self.assertEqual(adapter.close_count, 1)

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
        invalid = {**TASK, "refresh_min_seconds": 2}
        response = self.client.post(
            "/api/tasks", json=invalid, headers=self.headers
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("at least 3 seconds", response.text)

    def test_dashboard_includes_recent_task_progress_events(self) -> None:
        created = self.client.post(
            "/api/tasks", json=TASK, headers=self.headers
        )
        task_id = created.json()["id"]
        repository = self.client.app.state.repository
        repository.add_task_event(task_id, "checking_login", "Checking login")
        repository.add_task_event(
            task_id,
            "scan_complete",
            "Scan complete",
            {"available_count": 0, "scan_count": 1},
        )

        response = self.client.get("/api/dashboard", headers=self.headers)

        progress = response.json()["tasks"][0]["progress"]
        self.assertEqual(progress["current_stage"], "scan_complete")
        self.assertEqual(progress["current_message"], "Scan complete")
        self.assertEqual(progress["scan_count"], 1)
        self.assertEqual(progress["events"][0]["details"]["available_count"], 0)

    def test_dashboard_scan_count_is_total_not_recent_window(self) -> None:
        created = self.client.post(
            "/api/tasks", json=TASK, headers=self.headers
        )
        task_id = created.json()["id"]
        repository = self.client.app.state.repository
        for index in range(25):
            repository.add_task_event(
                task_id,
                "scan_complete",
                "Scan complete",
                {"available_count": 0, "index": index},
            )

        response = self.client.get("/api/dashboard", headers=self.headers)

        progress = response.json()["tasks"][0]["progress"]
        self.assertEqual(progress["scan_count"], 25)
        self.assertEqual(len(progress["events"]), 20)

    def test_dashboard_summarizes_seat_status_from_progress_events(self) -> None:
        created = self.client.post(
            "/api/tasks", json=TASK, headers=self.headers
        )
        task_id = created.json()["id"]
        repository = self.client.app.state.repository
        repository.add_task_event(
            task_id,
            "scan_complete",
            "Scan complete",
            {"available_count": 2, "available_seats": [12, 95]},
        )
        repository.add_task_event(
            task_id,
            "candidate_found",
            "Candidate seat found",
            {"seat": 95},
        )

        response = self.client.get("/api/dashboard", headers=self.headers)

        seat_status = response.json()["tasks"][0]["progress"]["seat_status"]
        self.assertEqual(seat_status["available_count"], 2)
        self.assertEqual(seat_status["available_seats"], [12, 95])
        self.assertEqual(seat_status["candidate_seat"], 95)

    def test_success_closes_other_area_workers_and_stops_tasks(self) -> None:
        class SuccessAdapter:
            instances = []

            def __init__(self) -> None:
                self.close_count = 0
                self.instances.append(self)

            def close(self) -> None:
                self.close_count += 1

            def check_login(self) -> bool:
                return True

            def scan(self, config):
                from seat_assistant.engine import ScanResult

                if config.area == "负一层东":
                    return ScanResult(())
                return ScanResult((88,))

            def submit(self, config, seat):
                from seat_assistant.domain import ReservationOutcome

                return ReservationOutcome.SUCCESS

            def verify_current_reservation(self, config, seat):
                return True

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                data_directory=Path(directory),
                access_token="test-token",
                enable_scheduler=False,
                adapter_factory=SuccessAdapter,
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
                now = datetime.now().astimezone()
                first = client.post(
                    "/api/tasks",
                    json={
                        **TASK,
                        "name": "Winner",
                        "area": "负一层书库",
                        "observation_mode": False,
                        "starts_at": now.isoformat(),
                        "stops_at": (now + timedelta(minutes=10)).isoformat(),
                    },
                    headers=headers,
                )
                second = client.post(
                    "/api/tasks",
                    json={
                        **TASK,
                        "name": "Sibling",
                        "area": "负一层东",
                        "observation_mode": False,
                        "starts_at": now.isoformat(),
                        "stops_at": (now + timedelta(minutes=10)).isoformat(),
                    },
                    headers=headers,
                )
                client.post(f"/api/tasks/{first.json()['id']}/start", headers=headers)
                client.post(f"/api/tasks/{second.json()['id']}/start", headers=headers)

                app.state.run_task(second.json()["id"])
                sibling_adapter = SuccessAdapter.instances[0]
                app.state.run_task(first.json()["id"])

                sibling = app.state.repository.get_task(second.json()["id"])

        self.assertEqual(sibling.state, TaskState.STOPPED)
        self.assertEqual(sibling_adapter.close_count, 1)

    def test_scheduler_retries_scan_failures_until_task_stops(self) -> None:
        class FailingAdapter:
            def close(self) -> None:
                pass

            def check_login(self) -> bool:
                return True

            def scan(self, config):
                raise RuntimeError("temporary page state")

            def submit(self, config, seat):
                raise AssertionError("submit should not run")

            def verify_current_reservation(self, config, seat):
                raise AssertionError("verify should not run")

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                data_directory=Path(directory),
                access_token="test-token",
                enable_scheduler=False,
                adapter_factory=FailingAdapter,
            )
            with TestClient(app) as client:
                headers = {"Authorization": "Bearer test-token"}
                now = datetime.now().astimezone()
                created = client.post(
                    "/api/tasks",
                    json={
                        **TASK,
                        "starts_at": now.isoformat(),
                        "stops_at": (now + timedelta(minutes=10)).isoformat(),
                        "max_consecutive_errors": 1,
                    },
                    headers=headers,
                )
                task_id = created.json()["id"]
                client.post(f"/api/tasks/{task_id}/start", headers=headers)

                app.state.tick()

                task = app.state.repository.get_task(task_id)
                payload = client.get("/api/dashboard", headers=headers).json()

        self.assertEqual(task.state, TaskState.SCHEDULED)
        self.assertEqual(
            payload["tasks"][0]["progress"]["current_stage"], "failed"
        )
        self.assertIsNotNone(payload["tasks"][0]["detection"]["next_check_at"])

    def test_scheduler_continues_immediately_after_each_scan(self) -> None:
        class EmptyAdapter:
            def close(self) -> None:
                pass

            def check_login(self) -> bool:
                return True

            def scan(self, config):
                from seat_assistant.engine import ScanResult

                return ScanResult(())

            def submit(self, config, seat):
                raise AssertionError("submit should not run")

            def verify_current_reservation(self, config, seat):
                raise AssertionError("verify should not run")

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                data_directory=Path(directory),
                access_token="test-token",
                enable_scheduler=False,
                adapter_factory=EmptyAdapter,
            )
            with TestClient(app) as client:
                headers = {"Authorization": "Bearer test-token"}
                now = datetime.now().astimezone()
                created = client.post(
                    "/api/tasks",
                    json={
                        **TASK,
                        "starts_at": now.isoformat(),
                        "stops_at": (now + timedelta(minutes=10)).isoformat(),
                        "refresh_min_seconds": 100,
                        "refresh_max_seconds": 100,
                    },
                    headers=headers,
                )
                task_id = created.json()["id"]
                client.post(f"/api/tasks/{task_id}/start", headers=headers)

                before_tick = datetime.now().astimezone()
                app.state.tick()

                payload = client.get("/api/dashboard", headers=headers).json()

        next_check_at = datetime.fromisoformat(
            payload["tasks"][0]["detection"]["next_check_at"]
        )
        self.assertLess(
            next_check_at,
            before_tick + timedelta(seconds=5),
        )

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

    def test_run_once_does_not_notify_on_generic_failure(self) -> None:
        class FakeNotifier:
            def __init__(self) -> None:
                self.errors = []

            def notify_success(self, task, seat: int | None) -> None:
                raise AssertionError("success notification was not expected")

            def notify_timeout(self, task) -> None:
                raise AssertionError("timeout notification was not expected")

            def notify_error(self, task, message: str) -> None:
                self.errors.append((task.config.name, message))

        class FailingAdapter:
            def close(self) -> None:
                pass

            def check_login(self) -> bool:
                return True

            def scan(self, config):
                raise RuntimeError("temporary page failure")

            def submit(self, config, seat):
                raise AssertionError("submit should not run")

            def verify_current_reservation(self, config, seat):
                raise AssertionError("verify should not run")

        notifier = FakeNotifier()
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                data_directory=Path(directory),
                access_token="test-token",
                enable_scheduler=False,
                adapter_factory=FailingAdapter,
                desktop_notifier=notifier,
            )
            with TestClient(app) as client:
                headers = {"Authorization": "Bearer test-token"}
                created = client.post(
                    "/api/tasks", json=TASK, headers=headers
                )

                response = app.state.run_task(created.json()["id"])

        self.assertEqual(response["outcome"], "failure")
        self.assertEqual(notifier.errors, [])

    def test_run_once_notifies_when_login_is_required(self) -> None:
        class FakeNotifier:
            def __init__(self) -> None:
                self.errors = []

            def notify_success(self, task, seat: int | None) -> None:
                raise AssertionError("success notification was not expected")

            def notify_timeout(self, task) -> None:
                raise AssertionError("timeout notification was not expected")

            def notify_error(self, task, message: str) -> None:
                self.errors.append((task.config.name, message))

        class LoginRequiredAdapter:
            def close(self) -> None:
                pass

            def check_login(self) -> bool:
                return False

            def scan(self, config):
                raise AssertionError("scan should not run")

            def submit(self, config, seat):
                raise AssertionError("submit should not run")

            def verify_current_reservation(self, config, seat):
                raise AssertionError("verify should not run")

        notifier = FakeNotifier()
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                data_directory=Path(directory),
                access_token="test-token",
                enable_scheduler=False,
                adapter_factory=LoginRequiredAdapter,
                desktop_notifier=notifier,
            )
            with TestClient(app) as client:
                headers = {"Authorization": "Bearer test-token"}
                created = client.post(
                    "/api/tasks", json=TASK, headers=headers
                )

                response = app.state.run_task(created.json()["id"])

        self.assertEqual(response["outcome"], "login_required")
        self.assertEqual(notifier.errors, [("Morning", response["message"])])

    def test_stopped_task_login_required_does_not_clear_account(self) -> None:
        class FakeNotifier:
            def __init__(self) -> None:
                self.errors = []

            def notify_success(self, task, seat: int | None) -> None:
                raise AssertionError("success notification was not expected")

            def notify_timeout(self, task) -> None:
                raise AssertionError("timeout notification was not expected")

            def notify_error(self, task, message: str) -> None:
                self.errors.append((task.config.name, message))

        class BlockingLoginAdapter:
            started = threading.Event()
            release = threading.Event()

            def close(self) -> None:
                self.release.set()

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

        notifier = FakeNotifier()
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                data_directory=Path(directory),
                access_token="test-token",
                enable_scheduler=False,
                adapter_factory=BlockingLoginAdapter,
                desktop_notifier=notifier,
            )
            with TestClient(app) as client:
                headers = {"Authorization": "Bearer test-token"}
                created = client.post(
                    "/api/tasks", json=TASK, headers=headers
                )
                task_id = created.json()["id"]
                app.state.repository.set_setting("account_status", "connected")
                thread = threading.Thread(target=lambda: app.state.run_task(task_id))
                thread.start()
                self.assertTrue(BlockingLoginAdapter.started.wait(timeout=2))

                client.post(f"/api/tasks/{task_id}/stop", headers=headers)
                thread.join(timeout=5)

                account_status = app.state.repository.get_setting("account_status")

        self.assertEqual(account_status, "connected")
        self.assertEqual(notifier.errors, [])

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
