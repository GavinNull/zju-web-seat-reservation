"""Local desktop notifications for reservation events."""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass
from typing import Protocol

from .domain import ReservationConfig
from .storage import StoredTask


class TaskNotifier(Protocol):
    def notify_success(self, task: StoredTask, seat: int | None) -> None: ...
    def notify_timeout(self, task: StoredTask) -> None: ...
    def notify_error(self, task: StoredTask, message: str) -> None: ...


@dataclass(frozen=True)
class DesktopNotifier:
    timeout_seconds: int = 10

    def notify_success(self, task: StoredTask, seat: int | None) -> None:
        if not task.config.notify_success:
            return
        seat_text = f"座位：{seat:03d}" if seat is not None else "座位：待确认"
        self._popup("预约成功", self._body(task.config, seat_text))

    def notify_timeout(self, task: StoredTask) -> None:
        if not task.config.notify_timeout:
            return
        self._popup("预约任务已超时", self._body(task.config, "任务已停止"))

    def notify_error(self, task: StoredTask, message: str) -> None:
        if not task.config.notify_error:
            return
        detail = message.strip() or "请检查登录状态或页面变化"
        self._popup("预约任务异常", self._body(task.config, detail))

    def _body(self, config: ReservationConfig, detail: str) -> str:
        return "\n".join(
            (
                config.name,
                f"{config.venue} / {config.floor} / {config.area}",
                f"{config.reservation_date.isoformat()} {config.time_slot}",
                detail,
            )
        )

    def _popup(self, title: str, body: str) -> None:
        if platform.system() != "Windows":
            return
        script = (
            "$shell = New-Object -ComObject WScript.Shell; "
            "$null = $shell.Popup("
            f"{_ps_string(body)}, {self.timeout_seconds}, {_ps_string(title)}, 64"
            ")"
        )
        try:
            subprocess.Popen(
                [
                    "powershell",
                    "-NoProfile",
                    "-WindowStyle",
                    "Hidden",
                    "-Command",
                    script,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError:
            return


def _ps_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
