"""FastAPI Web console for the local ZJU seat assistant."""

from __future__ import annotations

import argparse
import os
import queue
import secrets
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .browser import PlaywrightAdapter
from .desktop_notifications import DesktopNotifier, TaskNotifier
from .domain import (
    ReservationConfig,
    ReservationOutcome,
    SeatRule,
    TaskState,
    TERMINAL_STATES,
)
from .engine import ExecutionEngine
from .http_adapter import HybridReservationAdapter, HttpSessionAdapter
from .http_endpoints import endpoints_from_environment
from .storage import Repository, StoredTask


PACKAGE_DIRECTORY = Path(__file__).parent
LOGIN_POLL_SECONDS = 20
LOGIN_TIMEOUT_SECONDS = 600
MAX_PARALLEL_TASKS = 4


def create_app(
    data_directory: Path | None = None,
    access_token: str | None = None,
    enable_scheduler: bool = True,
    adapter_factory: Callable[[], PlaywrightAdapter] | None = None,
    desktop_notifier: TaskNotifier | None = None,
) -> FastAPI:
    data_directory = Path(data_directory or os.getenv("ZJU_SEAT_DATA", "data"))
    data_directory.mkdir(parents=True, exist_ok=True)
    repository = Repository(data_directory / "assistant.db")
    token = access_token if access_token is not None else os.getenv(
        "ZJU_SEAT_ACCESS_TOKEN", ""
    )
    notifier = desktop_notifier or DesktopNotifier()
    repository.finish_unfinished_runs("interrupted by service restart")
    if repository.get_setting("account_status") == "connected":
        _resume_waiting_login_tasks(repository)

    def submission_enabled() -> bool:
        return repository.get_setting("submission_enabled", "false") == "true"

    def reservation_browser_visible() -> bool:
        visible = os.getenv("ZJU_SEAT_RESERVATION_VISIBLE", "").casefold()
        return visible in {"1", "true", "yes", "on"}

    def http_submit_enabled() -> bool:
        enabled = os.getenv("ZJU_SEAT_HTTP_SUBMIT", "").casefold()
        return enabled in {"1", "true", "yes", "on"}

    def make_adapter(
        headless: bool | None = None,
        profile_directory: Path | None = None,
    ) -> PlaywrightAdapter:
        if adapter_factory is not None:
            return adapter_factory()
        visible = reservation_browser_visible()
        return PlaywrightAdapter(
            profile_directory=profile_directory or data_directory / "browser-profile",
            diagnostics_directory=data_directory / "diagnostics",
            headless=False if headless is None else headless,
            background_window=(not visible) if headless is None else False,
        )

    def make_reservation_adapter(task_id: str) -> Any:
        browser_adapter = make_adapter(
            profile_directory=_prepare_reservation_profile(data_directory, task_id)
        )
        endpoints = endpoints_from_environment()
        if endpoints is None:
            return browser_adapter

        def token_provider() -> str:
            token = browser_adapter.extract_token()
            return token or ""

        http = HttpSessionAdapter(
            endpoints,
            token_provider=token_provider,
        )
        return HybridReservationAdapter(
            browser_adapter,
            http,
            endpoints,
            http_submit_enabled=http_submit_enabled(),
        )

    def make_login_adapter() -> PlaywrightAdapter:
        if adapter_factory is not None:
            return adapter_factory()
        return PlaywrightAdapter(
            profile_directory=data_directory / "browser-profile",
            diagnostics_directory=data_directory / "diagnostics",
            headless=False,
            background_window=False,
        )

    scheduler: BackgroundScheduler | None = None
    login_check_event = threading.Event()
    next_attempt: dict[str, datetime] = {}
    consecutive_errors: dict[str, int] = {}
    active_run_lock = threading.Lock()
    active_run_task_ids: set[str] = set()
    reservation_workers: dict[str, _ReservationWorker] = {}
    reservation_worker_lock = threading.Lock()
    scheduler_executor = ThreadPoolExecutor(
        max_workers=MAX_PARALLEL_TASKS,
        thread_name_prefix="zju-seat-scheduler",
    )

    def task_payload(task: StoredTask) -> dict[str, Any]:
        next_check_at = next_attempt.get(task.id)
        if next_check_at is None and task.state == TaskState.SCHEDULED:
            next_check_at = task.config.starts_at
        return _task_to_dict(
            task,
            last_run=repository.get_latest_run(task.id),
            next_check_at=next_check_at,
            events=repository.list_task_events(task.id),
            scan_count=repository.count_task_events(task.id, "scan_complete"),
            submission_enabled=submission_enabled(),
        )

    def run_task_with_adapter(task_id: str, adapter: Any) -> dict[str, Any]:
        engine = ExecutionEngine(
            repository,
            adapter,
            submission_enabled=submission_enabled(),
        )
        result = engine.run_once(task_id)
        task = repository.get_task(task_id)
        if result.outcome.task_state is TaskState.SUCCEEDED:
            _safe_notify(notifier.notify_success, task, result.seat)
        elif (
            result.outcome is ReservationOutcome.LOGIN_REQUIRED
            and task.state is not TaskState.STOPPED
        ):
            was_connected = (
                repository.get_setting("account_status") == "connected"
            )
            if was_connected:
                _safe_notify(notifier.notify_error, task, result.message)
        return {
            "outcome": result.outcome.value,
            "seat": result.seat,
            "message": result.message,
        }

    def get_reservation_worker(task_id: str) -> "_ReservationWorker":
        with reservation_worker_lock:
            worker = reservation_workers.get(task_id)
            if worker is None:
                worker = _ReservationWorker(
                    lambda: make_reservation_adapter(task_id), run_task_with_adapter
                )
                reservation_workers[task_id] = worker
            return worker

    def close_reservation_adapter(task_id: str | None = None) -> None:
        with reservation_worker_lock:
            if task_id is None:
                workers = list(reservation_workers.values())
                reservation_workers.clear()
            else:
                worker = reservation_workers.pop(task_id, None)
                workers = [] if worker is None else [worker]
        for worker in workers:
            worker.close()

    def stop_other_tasks_after_success(winner_task_id: str) -> None:
        for item in repository.list_tasks():
            if item.id == winner_task_id:
                continue
            if item.state not in TERMINAL_STATES:
                try:
                    repository.set_task_state(item.id, TaskState.STOPPED)
                except ValueError:
                    pass
            next_attempt.pop(item.id, None)
            consecutive_errors.pop(item.id, None)
            close_reservation_adapter(item.id)

    def stop_all_reservation_tasks() -> int:
        stopped = 0
        for item in repository.list_tasks():
            if item.state not in TERMINAL_STATES:
                try:
                    repository.set_task_state(item.id, TaskState.STOPPED)
                    stopped += 1
                except ValueError:
                    pass
            next_attempt.pop(item.id, None)
            consecutive_errors.pop(item.id, None)
        close_reservation_adapter()
        return stopped

    def _mark_task_active(task_id: str) -> bool:
        with active_run_lock:
            if task_id in active_run_task_ids:
                return False
            active_run_task_ids.add(task_id)
            return True

    def _clear_task_active(task_id: str) -> None:
        with active_run_lock:
            active_run_task_ids.discard(task_id)

    def _in_progress_result() -> dict[str, Any]:
        return {
            "outcome": "in_progress",
            "seat": None,
            "message": "this task is already running",
        }

    def _run_marked_task(task_id: str) -> dict[str, Any]:
        result = get_reservation_worker(task_id).run(task_id)
        if (
            result["outcome"] == ReservationOutcome.LOGIN_REQUIRED.value
            and repository.get_setting("account_status") == "connected"
            and repository.get_task(task_id).state is not TaskState.STOPPED
        ):
            repository.set_setting("account_status", "not_connected")
        if result["outcome"] == ReservationOutcome.SUCCESS.value:
            stop_other_tasks_after_success(task_id)
        return result

    def run_task(task_id: str) -> dict[str, Any]:
        if not _mark_task_active(task_id):
            return _in_progress_result()
        try:
            return _run_marked_task(task_id)
        finally:
            _clear_task_active(task_id)

    def _finish_scheduled_task(
        task_id: str, stops_at: datetime, result: dict[str, Any]
    ) -> None:
        if result["outcome"] == "in_progress":
            return
        task = repository.get_task(task_id)
        if task.state is TaskState.STOPPED:
            next_attempt.pop(task_id, None)
            consecutive_errors.pop(task_id, None)
            return
        if result["outcome"] == "failure":
            consecutive_errors[task_id] = consecutive_errors.get(task_id, 0) + 1
            try:
                repository.set_task_state(task_id, TaskState.SCHEDULED)
            except ValueError:
                pass
        else:
            consecutive_errors[task_id] = 0
        after_run = datetime.now().astimezone()
        if after_run >= stops_at:
            try:
                repository.set_task_state(task_id, TaskState.TIMED_OUT)
                _safe_notify(notifier.notify_timeout, task)
            except ValueError:
                pass
            return
        if result["outcome"] == "failure":
            retry_seconds = max(3.0, task.config.refresh_min_seconds)
            next_attempt[task_id] = after_run + timedelta(seconds=retry_seconds)
        else:
            next_attempt[task_id] = after_run

    def _run_scheduled_task(task_id: str, stops_at: datetime) -> dict[str, Any]:
        try:
            result = _run_marked_task(task_id)
            _finish_scheduled_task(task_id, stops_at, result)
            return result
        finally:
            _clear_task_active(task_id)

    def _dispatch_scheduled_task(task_id: str, stops_at: datetime) -> bool:
        with active_run_lock:
            if (
                task_id in active_run_task_ids
                or len(active_run_task_ids) >= MAX_PARALLEL_TASKS
            ):
                return False
            active_run_task_ids.add(task_id)
        scheduler_executor.submit(_run_scheduled_task, task_id, stops_at)
        return True

    def tick() -> None:
        now = datetime.now().astimezone()
        for task in repository.list_tasks():
            if task.state not in {TaskState.SCHEDULED, TaskState.RUNNING}:
                continue
            starts_at = _localize(task.config.starts_at, now)
            stops_at = _localize(task.config.stops_at, now)
            if now >= stops_at:
                try:
                    repository.set_task_state(task.id, TaskState.TIMED_OUT)
                    _safe_notify(notifier.notify_timeout, task)
                except ValueError:
                    pass
                continue
            if now < starts_at or now < next_attempt.get(task.id, starts_at):
                continue
            _dispatch_scheduled_task(task.id, stops_at)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal scheduler
        if enable_scheduler:
            scheduler = BackgroundScheduler()
            scheduler.add_job(
                tick,
                "interval",
                seconds=1,
                max_instances=1,
                coalesce=True,
                id="reservation-tick",
            )
            scheduler.start()
        yield
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        scheduler_executor.shutdown(wait=False, cancel_futures=True)
        close_reservation_adapter()
        repository.close()

    app = FastAPI(title="浙大图书馆座位助手", lifespan=lifespan)
    app.state.repository = repository
    app.state.access_token = token
    app.state.run_task = run_task
    app.state.tick = tick
    app.mount(
        "/static",
        StaticFiles(directory=PACKAGE_DIRECTORY / "static"),
        name="static",
    )

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            if request.url.path != "/api/health" and token:
                supplied = request.headers.get("authorization", "")
                if not secrets.compare_digest(supplied, f"Bearer {token}"):
                    return Response(
                        content='{"detail":"invalid or missing access token"}',
                        status_code=401,
                        media_type="application/json",
                    )
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(PACKAGE_DIRECTORY / "templates" / "index.html")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "submission_enabled": submission_enabled(),
            "token_required": bool(token),
        }

    @app.get("/api/dashboard")
    async def dashboard() -> dict[str, Any]:
        return {
            "account_status": repository.get_setting(
                "account_status", "not_connected"
            ),
            "submission_enabled": submission_enabled(),
            "tasks": [task_payload(item) for item in repository.list_tasks()],
            "reservations": repository.list_reservations()[:20],
        }

    @app.post("/api/tasks", status_code=201)
    async def create_task(request: Request) -> dict[str, Any]:
        config = await _parse_config(request)
        task_id = repository.create_task(config)
        return task_payload(repository.get_task(task_id))

    @app.post("/api/tasks/stop-all")
    async def stop_all_tasks() -> dict[str, int]:
        return {"stopped": stop_all_reservation_tasks()}

    @app.put("/api/tasks/{task_id}")
    async def update_task(task_id: str, request: Request) -> dict[str, Any]:
        config = await _parse_config(request)
        try:
            repository.update_task(task_id, config)
            return task_payload(repository.get_task(task_id))
        except KeyError:
            raise HTTPException(404, "task not found")

    @app.delete("/api/tasks/{task_id}", status_code=204)
    async def delete_task(task_id: str) -> Response:
        try:
            repository.delete_task(task_id)
            next_attempt.pop(task_id, None)
            consecutive_errors.pop(task_id, None)
            close_reservation_adapter(task_id)
        except KeyError:
            raise HTTPException(404, "task not found")
        return Response(status_code=204)

    @app.post("/api/tasks/{task_id}/start")
    async def start_task(task_id: str) -> dict[str, Any]:
        try:
            task = repository.get_task(task_id)
            if task.state not in TERMINAL_STATES or task.state in {
                TaskState.STOPPED,
                TaskState.FAILED,
                TaskState.TIMED_OUT,
            }:
                repository.set_task_state(task_id, TaskState.SCHEDULED)
            next_attempt.pop(task_id, None)
            consecutive_errors.pop(task_id, None)
            return task_payload(repository.get_task(task_id))
        except KeyError:
            raise HTTPException(404, "task not found")
        except ValueError as error:
            raise HTTPException(409, str(error))

    @app.post("/api/tasks/{task_id}/stop")
    async def stop_task(task_id: str) -> dict[str, Any]:
        try:
            task = repository.get_task(task_id)
            if task.state not in TERMINAL_STATES:
                repository.set_task_state(task_id, TaskState.STOPPED)
            next_attempt.pop(task_id, None)
            consecutive_errors.pop(task_id, None)
            close_reservation_adapter(task_id)
            return task_payload(repository.get_task(task_id))
        except KeyError:
            raise HTTPException(404, "task not found")
        except ValueError as error:
            raise HTTPException(409, str(error))

    @app.post("/api/tasks/{task_id}/run-once")
    async def run_once(task_id: str, background: BackgroundTasks) -> dict[str, str]:
        try:
            repository.get_task(task_id)
        except KeyError:
            raise HTTPException(404, "task not found")
        background.add_task(run_task, task_id)
        return {"status": "started"}

    @app.post("/api/account/login")
    async def account_login(background: BackgroundTasks) -> dict[str, str]:
        close_reservation_adapter()
        _clear_reservation_profiles(data_directory)
        login_check_event.clear()
        background.add_task(
            _login_worker, repository, make_login_adapter, login_check_event
        )
        return {"status": "login_window_opening"}

    @app.post("/api/account/verify")
    async def account_verify() -> dict[str, str]:
        login_check_event.set()
        return {"status": "verification_requested"}

    @app.get("/api/settings/system")
    async def get_system_settings() -> dict[str, bool]:
        return {"submission_enabled": submission_enabled()}

    @app.put("/api/settings/system")
    async def put_system_settings(request: Request) -> dict[str, bool]:
        try:
            payload = await request.json()
            enabled = bool(payload["submission_enabled"])
        except (KeyError, TypeError, ValueError) as error:
            raise HTTPException(422, str(error))
        if enabled and payload.get("confirmation") != "确认自动预约":
            raise HTTPException(422, "请输入确认短语：确认自动预约")
        repository.set_setting(
            "submission_enabled", "true" if enabled else "false"
        )
        return {"submission_enabled": enabled}


    return app


