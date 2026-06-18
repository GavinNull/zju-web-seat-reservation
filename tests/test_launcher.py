import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class LauncherTests(unittest.TestCase):
    def test_launcher_starts_service_and_opens_console(self) -> None:
        launcher = (ROOT / "启动助手.bat").read_text(encoding="utf-8")

        self.assertIn("%~dp0", launcher)
        self.assertIn("zju-seat-assistant.exe", launcher)
        self.assertIn("/api/health", launcher)
        self.assertIn("http://127.0.0.1:8765", launcher)
        self.assertIn("Service is already running", launcher)


if __name__ == "__main__":
    unittest.main()
