"""FastAPI Web console for the local ZJU seat assistant."""

from __future__ import annotations

import argparse
import os
import random
import secrets
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import date, datetime
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
from .storage import Repository, StoredTask


PACKAGE_DIRECTORY = Path(__file__).parent
LOGIN_POLL_SECONDS = 20
LOGIN_TIMEOUT_SECONDS = 600


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

    def make_adapter(headless: bool | None = None) -> PlaywrightAdapter:
        if adapter_factory is not None:
            return adapter_factory()
        visible = reservation_browser_visible()
        return PlaywrightAdapter(
            profile_directory=data_directory / "browser-profile",
            diagnostics_directory=data_directory / "diagnostics",
            headless=False if headless is None else headless,
            background_window=(not visible) if headless is None else False,
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

    def task_payload(task: StoredTask) -> dict[str, Any]:
        next_check_at = next_attempt.get(task.id)
        if next_check_at is None and task.state == TaskState.SCHEDULED:
            next_check_at = task.config.starts_at
        return _task_to_dict(
            task,
            last_run=repository.get_latest_run(task.id),
            next_check_at=next_check_at,
            submission_enabled=submission_enabled(),
        )

    def run_task_with_adapter(
        task_id: str, adapter: PlaywrightAdapter
    ) -> dict[str, Any]:
        try:
            engine = ExecutionEngine(
                repository,
                adapter,
                submission_enabled=submission_enabled(),
            )
            result = engine.run_once(task_id)
            task = repository.get_task(task_id)
            if result.outcome.task_state is TaskState.SUCCEEDED:
                _safe_notify(notifier.notify_success, task, result.seat)
            elif result.outcome.task_state is TaskState.FAILED:
                _safe_notify(notifier.notify_error, task, result.message)
            return {
                "outcome": result.outcome.value,
                "seat": result.seat,
                "message": result.message,
            }
        finally:
            adapter.close()

    def run_task(task_id: str) -> dict[str, Any]:
        with active_run_lock:
            if active_run_task_ids:
                return {
                    "outcome": "in_progress",
                    "seat": None,
                    "message": "another detection is already running",
                }
            active_run_task_ids.add(task_id)
        try:
            result = run_task_with_adapter(task_id, make_adapter())
            if (
                result["outcome"] == ReservationOutcome.LOGIN_REQUIRED.value
                and repository.get_setting("account_status") == "connected"
            ):
                repository.set_setting("account_status", "not_connected")
            return result
        finally:
            with active_run_lock:
                active_run_task_ids.discard(task_id)

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
            result = run_task(task.id)
            if result["outcome"] == "in_progress":
                continue
            if repository.get_task(task.id).state is TaskState.STOPPED:
                next_attempt.pop(task.id, None)
                consecutive_errors.pop(task.id, None)
                continue
            if result["outcome"] == "failure":
                consecutive_errors[task.id] = consecutive_errors.get(task.id, 0) + 1
                if consecutive_errors[task.id] >= task.config.max_consecutive_errors:
                    try:
                        repository.set_task_state(
                            task.id,
                            TaskState.FAILED,
                            "maximum consecutive errors reached",
                        )
                        _safe_notify(
                            notifier.notify_error,
                            task,
                            "maximum consecutive errors reached",
                        )
                    except ValueError:
                        pass
                    continue
                try:
                    repository.set_task_state(task.id, TaskState.SCHEDULED)
                except ValueError:
                    pass
            else:
                consecutive_errors[task.id] = 0
            after_run = datetime.now().astimezone()
            if after_run >= stops_at:
                try:
                    repository.set_task_state(task.id, TaskState.TIMED_OUT)
                    _safe_notify(notifier.notify_timeout, task)
                except ValueError:
                    pass
                continue
            delay = random.uniform(
                task.config.refresh_min_seconds,
                task.config.refresh_max_seconds,
            )
            next_attempt[task.id] = datetime.fromtimestamp(
                after_run.timestamp() + delay, tz=after_run.tzinfo
            )

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
        repository.close()

    app = FastAPI(title="浙大图书馆座位助手", lifespan=lifespan)
    app.state.repository = repository
    app.state.access_token = token
    app.state.run_task = run_task
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
        "submission_policy": {
            "will_submit": not blockers,
            "blockers": blockers,
        },
    }


def _optional_int(value: Any) -> int | None:
    return None if value in (None, "") else int(value)



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