async def _parse_config(request: Request) -> ReservationConfig:
    try:
        payload = await request.json()
        rules = tuple(
            SeatRule(
                priority=int(rule["priority"]),
                start=_optional_int(rule.get("start")),
                end=_optional_int(rule.get("end")),
                included=frozenset(int(value) for value in rule.get("included", [])),
                excluded=frozenset(int(value) for value in rule.get("excluded", [])),
                accept_any=bool(rule.get("accept_any", False)),
                order=str(rule.get("order", "asc")),
            )
            for rule in payload["seat_rules"]
        )
        return ReservationConfig(
            name=str(payload["name"]),
            venue=str(payload["venue"]),
            floor=str(payload.get("floor", "")),
            area=str(payload["area"]),
            reservation_date=date.fromisoformat(payload["reservation_date"]),
            time_slot=str(payload["time_slot"]),
            starts_at=datetime.fromisoformat(payload["starts_at"]),
            stops_at=datetime.fromisoformat(payload["stops_at"]),
            seat_rules=rules,
            refresh_min_seconds=float(payload.get("refresh_min_seconds", 8)),
            refresh_max_seconds=float(payload.get("refresh_max_seconds", 15)),
            max_consecutive_errors=int(payload.get("max_consecutive_errors", 5)),
            observation_mode=bool(payload.get("observation_mode", True)),
            notify_success=bool(payload.get("notify_success", True)),
            notify_timeout=bool(payload.get("notify_timeout", True)),
            notify_error=bool(payload.get("notify_error", True)),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(422, str(error))


def _task_to_dict(
    task: StoredTask,
    last_run: dict[str, Any] | None = None,
    next_check_at: datetime | None = None,
    events: list[dict[str, Any]] | None = None,
    scan_count: int | None = None,
    submission_enabled: bool = False,
) -> dict[str, Any]:
    config = asdict(task.config)
    config["reservation_date"] = task.config.reservation_date.isoformat()
    config["starts_at"] = task.config.starts_at.isoformat()
    config["stops_at"] = task.config.stops_at.isoformat()
    config["seat_rules"] = [
        {
            **asdict(rule),
            "included": sorted(rule.included),
            "excluded": sorted(rule.excluded),
        }
        for rule in task.config.seat_rules
    ]
    blockers = []
    if not submission_enabled:
        blockers.append("自动提交总开关未开启")
    if task.config.observation_mode:
        blockers.append("任务仍处于观察模式")
    progress_events = events or []
    current_event = progress_events[0] if progress_events else None
    recent_scan_count = sum(
        1 for event in progress_events if event["stage"] == "scan_complete"
    )
    seat_status = _seat_status_from_events(progress_events)
    return {
        "id": task.id,
        "state": task.state.value,
        "config": config,
        "last_error": task.last_error,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "detection": {
            "last_run": last_run,
            "next_check_at": (
                next_check_at.isoformat() if next_check_at is not None else None
            ),
        },
        "progress": {
            "current_stage": current_event["stage"] if current_event else None,
            "current_message": current_event["message"] if current_event else None,
            "scan_count": (
                recent_scan_count if scan_count is None else scan_count
            ),
            "seat_status": seat_status,
            "events": progress_events,
        },
        "submission_policy": {
            "will_submit": not blockers,
            "blockers": blockers,
        },
    }


def _optional_int(value: Any) -> int | None:
    return None if value in (None, "") else int(value)


def _prepare_reservation_profile(data_directory: Path, task_id: str) -> Path:
    source = data_directory / "browser-profile"
    target = data_directory / "reservation-profiles" / task_id
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.exists():
        shutil.copytree(
            source,
            target,
            ignore=shutil.ignore_patterns(
                "Singleton*",
                "LOCK",
                "lockfile",
                "*.lock",
                "DevToolsActivePort",
                "Crashpad",
            ),
        )
    else:
        target.mkdir(parents=True, exist_ok=True)
    return target


def _clear_reservation_profiles(data_directory: Path) -> None:
    profiles = data_directory / "reservation-profiles"
    if profiles.exists():
        shutil.rmtree(profiles, ignore_errors=True)


def _seat_status_from_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    latest_scan = next(
        (event for event in events if event["stage"] == "scan_complete"),
        None,
    )
    latest_candidate = next(
        (event for event in events if event["stage"] == "candidate_found"),
        None,
    )
    scan_details = latest_scan["details"] if latest_scan else {}
    candidate_details = latest_candidate["details"] if latest_candidate else {}
    return {
        "available_count": scan_details.get("available_count"),
        "available_seats": scan_details.get("available_seats", []),
        "candidate_seat": candidate_details.get("seat"),
    }


class _ReservationRunRequest:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.done = threading.Event()
        self.result: dict[str, Any] | None = None
        self.error: BaseException | None = None


class _ReservationWorker:
    def __init__(
        self,
        adapter_factory: Callable[[], Any],
        run_with_adapter: Callable[[str, Any], dict[str, Any]],
    ):
        self._adapter_factory = adapter_factory
        self._run_with_adapter = run_with_adapter
        self._queue: queue.Queue[_ReservationRunRequest | None] = queue.Queue()
        self._thread = threading.Thread(
            target=self._loop,
            name="zju-seat-reservation-browser",
            daemon=True,
        )
        self._thread.start()

    def run(self, task_id: str) -> dict[str, Any]:
        request = _ReservationRunRequest(task_id)
        self._queue.put(request)
        request.done.wait()
        if request.error is not None:
            raise request.error
        if request.result is None:
            raise RuntimeError("reservation worker finished without a result")
        return request.result

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=15)

    def _loop(self) -> None:
        adapter: Any | None = None
        try:
            while True:
                request = self._queue.get()
                if request is None:
                    return
                try:
                    if adapter is None:
                        adapter = self._adapter_factory()
                    request.result = self._run_with_adapter(
                        request.task_id, adapter
                    )
                except BaseException as error:
                    request.error = error
                finally:
                    request.done.set()
        finally:
            if adapter is not None:
                adapter.close()



