import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _launcher_text() -> str:
    launchers = list(ROOT.glob("*.bat"))
    assert len(launchers) == 1
    return launchers[0].read_text(encoding="utf-8")


class LauncherTests(unittest.TestCase):
    def test_launcher_starts_service_and_opens_console(self) -> None:
        launcher = _launcher_text()

        self.assertIn("%~dp0", launcher)
        self.assertIn("zju-seat-assistant.exe", launcher)
        self.assertIn("/api/health", launcher)
        self.assertIn("http://127.0.0.1:8765", launcher)
        self.assertIn("Service is already running", launcher)

    def test_launcher_recreates_broken_virtual_environment(self) -> None:
        launcher = _launcher_text()

        self.assertIn('".venv\\Scripts\\python.exe" --version', launcher)
        self.assertIn("Existing .venv is broken. Recreating it...", launcher)
        self.assertIn('rmdir /s /q ".venv"', launcher)

    def test_launcher_enables_http_reservation_mode(self) -> None:
        launcher = _launcher_text()

        self.assertIn('set "ZJU_SEAT_HTTP_ENABLED=1"', launcher)


if __name__ == "__main__":
    unittest.main()
