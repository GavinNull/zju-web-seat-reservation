import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class FrontendContentTests(unittest.TestCase):
    def test_page_contains_complete_readable_controls(self) -> None:
        html = (
            ROOT / "src" / "seat_assistant" / "templates" / "index.html"
        ).read_text(encoding="utf-8")

        for label in (
            "浙大图书馆座位助手",
            "馆舍选择",
            "楼层选择",
            "分区选择",
            "单选",
            "多选",
            "指定座位",
            "排除座位",
            "屏幕通知",
            "自动提交总开关",
            "确认自动预约",
        ):
            self.assertIn(label, html)
        self.assertNotIn("SMTP", html)
        self.assertNotIn("邮件通知", html)
        self.assertNotIn("娴欏", html)
        self.assertRegex(html, r'/static/app\.js\?v=[^"]+')

    def test_javascript_calls_settings_apis(self) -> None:
        script = (
            ROOT / "src" / "seat_assistant" / "static" / "app.js"
        ).read_text(encoding="utf-8")

        self.assertIn("/api/settings/system", script)
        self.assertNotIn("/api/settings/smtp", script)
        self.assertNotIn("/api/settings/smtp/test", script)
        self.assertIn("seat_rules", script)
        self.assertIn("LOCATION_OPTIONS", script)
        self.assertIn("locationTargets", script)
        self.assertIn("renderLocationPickers", script)
        self.assertIn("自动提交总开关未开启", script)
        self.assertIn("任务仍处于观察模式", script)
        self.assertIn("发现空位后直接预约", script)
        self.assertIn("本任务会点击立即预约", script)
        self.assertIn("最近检测", script)
        self.assertIn("下次检测", script)
        self.assertIn("当前步骤", script)
        self.assertIn("检测轮次", script)
        self.assertIn("查看最近过程", script)
        self.assertIn("当前空余", script)
        self.assertIn("可用座位", script)
        self.assertIn("候选座位", script)
        self.assertIn("seat_status", script)
        self.assertIn("2000", script)

        html = (
            ROOT / "src" / "seat_assistant" / "templates" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn("每轮检测完成后会立即进入下一轮", html)
        self.assertIn('name="refresh_min_seconds" type="hidden"', html)

    def test_new_task_defaults_reservation_date_to_today(self) -> None:
        script = (
            ROOT / "src" / "seat_assistant" / "static" / "app.js"
        ).read_text(encoding="utf-8")

        self.assertIn("taskForm.elements.reservation_date.value = localDate(now)", script)
        self.assertNotIn("taskForm.elements.reservation_date.value = localDate(tomorrow)", script)

    def test_new_task_defaults_to_main_library_third_floor(self) -> None:
        script = (
            ROOT / "src" / "seat_assistant" / "static" / "app.js"
        ).read_text(encoding="utf-8")

        self.assertIn('locationSelection.venues = new Set(["主馆"])', script)
        self.assertIn('locationSelection.floors = new Set([locationKey("主馆", "三层")])', script)
        self.assertIn('locationKey("主馆", "三层", "三层东")', script)

    def test_page_uses_centered_app_shell_layout(self) -> None:
        html = (
            ROOT / "src" / "seat_assistant" / "templates" / "index.html"
        ).read_text(encoding="utf-8")
        styles = (
            ROOT / "src" / "seat_assistant" / "static" / "app.css"
        ).read_text(encoding="utf-8")

        self.assertIn('<body class="app-shell">', html)
        self.assertIn('<header class="hero">', html)
        self.assertIn("body.app-shell", styles)
        self.assertIn("header.hero", styles)
        self.assertIn("width: min(1280px, calc(100% - 48px))", styles)

    def test_launcher_bootstraps_environment(self) -> None:
        launcher = (ROOT / "启动助手.bat").read_text(encoding="utf-8")

        self.assertIn('cd /d "%~dp0"', launcher)
        self.assertIn("where py", launcher)
        self.assertIn("where python", launcher)
        self.assertIn("%PYTHON_CMD% -m venv .venv", launcher)
        self.assertIn("ensurepip --upgrade", launcher)
        self.assertIn('pip install -e ".[test]"', launcher)
        self.assertIn("playwright install chromium", launcher)
        self.assertIn("zju-seat-assistant.exe", launcher)

    def test_location_options_match_collected_site_data(self) -> None:
        script = (
            ROOT / "src" / "seat_assistant" / "static" / "app.js"
        ).read_text(encoding="utf-8")

        for collected_value in (
            "负一层书库",
            "301信息共享空间",
            "二层南",
            "三层北",
            "四层西",
            "五层东",
            "112李摩西阅览室",
            "207中文图书阅览室",
            "322中外文现刊阅览室",
        ):
            self.assertIn(collected_value, script)
