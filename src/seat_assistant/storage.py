"""SQLite persistence for tasks, runs, reservations, and settings."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .domain import ReservationConfig, SeatRule, TaskState, validate_transition


@dataclass(frozen=True)
class StoredTask:
    id: str
    state: TaskState
    config: ReservationConfig
    created_at: str
    updated_at: str
    last_error: str | None = None


class Repository:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "Repository":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _initialize(self) -> None:
        self._connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                config_json TEXT NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_runs (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                result TEXT,
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS reservations (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                run_id TEXT REFERENCES task_runs(id) ON DELETE SET NULL,
                seat_number TEXT NOT NULL,
                verified INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        run_columns = {
            row["name"]
            for row in self._connection.execute(
                "PRAGMA table_info(task_runs)"
            ).fetchall()
        }
        if "seat_number" not in run_columns:
            self._connection.execute(
                "ALTER TABLE task_runs ADD COLUMN seat_number INTEGER"
            )

    def create_task(self, config: ReservationConfig) -> str:
        task_id = uuid.uuid4().hex
        now = _now()
        with self._lock:
            self._connection.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, NULL, ?, ?)",
                (
                    task_id,
                    TaskState.DRAFT.value,
                    _config_to_json(config),
                    now,
                    now,
                ),
            )
        return task_id

    def update_task(self, task_id: str, config: ReservationConfig) -> None:
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE tasks SET config_json=?, updated_at=? WHERE id=?",
                (_config_to_json(config), _now(), task_id),
            )
        if cursor.rowcount != 1:
            raise KeyError(task_id)

    def get_task(self, task_id: str) -> StoredTask:
        row = self._connection.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return _row_to_task(row)

    def list_tasks(self) -> list[StoredTask]:
        rows = self._connection.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC"
        ).fetchall()
        return [_row_to_task(row) for row in rows]

    def delete_task(self, task_id: str) -> None:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM tasks WHERE id=?", (task_id,)
            )
        if cursor.rowcount != 1:
            raise KeyError(task_id)

    def set_task_state(
        self, task_id: str, state: TaskState, error: str | None = None
    ) -> None:
        task = self.get_task(task_id)
        validate_transition(task.state, state)
        with self._lock:
            self._connection.execute(
                "UPDATE tasks SET state=?, last_error=?, updated_at=? WHERE id=?",
                (state.value, error, _now(), task_id),
            )

    def start_run(self, task_id: str) -> str:
        self.get_task(task_id)
        run_id = uuid.uuid4().hex
        self._connection.execute(
            "INSERT INTO task_runs(id, task_id, started_at) VALUES (?, ?, ?)",
            (run_id, task_id, _now()),
        )
        return run_id

    def finish_run(
        self,
        run_id: str,
        result: str,
        error: str | None = None,
        seat_number: int | None = None,
    ) -> None:
        self._connection.execute(
            """
            UPDATE task_runs
            SET finished_at=?, result=?, last_error=?, seat_number=?
            WHERE id=?
            """,
            (_now(), result, error, seat_number, run_id),
        )

    def finish_unfinished_runs(self, error: str) -> int:
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE task_runs
                SET finished_at=?, result=?, last_error=?
                WHERE finished_at IS NULL
                """,
                (_now(), "failure", error),
            )
        return cursor.rowcount

    def get_latest_run(self, task_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            """
            SELECT id, task_id, started_at, finished_at, result, last_error,
                   seat_number
            FROM task_runs
            WHERE task_id=?
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def record_reservation(
        self,
        task_id: str,
        run_id: str | None,
        seat_number: str,
        verified: bool,
    ) -> str:
        reservation_id = uuid.uuid4().hex
        self._connection.execute(
            "INSERT INTO reservations VALUES (?, ?, ?, ?, ?, ?)",
            (
                reservation_id,
                task_id,
                run_id,
                seat_number,
                int(verified),
                _now(),
            ),
        )
        return reservation_id

    def list_reservations(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM reservations ORDER BY created_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def set_setting(self, key: str, value: str) -> None:
        self._connection.execute(
            """
            INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (key, value, _now()),
        )

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._connection.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return default if row is None else str(row["value"])


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _config_to_json(config: ReservationConfig) -> str:
    payload = asdict(config)
    payload["reservation_date"] = config.reservation_date.isoformat()
    payload["starts_at"] = config.starts_at.isoformat()
    payload["stops_at"] = config.stops_at.isoformat()
    payload["seat_rules"] = [
        {
            **asdict(rule),
            "included": sorted(rule.included),
            "excluded": sorted(rule.excluded),
        }
        for rule in config.seat_rules
    ]
    return json.dumps(payload, ensure_ascii=False)


def _config_from_json(value: str) -> ReservationConfig:
    payload = json.loads(value)
    payload["reservation_date"] = date.fromisoformat(payload["reservation_date"])
    payload["starts_at"] = datetime.fromisoformat(payload["starts_at"])
    payload["stops_at"] = datetime.fromisoformat(payload["stops_at"])
    payload["seat_rules"] = tuple(
        SeatRule(
            **{
                **rule,
                "included": frozenset(rule["included"]),
                "excluded": frozenset(rule["excluded"]),
            }
        )
        for rule in payload["seat_rules"]
    )
    return ReservationConfig(**payload)


def _row_to_task(row: sqlite3.Row) -> StoredTask:
    return StoredTask(
        id=str(row["id"]),
        state=TaskState(row["state"]),
        config=_config_from_json(row["config_json"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        last_error=row["last_error"],
    )