def _safe_notify(callback: Callable[..., None], *args: Any) -> None:
    try:
        callback(*args)
    except Exception:
        return


def _resume_waiting_login_tasks(repository: Repository) -> None:
    for task in repository.list_tasks():
        if task.state is TaskState.WAITING_LOGIN:
            try:
                repository.set_task_state(task.id, TaskState.SCHEDULED)
            except ValueError:
                pass


def _localize(value: datetime, now: datetime) -> datetime:
    return value.replace(tzinfo=now.tzinfo) if value.tzinfo is None else value


def _login_worker(
    repository: Repository,
    adapter_factory: Callable[[], PlaywrightAdapter],
    check_event: threading.Event | None = None,
) -> None:
    adapter = adapter_factory()
    check_event = check_event or threading.Event()
    try:
        adapter.open_for_login()
        deadline = time.monotonic() + LOGIN_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            check_event.wait(LOGIN_POLL_SECONDS)
            check_event.clear()
            if adapter.check_login(navigate=False):
                repository.set_setting("account_status", "connected")
                repository.set_setting(
                    "last_login_check", datetime.now().astimezone().isoformat()
                )
                _resume_waiting_login_tasks(repository)
                return
        repository.set_setting("account_status", "login_timeout")
    except Exception as error:
        detail = str(error).strip()
        if len(detail) > 240:
            detail = detail[:237] + "..."
        repository.set_setting(
            "account_status",
            f"error:{type(error).__name__}: {detail}"
            if detail
            else f"error:{type(error).__name__}",
        )
    finally:
        adapter.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the ZJU seat assistant")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"} and not os.getenv(
        "ZJU_SEAT_ACCESS_TOKEN"
    ):
        parser.error(
            "ZJU_SEAT_ACCESS_TOKEN is required when listening beyond localhost"
        )
    import uvicorn

    uvicorn.run(
        create_app(args.data_dir),
        host=args.host,
        port=args.port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
