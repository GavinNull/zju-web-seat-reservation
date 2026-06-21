"""Playwright adapter for the ZJU library reservation website.

All unstable page knowledge is isolated in this module. The defaults prefer
visible Chinese text. CSS selectors can be overridden after inspecting the
authenticated page without changing scheduler or domain code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .domain import (
    ReservationConfig,
    ReservationOutcome,
    classify_result_message,
)
from .engine import ScanResult


HOME_URL = "https://booking.lib.zju.edu.cn/h5/index.html#/home"
SEAT_SELECT_URL = (
    "https://booking.lib.zju.edu.cn/h5/index.html"
    "#/SeatScreening/1/seatSelect"
)
LOGIN_READY_TIMEOUT_MS = 8000
LOGIN_POLL_INTERVAL_MS = 500
FILTER_PANEL_TIMEOUT_MS = 8000
FILTER_PANEL_POLL_INTERVAL_MS = 500
SEAT_SCAN_TIMEOUT_SECONDS = 25
SEAT_PROBE_CLICK_TIMEOUT_MS = 800
LOGIN_REDIRECT_SETTLE_TIMEOUT_MS = 10000
LOGIN_REDIRECT_POLL_INTERVAL_MS = 500
RESERVATION_PANEL_TIMEOUT_SECONDS = 30
_SEAT_NUMBER = re.compile(r"(?<!\d)(\d{1,4})(?!\d)")
_SELECTED_SEAT_VALUE = re.compile(r"已选座位号")
_SELECTED_SEAT_NUMBER = re.compile(r"已选座位号")


@dataclass(frozen=True)
class SelectorConfig:
    seat_entry_text: str = "座位预约"
    seat_entry_fallback_texts: tuple[str, ...] = ("座位预约", "普通座位")
    list_mode_text: str = "列表模式"
    submit_text: str = "立即预约"
    success_text: str = "预约成功"
    current_reservation_text: str = "当前预约"
    login_markers: tuple[str, ...] = (
        "我的中心",
    )
    room_item_css: str = ".roomItem"
    seat_item_css: str = (
        ".absolute, [data-seat-no], .seat-item, .seat-list-item"
    )
    available_css: str = (
        "[data-status='available'], .available, .can-select, "
        ":not(.disabled):not(.occupied)"
    )
    selected_css: str = ".selected, [aria-selected='true']"


def parse_seat_number(label: str) -> int | None:
    match = _SEAT_NUMBER.search(label)
    return int(match.group(1)) if match else None


def parse_selected_seat_number(body_text: str) -> int | None:
    match = _SELECTED_SEAT_VALUE.search("".join(body_text.split()))
    if not match:
        return None
    digit_groups = re.findall(r"\d{1,4}", match.string[match.end() : match.end() + 24])
    return int(digit_groups[-1]) if digit_groups else None


def _is_closed(page: Any) -> bool:
    try:
        closed = page.is_closed()
    except AttributeError:
        return False
    except Exception:
        return True
    return closed is True


def _text_count(page: Any, marker: str) -> int:
    try:
        count = page.get_by_text(marker, exact=False).count()
    except Exception:
        return 0
    return count if isinstance(count, int) else 0


def _is_login_url(url: str) -> bool:
    lowered = url.casefold()
    return any(marker in lowered for marker in ("login", "sso", "cas", "pageverify"))


def _window_args(background_window: bool, headless: bool) -> list[str] | None:
    if headless:
        return None
    if background_window:
        return ["--window-position=-32000,-32000", "--window-size=1280,900"]
    return ["--window-position=120,80", "--window-size=1280,900"]


def area_card_matches(
    card_text: str, venue: str, floor: str, area: str
) -> bool:
    lines = {line.strip() for line in card_text.splitlines() if line.strip()}
    return {venue, floor, area}.issubset(lines)


def single_area_card_matches(
    card_text: str, venue: str, floor: str, area: str
) -> bool:
    if not area_card_matches(card_text, venue, floor, area):
        return False
    return card_text.count("预约") <= 1


class PlaywrightAdapter:
    def __init__(
        self,
        profile_directory: Path,
        diagnostics_directory: Path,
        selectors: SelectorConfig | None = None,
        headless: bool = True,
        background_window: bool = False,
    ):
        self.profile_directory = Path(profile_directory)
        self.diagnostics_directory = Path(diagnostics_directory)
        self.selectors = selectors or SelectorConfig()
        self.headless = headless
        self.background_window = background_window
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._current_area_key: tuple[str, str, str, str] | None = None
        self._progress_reporter: Callable[
            [str, str, dict[str, object] | None], None
        ] | None = None

    def set_progress_reporter(
        self,
        reporter: Callable[[str, str, dict[str, object] | None], None] | None,
    ) -> None:
        self._progress_reporter = reporter

    def _progress(
        self,
        stage: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        if self._progress_reporter is None:
            return
        try:
            self._progress_reporter(stage, message, details)
        except Exception:
            return

    def open_for_login(self) -> None:
        page = self._ensure_page()
        page.goto(HOME_URL, wait_until="domcontentloaded")
        page.bring_to_front()

    def check_login(self, navigate: bool = True) -> bool:
        page = self._ensure_page()
        if navigate:
            page.goto(HOME_URL, wait_until="domcontentloaded")
        body_text = ""
        attempts = max(1, LOGIN_READY_TIMEOUT_MS // LOGIN_POLL_INTERVAL_MS)
        for _ in range(attempts):
            page = self._ensure_page()
            body_text = ""
            try:
                body_text = page.locator("body").inner_text()
            except Exception:
                pass
            if not isinstance(body_text, str):
                body_text = ""
            recognized = any(
                marker in body_text or _text_count(page, marker) > 0
                for marker in self.selectors.login_markers
            )
            if recognized:
                return True
            page.wait_for_timeout(LOGIN_POLL_INTERVAL_MS)
        self._save_diagnostic("login-check-failed", body_text=body_text)
        return False

    def scan(self, config: ReservationConfig) -> ScanResult:
        page = self._ensure_page()
        try:
            area_key = self._area_key(config)
            if self._current_area_key == area_key:
                self._progress(
                    "refreshing_seat_page",
                    "Refreshing current seat page",
                    {"area": config.area},
                )
                self._reload_current_seat_page()
                if not self._is_valid_seat_page(config):
                    try:
                        self._progress(
                            "opening_seat_map",
                            "Opening seat map after refresh",
                            {
                                "venue": config.venue,
                                "floor": config.floor,
                                "area": config.area,
                            },
                        )
                        self._detail_reservation_button().click()
                        self._wait_for_valid_seat_page(config, timeout_ms=6000)
                    except Exception:
                        pass
                if not self._is_valid_seat_page(config):
                    self._progress(
                        "seat_page_lost",
                        "Seat page was lost after refresh; reopening area",
                        {"area": config.area},
                    )
                    self._open_area(config)
            else:
                self._open_area(config)
                self._current_area_key = area_key
            if not self._is_valid_seat_page(config):
                self._progress(
                    "seat_page_invalid",
                    "Current page is not a valid seat page",
                    self._page_state_details(config),
                )
                raise RuntimeError("valid seat page was not reached")
            self._progress(
                "reading_seats",
                "Reading seat availability",
                self._page_state_details(config),
            )
            seats = self._scan_current_seat_page()
            if seats:
                return ScanResult(tuple(sorted(set(seats))))
            return ScanResult((), "no available seat")
        except Exception:
            body_text = ""
            try:
                body_text = page.locator("body").inner_text()
            except Exception:
                pass
            self._save_diagnostic("scan-error", body_text=body_text)
            raise

    def _scan_current_seat_page(self) -> list[int]:
        page = self._ensure_page()
        seats: list[int] = []
        items = page.locator(self.selectors.seat_item_css)
        deadline = datetime.now().timestamp() + SEAT_SCAN_TIMEOUT_SECONDS
        for index in range(items.count()):
            if datetime.now().timestamp() >= deadline:
                if seats:
                    return seats
                raise TimeoutError("seat scan timed out")
            item = items.nth(index)
            if self._seat_unavailable(item):
                continue
            number = self._explicit_seat_number(item)
            if number is None:
                number = self._probe_seat_number(item)
            if number is not None:
                seats.append(number)
        return seats

    def _refresh_current_seat_page(self, config: ReservationConfig) -> None:
        page = self._ensure_page()
        wait_ms = int(max(1.0, config.refresh_min_seconds) * 1000)
        page.wait_for_timeout(wait_ms)
        self._reload_current_seat_page()
        heading = f"{config.venue}-{config.floor}-{config.area}"
        if not page.get_by_text(heading, exact=True).count():
            self._open_area(config)

    def _reload_current_seat_page(self) -> None:
        page = self._ensure_page()
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(500)

    def _wait_for_valid_seat_page(
        self, config: ReservationConfig, timeout_ms: int = 5000
    ) -> bool:
        page = self._ensure_page()
        attempts = max(1, timeout_ms // FILTER_PANEL_POLL_INTERVAL_MS)
        for _ in range(attempts):
            if self._is_valid_seat_page(config):
                return True
            page.wait_for_timeout(FILTER_PANEL_POLL_INTERVAL_MS)
        return False

    def _is_valid_seat_page(self, config: ReservationConfig) -> bool:
        page = self._ensure_page()
        url = getattr(page, "url", "")
        if "SeatScreening/1/seatSelect" not in url:
            return False
        heading = f"{config.venue}-{config.floor}-{config.area}"
        try:
            if page.get_by_text(heading, exact=True).count():
                return True
        except Exception:
            pass
        try:
            return page.locator(self.selectors.seat_item_css).count() > 0
        except Exception:
            return False

    def _page_state_details(self, config: ReservationConfig) -> dict[str, object]:
        page = self._ensure_page()
        url = getattr(page, "url", "")
        detail_heading = f"{config.venue}-{config.floor}-{config.area}"
        try:
            heading_visible = bool(page.get_by_text(detail_heading, exact=True).count())
        except Exception:
            heading_visible = False
        try:
            seat_item_count = page.locator(self.selectors.seat_item_css).count()
        except Exception:
            seat_item_count = 0
        return {
            "area": config.area,
            "url": url,
            "is_valid_seat_page": self._is_valid_seat_page(config),
            "detail_heading_visible": heading_visible,
            "seat_item_count": seat_item_count,
        }

    def submit(
        self, config: ReservationConfig, seat: int
    ) -> ReservationOutcome:
        page = self._ensure_page()
        try:
            self._select_seat(seat)
            after = page.locator("body").inner_text()
            if parse_selected_seat_number(after) != seat:
                raise RuntimeError("selected seat could not be verified")
            page.get_by_role(
                "button", name=self.selectors.submit_text, exact=True
            ).click()
            page.wait_for_timeout(1000)
            body_text = page.locator("body").inner_text()
            return classify_result_message(body_text)
        except Exception:
            self._save_diagnostic("submit-error")
            raise

    def extract_token(self) -> str | None:
        """Get the JWT bearer token from the browser's sessionStorage.

        Loads the home page (which triggers /api/cas/user) and reads the
        token that the frontend stores after the CAS exchange.
        """
        page = self._ensure_page()
        page.goto(HOME_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        token = page.evaluate("() => sessionStorage.getItem('token') || ''")
        return token.strip() or None

    def submit_via_proxy(
        self, auth: str, encrypted_payload: str
    ) -> ReservationOutcome:
        """Submit reservation through the browser's cookie session.

        Calls /api/Seat/confirm via page.evaluate(fetch(...)), bypassing
        DOM interaction. The browser's cookie session satisfies the
        confirm endpoint's second authentication layer.

        Args:
            auth: Normalized bearer token string (e.g. "bearer<JWT>").
            encrypted_payload: AES-CBC encrypted Base64 JSON payload.

        Returns:
            ReservationOutcome based on the API response code.
        """
        page = self._ensure_page()
        # Navigate to same-origin page to establish cookie session
        if "booking.lib.zju.edu.cn" not in (getattr(page, "url", "") or ""):
            page.goto(HOME_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

        result = page.evaluate(
            f"""
                async () => {{
                    const r = await fetch('/api/Seat/confirm', {{
                        method: 'POST',
                        headers: {{
                            'Content-Type': 'application/json',
                            'Authorization': '{auth}',
                            'X-Requested-With': 'XMLHttpRequest'
                        }},
                        body: JSON.stringify({{
                            aesjson: '{encrypted_payload}',
                            authorization: '{auth}'
                        }})
                    }});
                    return await r.json();
                }}
            """
        )
        if not isinstance(result, dict):
            return ReservationOutcome.FAILURE

        code = result.get("code")
        msg = str(result.get("msg", result.get("message", "")))

        if code == 1:
            return ReservationOutcome.SUCCESS
        if code == 10001 or "未登录" in msg or "登录" in msg:
            return ReservationOutcome.LOGIN_REQUIRED
        if "已有" in msg or "重复" in msg:
            return ReservationOutcome.ALREADY_RESERVED
        if "请选择" in msg:
            return ReservationOutcome.FAILURE
        return ReservationOutcome.AMBIGUOUS

    def verify_current_reservation(
        self, config: ReservationConfig, seat: int
    ) -> bool:
        page = self._ensure_page()
        page.goto(HOME_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        body = page.locator("body").inner_text()
        return (
            self.selectors.current_reservation_text in body
            and str(seat) in body
            and config.area in body
        )

    def export_storage_state(self) -> dict[str, Any]:
        if self._context is None:
            self._ensure_page()
        if self._context is None:
            return {"cookies": [], "origins": []}
        return self._context.storage_state()

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._page = self._context = self._playwright = None

    def _ensure_page(self) -> Any:
        if self._page is not None and not _is_closed(self._page):
            return self._page
        if self._context is not None:
            for page in reversed(self._context.pages):
                if not _is_closed(page):
                    self._page = page
                    return self._page
            self._page = self._context.new_page()
            return self._page
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RuntimeError(
                "Playwright is not installed; install the project and run "
                '`playwright install chromium`'
            ) from error
        self.profile_directory.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        args = _window_args(self.background_window, self.headless)
        self._context = self._playwright.chromium.launch_persistent_context(
            str(self.profile_directory),
            headless=self.headless,
            viewport={"width": 1280, "height": 900},
            args=args,
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self._page

    def _open_area(self, config: ReservationConfig) -> None:
        page = self._ensure_page()
        page.set_viewport_size({"width": 1280, "height": 900})
        self._open_seat_reservation_panel(config)
        page = self._ensure_page()
        self._raise_if_login_page(page)

        self._progress(
            "selecting_filters",
            "Selecting venue, floor and area",
            {
                "venue": config.venue,
                "floor": config.floor,
                "area": config.area,
            },
        )
        self._clear_location_filters()
        self._raise_if_login_page(page)
        self._select_date_filter(config)
        self._raise_if_login_page(page)
        self._expand_filter("馆舍", config.venue)
        self._left_option(config.venue).click()
        page.wait_for_timeout(600)
        self._raise_if_login_page(page)
        if config.floor:
            self._expand_filter("楼层", config.floor)
            self._left_option(config.floor).click()
            page.wait_for_timeout(900)
            self._raise_if_login_page(page)

        self._progress(
            "opening_area_detail",
            "Opening selected area detail",
            {"venue": config.venue, "floor": config.floor, "area": config.area},
        )
        self._click_room_card(config)
        self._raise_if_login_page(page)

        detail_heading = f"{config.venue}-{config.floor}-{config.area}"
        if not page.get_by_text(detail_heading, exact=True).count():
            raise RuntimeError(f"area detail did not open: {detail_heading}")
        self._progress(
            "opening_seat_map",
            "Opening seat map",
            {"venue": config.venue, "floor": config.floor, "area": config.area},
        )
        self._detail_reservation_button().click()
        page.wait_for_timeout(1200)
        self._raise_if_login_page(page)
        if not self._wait_for_valid_seat_page(config, timeout_ms=6000):
            raise RuntimeError("seat map did not open")

    def _area_key(self, config: ReservationConfig) -> tuple[str, str, str, str]:
        return (
            config.reservation_date.isoformat(),
            config.venue,
            config.floor,
            config.area,
        )

    def _open_seat_reservation_panel(
        self, config: ReservationConfig | None = None
    ) -> None:
        page = self._ensure_page()
        url = SEAT_SELECT_URL
        if config is not None:
            url = f"{url}?date={config.reservation_date.isoformat()}"
        deadline = datetime.now().timestamp() + RESERVATION_PANEL_TIMEOUT_SECONDS
        self._progress(
            "opening_reservation_page",
            "Opening reservation filter page",
            {"attempt": 1},
        )
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        self._raise_if_login_page(page)
        self._progress(
            "waiting_filter_panel",
            "Waiting for filter panel",
            {"attempt": 1},
        )
        if self._wait_for_left_option("馆舍", timeout_ms=5000):
            return

        self._progress(
            "opening_reservation_page",
            "Opening reservation page from home",
            {"attempt": 1},
        )
        page.goto(HOME_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        while datetime.now().timestamp() < deadline:
            self._raise_if_login_page(page)
            if self._wait_for_left_option("馆舍", timeout_ms=1000):
                return
            if self._home_is_authenticating(page):
                page.wait_for_timeout(FILTER_PANEL_POLL_INTERVAL_MS)
                continue
            self._progress(
                "waiting_filter_panel",
                "Waiting for reservation entry",
            )
            for text in self.selectors.seat_entry_fallback_texts:
                entry = self._visible_text(text)
                if entry is None:
                    continue
                entry.click()
                if self._wait_for_left_option("馆舍", timeout_ms=3000):
                    return
                break
            page.wait_for_timeout(FILTER_PANEL_POLL_INTERVAL_MS)
        raise RuntimeError("seat reservation entry did not open filter panel")

    def _home_is_authenticating(self, page: Any | None = None) -> bool:
        page = page or self._ensure_page()
        try:
            body_text = page.locator("body").inner_text()
        except Exception:
            return False
        if not isinstance(body_text, str):
            return False
        return any(marker in body_text for marker in ("正在认证", "正在加载"))

    def _wait_for_left_option(
        self,
        text: str,
        timeout_ms: int = FILTER_PANEL_TIMEOUT_MS,
    ) -> bool:
        page = self._ensure_page()
        attempts = max(1, timeout_ms // FILTER_PANEL_POLL_INTERVAL_MS)
        for _ in range(attempts):
            if self._left_option_or_none(text) is not None:
                return True
            page.wait_for_timeout(FILTER_PANEL_POLL_INTERVAL_MS)
        return False

    def _visible_text(self, text: str) -> Any | None:
        matches = self._ensure_page().get_by_text(text, exact=True)
        for index in range(matches.count()):
            item = matches.nth(index)
            try:
                if item.is_visible():
                    return item
            except Exception:
                continue
        return None

    def _left_option(self, text: str) -> Any:
        item = self._left_option_or_none(text)
        if item is not None:
            return item
        raise RuntimeError(f"left filter option not found: {text}")

    def _left_option_or_none(self, text: str) -> Any | None:
        page = self._ensure_page()
        self._raise_if_login_page(page)
        matches = page.get_by_text(text, exact=True)
        for index in range(matches.count()):
            item = matches.nth(index)
            box = item.bounding_box(timeout=800)
            if box and box["x"] < 320:
                return item
        return None

    def _raise_if_login_page(self, page: Any | None = None) -> None:
        page = page or self._ensure_page()
        url = getattr(page, "url", "")
        if not _is_login_url(url):
            return
        attempts = max(
            1, LOGIN_REDIRECT_SETTLE_TIMEOUT_MS // LOGIN_REDIRECT_POLL_INTERVAL_MS
        )
        for _ in range(attempts):
            page.wait_for_timeout(LOGIN_REDIRECT_POLL_INTERVAL_MS)
            if not _is_login_url(getattr(page, "url", "")):
                return
        raise RuntimeError("login required")

    def _expand_filter(self, heading: str, option: str) -> None:
        try:
            self._left_option(option)
        except RuntimeError:
            self._left_option(heading).click()
            self._ensure_page().wait_for_timeout(300)

    def _clear_location_filters(self) -> None:
        page = self._ensure_page()
        for _ in range(30):
            active = page.locator("div.selectItem.active")
            target = None
            for index in range(active.count()):
                item = active.nth(index)
                box = item.bounding_box(timeout=800)
                if box and box["x"] < 320:
                    target = item
                    break
            if target is None:
                return
            target.click()
            page.wait_for_timeout(120)

    def _select_date_filter(self, config: ReservationConfig) -> None:
        date_text = config.reservation_date.isoformat()
        try:
            self._expand_filter("日期", date_text)
            self._left_option(date_text).click()
            self._ensure_page().wait_for_timeout(600)
        except RuntimeError:
            return

    def _room_card(
        self, config: ReservationConfig, timeout_seconds: float = 12
    ) -> Any:
        page = self._ensure_page()
        deadline = datetime.now().timestamp() + timeout_seconds
        while datetime.now().timestamp() < deadline:
            cards = page.locator(self.selectors.room_item_css)
            for index in range(cards.count()):
                card = cards.nth(index)
                if not card.is_visible():
                    continue
                try:
                    card_text = card.inner_text(timeout=1000)
                except Exception:
                    continue
                if single_area_card_matches(
                    card_text, config.venue, config.floor, config.area
                ):
                    return card
            page.wait_for_timeout(300)
        raise RuntimeError(
            "area card not found: "
            f"{config.venue}/{config.floor}/{config.area}"
        )

    def _click_room_card(self, config: ReservationConfig) -> None:
        page = self._ensure_page()
        deadline = datetime.now().timestamp() + 15
        last_error: Exception | None = None
        while datetime.now().timestamp() < deadline:
            try:
                card = self._room_card(config, timeout_seconds=2)
                card.scroll_into_view_if_needed()
                self._click_room_card_entry(card)
                page.wait_for_timeout(700)
                return
            except Exception as error:
                last_error = error
                page.wait_for_timeout(500)
        raise RuntimeError(
            "area card could not be clicked: "
            f"{config.venue}/{config.floor}/{config.area}"
        ) from last_error

    def _click_room_card_entry(self, card: Any) -> None:
        card.click(timeout=3000)

    def _detail_reservation_button(self) -> Any:
        candidates: list[tuple[float, Any]] = []
        for text in (self.selectors.submit_text, "预约"):
            buttons = self._ensure_page().get_by_role(
                "button", name=text, exact=True
            )
            for index in range(buttons.count()):
                button = buttons.nth(index)
                box = button.bounding_box(timeout=800)
                if not box or not button.is_visible():
                    continue
                if box["x"] < 650:
                    continue
                candidates.append((box["x"] + box["y"], button))
        if candidates:
            return max(candidates, key=lambda item: item[0])[1]
        raise RuntimeError("right-side reservation button not found")

    def _seat_locator(self, seat: int) -> Any:
        return self._select_seat(seat)

    def _select_seat(self, seat: int) -> Any:
        items = self._ensure_page().locator(self.selectors.seat_item_css)
        for index in range(items.count()):
            item = items.nth(index)
            if self._seat_unavailable(item):
                continue
            number = self._explicit_seat_number(item)
            if number == seat:
                item.click(timeout=SEAT_PROBE_CLICK_TIMEOUT_MS)
                self._ensure_page().wait_for_timeout(500)
                if self._current_selected_seat_number() == seat:
                    return item
                continue
            if number is not None:
                continue
            number = self._probe_seat_number(item)
            if number == seat:
                return item
        raise RuntimeError(f"seat {seat} is no longer visible")

    def _seat_unavailable(self, item: Any) -> bool:
        class_name = item.get_attribute("class") or ""
        status = item.get_attribute("data-status") or ""
        disabled = item.get_attribute("aria-disabled") == "true"
        if disabled or status in {"occupied", "disabled", "unavailable"}:
            return True
        return any(
            word in class_name
            for word in ("occupied", "disabled", "noSelectV2")
        )

    def _explicit_seat_number(self, item: Any) -> int | None:
        return parse_seat_number(
            item.get_attribute("data-seat-no") or item.inner_text()
        )

    def _probe_seat_number(self, item: Any) -> int | None:
        try:
            item.click(timeout=SEAT_PROBE_CLICK_TIMEOUT_MS)
        except Exception:
            return None
        self._ensure_page().wait_for_timeout(300)
        return self._current_selected_seat_number()

    def _current_selected_seat_number(self) -> int | None:
        return parse_selected_seat_number(
            self._ensure_page().locator("body").inner_text()
        )

    def _save_diagnostic(self, prefix: str, body_text: str | None = None) -> None:
        if self._page is None:
            return
        self.diagnostics_directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        screenshot_path = self.diagnostics_directory / f"{prefix}-{timestamp}.png"
        try:
            self._page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            pass
        if body_text is None:
            return
        try:
            title = self._page.title()
        except Exception:
            title = ""
        try:
            url = self._page.url
        except Exception:
            url = ""
        text_path = self.diagnostics_directory / f"{prefix}-{timestamp}.txt"
        text_path.write_text(
            "\n".join(
                [
                    f"url: {url}",
                    f"title: {title}",
                    "",
                    body_text[:5000],
                ]
            ),
            encoding="utf-8",
        )
