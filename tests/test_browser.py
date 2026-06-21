import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from seat_assistant.browser import (
    PlaywrightAdapter,
    SelectorConfig,
    _window_args,
    area_card_matches,
    parse_seat_number,
    parse_selected_seat_number,
    single_area_card_matches,
)


class BrowserHelpersTests(unittest.TestCase):
    def test_parses_seat_number_from_visible_label(self) -> None:
        self.assertEqual(parse_seat_number("座位 095 可预约"), 95)
        self.assertEqual(parse_seat_number("12号座"), 12)
        self.assertIsNone(parse_seat_number("暂无空闲座位"))

    def test_selector_config_has_safe_text_defaults(self) -> None:
        selectors = SelectorConfig()
        self.assertEqual(selectors.submit_text, "立即预约")
        self.assertEqual(selectors.success_text, "预约成功")
        self.assertIn(".roomItem", selectors.room_item_css)
        self.assertIn(".absolute", selectors.seat_item_css)

    def test_reservation_adapter_defaults_to_background_browser(self) -> None:
        adapter = PlaywrightAdapter("profile", "diagnostics")

        self.assertTrue(adapter.headless)

    def test_visible_login_browser_uses_stable_window_position(self) -> None:
        self.assertEqual(
            _window_args(background_window=False, headless=False),
            ["--window-position=120,80", "--window-size=1280,900"],
        )

    def test_background_browser_stays_offscreen(self) -> None:
        self.assertEqual(
            _window_args(background_window=True, headless=False),
            ["--window-position=-32000,-32000", "--window-size=1280,900"],
        )

    def test_headless_browser_does_not_need_window_args(self) -> None:
        self.assertIsNone(_window_args(background_window=True, headless=True))

    def test_matches_room_card_by_complete_location(self) -> None:
        text = "基础馆\n二层书库\n二层\n座 位 160空闲 159\n预约"
        self.assertTrue(
            area_card_matches(text, "基础馆", "二层", "二层书库")
        )
        self.assertFalse(
            area_card_matches(text, "基础馆", "三层", "二层书库")
        )

    def test_rejects_parent_room_container_with_multiple_reservation_buttons(self) -> None:
        text = (
            "主馆\n三层东\n三层\n预约\n"
            "主馆\n三层南\n三层\n预约\n"
            "主馆\n三层北\n三层\n预约"
        )
        self.assertFalse(single_area_card_matches(text, "主馆", "三层", "三层北"))

    def test_parses_selected_seat_number_from_page_feedback(self) -> None:
        self.assertEqual(parse_selected_seat_number("已选座位号：055"), 55)
        self.assertEqual(parse_selected_seat_number("已选座位号: 12"), 12)
        self.assertIsNone(parse_selected_seat_number("已选座位号：-"))

    def test_parses_prefixed_selected_seat_number_from_page_feedback(self) -> None:
        self.assertEqual(parse_selected_seat_number("已选座位号：Z3F234"), 234)

    def test_unlabeled_absolute_seat_is_resolved_by_click_feedback(self) -> None:
        page = FakeSeatPage([FakeSeatElement("", "absolute", "已选座位号：055")])
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._page = page

        item = adapter._seat_locator(55)

        self.assertIs(item, page.items[0])
        self.assertEqual(page.items[0].clicks, 1)
        self.assertEqual(page.items[0].last_click_timeout, 800)

    def test_unlabeled_unclickable_seat_probe_is_skipped(self) -> None:
        page = FakeSeatPage(
            [
                FakeSeatElement("", "absolute", "", click_error=RuntimeError("covered")),
                FakeSeatElement("", "absolute", "已选座位号：055"),
            ]
        )
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._page = page

        item = adapter._seat_locator(55)

        self.assertIs(item, page.items[1])
        self.assertEqual(page.items[0].clicks, 1)
        self.assertEqual(page.items[1].clicks, 1)

    def test_scan_returns_partial_candidates_when_deadline_hits_after_found_seat(self) -> None:
        page = FakeDeadlineSeatPage(
            [
                FakeSeatElement("", "absolute", "已选座位号：Z3F243"),
                FakeSeatElement("", "absolute", "已选座位号：Z3F244"),
            ]
        )
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._page = page
        adapter._open_area = Mock()
        FakeScanClock.values = [100.0, 101.0, 130.0]

        with patch("seat_assistant.browser.datetime", FakeScanClock):
            result = adapter.scan(Mock())

        self.assertEqual(result.available_seats, (243,))
        self.assertEqual(result.message, "")

    def test_scan_returns_no_seat_without_waiting_for_internal_refresh(self) -> None:
        page = FakeRefreshingSeatPage(
            [
                [],
                [FakeSeatElement("", "absolute", "已选座位号：Z3F243")],
            ]
        )
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._page = page
        adapter._open_area = Mock()
        config = Mock(
            venue="主馆",
            floor="三层",
            area="三层北",
            stops_at=datetime(2099, 1, 1, 0, 0, 0),
            refresh_min_seconds=8,
        )

        result = adapter.scan(config)

        self.assertEqual(result.available_seats, ())
        self.assertEqual(result.message, "no available seat")
        self.assertEqual(page.reloads, 0)
        adapter._open_area.assert_called_once()

    def test_repeated_scan_keeps_same_seat_page_and_reloads_before_reading(self) -> None:
        page = FakeRefreshingSeatPage(
            [
                [],
                [FakeSeatElement("243", "absolute", "")],
            ]
        )
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._page = page
        adapter._open_area = Mock(side_effect=lambda _config: page.mark_area_open())
        config = Mock(
            venue="主馆",
            floor="三层",
            area="三层北",
            stops_at=datetime(2099, 1, 1, 0, 0, 0),
            refresh_min_seconds=8,
        )

        first = adapter.scan(config)
        second = adapter.scan(config)

        self.assertEqual(first.available_seats, ())
        self.assertEqual(second.available_seats, (243,))
        self.assertEqual(page.reloads, 1)
        adapter._open_area.assert_called_once()

    def test_scan_reopens_area_when_cached_page_is_not_seat_page(self) -> None:
        page = FakeHomeInsteadOfSeatPage()
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._page = page
        config = Mock(
            reservation_date=datetime(2026, 6, 19).date(),
            venue="主馆",
            floor="三层",
            area="三层北",
            stops_at=datetime(2099, 1, 1, 0, 0, 0),
            refresh_min_seconds=8,
        )
        adapter._current_area_key = adapter._area_key(config)
        adapter._open_area = Mock(side_effect=lambda _config: page.mark_seat_page())

        result = adapter.scan(config)

        self.assertEqual(result.available_seats, (243,))
        self.assertEqual(page.reloads, 1)
        adapter._open_area.assert_called_once_with(config)

    def test_repeated_scan_accepts_seat_page_when_heading_is_missing(self) -> None:
        page = FakeSeatPageWithoutHeading(
            [
                [FakeSeatElement("242", "absolute", "")],
                [FakeSeatElement("243", "absolute", "")],
            ]
        )
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._page = page
        adapter._open_area = Mock(side_effect=lambda _config: page.mark_area_open())
        config = Mock(
            venue="主馆",
            floor="三层",
            area="三层北",
            reservation_date=datetime(2026, 6, 19).date(),
            stops_at=datetime(2099, 1, 1, 0, 0, 0),
            refresh_min_seconds=8,
        )

        first = adapter.scan(config)
        second = adapter.scan(config)

        self.assertEqual(first.available_seats, (242,))
        self.assertEqual(second.available_seats, (243,))
        self.assertEqual(page.reloads, 1)
        adapter._open_area.assert_called_once()

    def test_repeated_scan_clicks_reservation_button_after_refreshing_to_detail_page(
        self,
    ) -> None:
        page = FakeDetailAfterRefreshPage()
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._page = page
        adapter._current_area_key = (
            "2026-06-19",
            "主馆",
            "三层",
            "三层北",
        )
        adapter._open_area = Mock()
        config = Mock(
            venue="主馆",
            floor="三层",
            area="三层北",
            reservation_date=datetime(2026, 6, 19).date(),
            stops_at=datetime(2099, 1, 1, 0, 0, 0),
            refresh_min_seconds=8,
        )

        result = adapter.scan(config)

        self.assertEqual(result.available_seats, (243,))
        self.assertEqual(page.reloads, 1)
        self.assertEqual(page.reservation_button.clicks, 1)
        adapter._open_area.assert_not_called()

    def test_scan_does_not_read_seat_items_until_page_is_valid_seat_select(
        self,
    ) -> None:
        page = FakeSeatLikeButInvalidPage()
        with tempfile.TemporaryDirectory() as directory:
            adapter = PlaywrightAdapter("profile", Path(directory) / "diagnostics")
            adapter._page = page
            adapter._open_area = Mock()
            config = Mock(
                venue="Main",
                floor="Third",
                area="North",
                reservation_date=datetime(2026, 6, 19).date(),
                stops_at=datetime(2099, 1, 1, 0, 0, 0),
                refresh_min_seconds=8,
            )

            with self.assertRaisesRegex(RuntimeError, "valid seat page"):
                adapter.scan(config)

        adapter._open_area.assert_called_once_with(config)

    def test_room_card_skips_unreadable_cards(self) -> None:
        page = FakeRoomPage(
            [
                FakeRoomCard(RuntimeError("detached card")),
                FakeRoomCard("基础馆\n二层书库\n二层\n预约"),
            ]
        )
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._page = page
        config = Mock(venue="基础馆", floor="二层", area="二层书库")

        card = adapter._room_card(config)

        self.assertIs(card, page.cards[1])

    def test_room_card_click_retries_when_toast_blocks_pointer(self) -> None:
        page = FakeRoomPage(
            [
                FakeRoomCard(
                    "基础馆\n二层书库\n二层\n预约",
                    click_errors=[RuntimeError("intercepts pointer events")],
                )
            ]
        )
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._page = page
        config = Mock(venue="基础馆", floor="二层", area="二层书库")

        adapter._click_room_card(config)

        self.assertEqual(page.cards[0].clicks, 2)

    def test_room_card_click_selects_exact_area_card_not_inner_button(self) -> None:
        target = FakeRoomCardWithButton("主馆\n三层北\n三层\n预约")
        page = FakeRoomPage(
            [
                FakeRoomCardWithButton("主馆\n三层东\n三层\n预约"),
                target,
            ]
        )
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._page = page
        config = Mock(venue="主馆", floor="三层", area="三层北")

        adapter._click_room_card(config)

        self.assertEqual(target.button.clicks, 0)
        self.assertEqual(target.clicks, 1)

    def test_room_card_skips_parent_container_and_clicks_exact_area(self) -> None:
        parent = FakeRoomCardWithButton(
            "主馆\n三层东\n三层\n预约\n主馆\n三层北\n三层\n预约"
        )
        target = FakeRoomCardWithButton("主馆\n三层北\n三层\n预约")
        page = FakeRoomPage([parent, target])
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._page = page
        config = Mock(venue="主馆", floor="三层", area="三层北")

        adapter._click_room_card(config)

        self.assertEqual(parent.button.clicks, 0)
        self.assertEqual(target.button.clicks, 0)
        self.assertEqual(target.clicks, 1)

    def test_detail_reservation_button_prefers_bottom_right_submit_button(self) -> None:
        page = FakeDetailButtonPage()
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._page = page

        button = adapter._detail_reservation_button()
        button.click()

        self.assertEqual(page.card_button.clicks, 0)
        self.assertEqual(page.bottom_button.clicks, 1)

    def test_select_date_filter_clicks_requested_date_when_available(self) -> None:
        page = FakeDateFilterPage()
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._page = page
        config = Mock(reservation_date=datetime(2026, 6, 19).date())

        adapter._select_date_filter(config)

        self.assertIn("2026-06-19", page.clicked)

    def test_seat_reservation_panel_clicks_home_entry_when_direct_route_stays_on_home(self) -> None:
        page = FakeSeatEntryPage()
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._page = page

        adapter._open_seat_reservation_panel()

        self.assertTrue(page.entry_clicked)
        self.assertEqual(page.state, "filters")

    def test_seat_reservation_panel_waits_for_slow_home_entry_transition(self) -> None:
        page = SlowSeatEntryPage(wait_cycles_after_click=25)
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._page = page

        adapter._open_seat_reservation_panel()

        self.assertEqual(page.state, "filters")
        self.assertEqual(page.goto.call_count, 2)

    def test_seat_reservation_panel_opens_requested_reservation_date(self) -> None:
        page = FakeFilterPanelPage()
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._page = page
        config = Mock(reservation_date=datetime(2026, 6, 19).date())

        adapter._open_seat_reservation_panel(config)

        self.assertIn("date=2026-06-19", page.goto.call_args.args[0])

    def test_login_poll_does_not_navigate_or_refresh_current_page(self) -> None:
        page = Mock()
        page.url = "https://zjuam.zju.edu.cn/cas/login"
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._page = page

        self.assertFalse(adapter.check_login(navigate=False))
        page.goto.assert_not_called()

    def test_login_check_accepts_authenticated_platform_page_text(self) -> None:
        page = FakeLoginPage(
            "https://booking.lib.zju.edu.cn/h5/index.html#/home",
            "浙江大学图书馆预约平台\n预约规则\n首页\n我的中心\n座位预约",
        )
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._page = page

        self.assertTrue(adapter.check_login(navigate=False))

    def test_login_check_waits_for_authenticated_redirect(self) -> None:
        page = TransitioningLoginPage(
            [
                (
                    "https://booking.lib.zju.edu.cn/h5/index.html#/login",
                    "",
                ),
                (
                    "https://booking.lib.zju.edu.cn/h5/index.html#/home",
                    "浙江大学图书馆预约平台\n我的中心\n座位预约",
                ),
            ]
        )
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._page = page

        self.assertTrue(adapter.check_login(navigate=False))
        page.goto.assert_not_called()

    def test_login_check_uses_current_page_when_original_login_page_closed(self) -> None:
        closed_page = FakeLoginPage(
            "https://zjuam.zju.edu.cn/cas/login",
            "",
            closed=True,
        )
        current_page = FakeLoginPage(
            "https://booking.lib.zju.edu.cn/h5/index.html#/home",
            "浙江大学图书馆预约平台\n我的中心\n座位预约",
        )
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._page = closed_page
        adapter._context = FakeContext([closed_page, current_page])

        self.assertTrue(adapter.check_login(navigate=False))
        self.assertIs(adapter._page, current_page)

    def test_login_check_rejects_generic_platform_shell_without_account_marker(self) -> None:
        page = FakeLoginPage(
            "https://booking.lib.zju.edu.cn/h5/index.html#/home",
            "浙江大学图书馆预约平台\n预约规则\n首页\n图书馆主页",
        )
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._page = page

        self.assertFalse(adapter.check_login(navigate=False))

    def test_login_check_rejects_seat_entry_without_account_marker(self) -> None:
        page = FakeLoginPage(
            "https://booking.lib.zju.edu.cn/h5/index.html#/home",
            "浙江大学图书馆预约平台\n座位预约\n预约规则\n首页",
        )
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._page = page

        self.assertFalse(adapter.check_login(navigate=False))

    def test_login_check_saves_diagnostic_when_authenticated_page_is_unrecognized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = FakeLoginPage(
                "https://booking.lib.zju.edu.cn/h5/index.html#/home",
                "unexpected authenticated shell",
            )
            adapter = PlaywrightAdapter("profile", directory)
            adapter._page = page

            self.assertFalse(adapter.check_login(navigate=False))

            diagnostics = list(Path(directory).glob("login-check-failed-*.txt"))
            self.assertEqual(len(diagnostics), 1)
            self.assertIn(page.url, diagnostics[0].read_text(encoding="utf-8"))
            self.assertEqual(len(page.screenshots), 1)

    def test_export_storage_state_returns_browser_context_state(self) -> None:
        state = {"cookies": [{"name": "SESSION", "value": "abc"}]}
        adapter = PlaywrightAdapter("profile", "diagnostics")
        adapter._context = FakeStorageContext(state)

        self.assertEqual(adapter.export_storage_state(), state)


if __name__ == "__main__":
    unittest.main()


class FakeSeatElement:
    def __init__(
        self,
        label: str,
        class_name: str,
        selection_text: str,
        click_error: Exception | None = None,
    ):
        self.label = label
        self.class_name = class_name
        self.selection_text = selection_text
        self.click_error = click_error
        self.page = None
        self.clicks = 0
        self.last_click_timeout = None

    def get_attribute(self, name: str) -> str | None:
        if name == "class":
            return self.class_name
        if name == "data-seat-no":
            return self.label
        if name == "aria-disabled":
            return "false"
        return None

    def inner_text(self) -> str:
        return self.label

    def click(self, **kwargs) -> None:
        self.clicks += 1
        self.last_click_timeout = kwargs.get("timeout")
        if self.click_error is not None:
            raise self.click_error
        self.page.body_text = self.selection_text


class FakeLocatorList:
    def __init__(self, items):
        self.items = items

    def count(self) -> int:
        return len(self.items)

    def nth(self, index: int):
        return self.items[index]


class FakeBodyLocator:
    def __init__(self, page):
        self.page = page

    def inner_text(self) -> str:
        return self.page.body_text


class FakeTextLocator:
    def __init__(self, found: bool):
        self.found = found

    def count(self) -> int:
        return 1 if self.found else 0


class FakeLoginPage:
    def __init__(self, url: str, body_text: str, closed: bool = False):
        self.url = url
        self.body_text = body_text
        self.closed = closed
        self.goto = Mock()
        self.screenshots = []

    def is_closed(self) -> bool:
        return self.closed

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def locator(self, selector: str):
        if selector == "body":
            return FakeBodyLocator(self)
        return FakeLocatorList([])

    def get_by_text(self, marker: str, exact: bool = False):
        return FakeTextLocator(marker in self.body_text)

    def screenshot(self, path: str, full_page: bool = True) -> None:
        self.screenshots.append((path, full_page))


class FakeContext:
    def __init__(self, pages):
        self.pages = pages

    def new_page(self):
        page = FakeLoginPage("", "")
        self.pages.append(page)
        return page


class FakeStorageContext(FakeContext):
    def __init__(self, state):
        super().__init__([FakeLoginPage("", "")])
        self.state = state

    def storage_state(self):
        return self.state


class TransitioningLoginPage(FakeLoginPage):
    def __init__(self, states):
        self.states = list(states)
        super().__init__(*self.states[0])

    def wait_for_timeout(self, _milliseconds: int) -> None:
        if len(self.states) > 1:
            self.states.pop(0)
            self.url, self.body_text = self.states[0]


class FakeClickableText:
    def __init__(self, page, text: str, x: int = 80):
        self.page = page
        self.text = text
        self.x = x

    def bounding_box(self, **_kwargs):
        return {"x": self.x, "y": 20, "width": 80, "height": 24}

    def is_visible(self) -> bool:
        return True

    def click(self, **_kwargs) -> None:
        if hasattr(self.page, "clicked"):
            self.page.clicked.append(self.text)
        if self.text == "座位预约":
            self.page.entry_clicked = True
            self.page.state = "filters"
            self.page.url = "https://booking.lib.zju.edu.cn/h5/index.html#/SeatScreening/1/seatSelect"


class FakeSeatEntryPage:
    def __init__(self):
        self.state = "home"
        self.url = "https://booking.lib.zju.edu.cn/h5/index.html#/home"
        self.entry_clicked = False
        self.goto = Mock(side_effect=self._goto)

    def _goto(self, _url: str, **_kwargs) -> None:
        self.url = "https://booking.lib.zju.edu.cn/h5/index.html#/home"
        self.state = "home"

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def get_by_text(self, text: str, exact: bool = False):
        if text == "座位预约" and self.state == "home":
            return FakeLocatorList([FakeClickableText(self, text, x=650)])
        if text == "馆舍" and self.state == "filters":
            return FakeLocatorList([FakeClickableText(self, text, x=40)])
        return FakeLocatorList([])


class SlowSeatEntryPage(FakeSeatEntryPage):
    def __init__(self, wait_cycles_after_click: int):
        super().__init__()
        self.wait_cycles_after_click = wait_cycles_after_click
        self.waits_after_click = 0

    def _goto(self, _url: str, **_kwargs) -> None:
        self.url = "https://booking.lib.zju.edu.cn/h5/index.html#/home"
        self.state = "home"
        self.entry_clicked = False
        self.waits_after_click = 0

    def wait_for_timeout(self, _milliseconds: int) -> None:
        if not self.entry_clicked:
            return
        self.waits_after_click += 1
        if self.waits_after_click >= self.wait_cycles_after_click:
            self.state = "filters"
            self.url = "https://booking.lib.zju.edu.cn/h5/index.html#/SeatScreening/1/seatSelect"

    def get_by_text(self, text: str, exact: bool = False):
        if text == "座位预约" and self.state == "home":
            return FakeLocatorList([SlowClickableText(self, text, x=650)])
        if text == "馆舍" and self.state == "filters":
            return FakeLocatorList([FakeClickableText(self, text, x=40)])
        return FakeLocatorList([])


class SlowClickableText(FakeClickableText):
    def click(self, **_kwargs) -> None:
        if hasattr(self.page, "clicked"):
            self.page.clicked.append(self.text)
        if self.text == "座位预约":
            self.page.entry_clicked = True


class FakeFilterPanelPage:
    def __init__(self):
        self.url = ""
        self.goto = Mock(side_effect=self._goto)

    def _goto(self, url: str, **_kwargs) -> None:
        self.url = url

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def get_by_text(self, text: str, exact: bool = False):
        if text == "馆舍":
            return FakeLocatorList([FakeClickableText(self, text, x=40)])
        return FakeLocatorList([])


class FakeDateFilterPage:
    def __init__(self):
        self.clicked = []

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def get_by_text(self, text: str, exact: bool = False):
        if text in {"日期", "2026-06-19"}:
            return FakeLocatorList([FakeClickableText(self, text, x=40)])
        return FakeLocatorList([])


class FakeSeatPage:
    def __init__(self, items):
        if not hasattr(self, "url"):
            self.url = (
                "https://booking.lib.zju.edu.cn/h5/index.html"
                "#/SeatScreening/1/seatSelect"
            )
        self.items = items
        self.body_text = "已选座位号：-"
        for item in self.items:
            item.page = self

    def locator(self, selector: str):
        if selector == "body":
            return FakeBodyLocator(self)
        return FakeLocatorList(self.items)

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None


class FakeDeadlineSeatPage(FakeSeatPage):
    pass


class FakeRefreshingSeatPage(FakeSeatPage):
    def __init__(self, item_sets):
        self.item_sets = item_sets
        self.reloads = 0
        self.area_open = False
        self.url = (
            "https://booking.lib.zju.edu.cn/h5/index.html"
            "#/SeatScreening/1/seatSelect?date=2026-06-19&area=1"
        )
        super().__init__(self.item_sets[0])

    def mark_area_open(self) -> None:
        self.area_open = True
        self.url = (
            "https://booking.lib.zju.edu.cn/h5/index.html"
            "#/SeatScreening/1/seatSelect?date=2026-06-19&area=1"
        )

    def reload(self, **_kwargs) -> None:
        self.reloads += 1
        next_index = min(self.reloads, len(self.item_sets) - 1)
        self.items = self.item_sets[next_index]
        self.body_text = "已选座位号：-"
        for item in self.items:
            item.page = self

    def get_by_text(self, text: str, exact: bool = False):
        return FakeTextLocator(text == "主馆-三层-三层北")


class FakeSeatPageWithoutHeading(FakeRefreshingSeatPage):
    def get_by_text(self, text: str, exact: bool = False):
        return FakeTextLocator(False)


class FakeDetailAfterRefreshPage(FakeSeatPage):
    def __init__(self):
        self.reloads = 0
        self.waits_after_click = 0
        self.reservation_button = FakeRoomButton()
        self.url = (
            "https://booking.lib.zju.edu.cn/h5/index.html"
            "#/SeatScreening/1/seatSelect?date=2026-06-19&area=1"
        )
        super().__init__([])

    def reload(self, **_kwargs) -> None:
        self.reloads += 1

    def wait_for_timeout(self, _milliseconds: int) -> None:
        if self.reservation_button.clicks:
            self.waits_after_click += 1

    def locator(self, selector: str):
        if selector == "body":
            return FakeBodyLocator(self)
        if self.reservation_button.clicks and self.waits_after_click >= 2:
            item = FakeSeatElement("243", "absolute", "")
            item.page = self
            return FakeLocatorList([item])
        return FakeLocatorList([])

    def get_by_text(self, text: str, exact: bool = False):
        return FakeTextLocator(False)

    def get_by_role(self, role: str, name: str, exact: bool = True):
        if role == "button":
            return FakeLocatorList([self.reservation_button])
        return FakeLocatorList([])


class FakeHomeInsteadOfSeatPage(FakeSeatPage):
    def __init__(self):
        self.reloads = 0
        self.url = "https://booking.lib.zju.edu.cn/h5/index.html#/home?"
        self.seat_page = False
        super().__init__([])

    def mark_seat_page(self) -> None:
        self.seat_page = True
        self.url = (
            "https://booking.lib.zju.edu.cn/h5/index.html"
            "#/SeatScreening/1/seatSelect?date=2026-06-19&area=1"
        )
        self.items = [FakeSeatElement("243", "absolute", "")]
        for item in self.items:
            item.page = self

    def reload(self, **_kwargs) -> None:
        self.reloads += 1

    def get_by_text(self, text: str, exact: bool = False):
        return FakeTextLocator(self.seat_page and text == "主馆-三层-三层北")


class FakeSeatLikeButInvalidPage(FakeSeatPage):
    def __init__(self):
        self.url = "https://booking.lib.zju.edu.cn/h5/index.html#/home"
        super().__init__([FakeSeatElement("243", "absolute", "")])

    def get_by_text(self, text: str, exact: bool = False):
        return FakeTextLocator(False)

    def screenshot(self, path: str, full_page: bool = True) -> None:
        return None


class FakeTimestamp:
    def __init__(self, value: float):
        self.value = value

    def timestamp(self) -> float:
        return self.value


class FakeScanClock:
    values = [100.0, 101.0, 130.0]

    @classmethod
    def now(cls):
        value = cls.values.pop(0) if cls.values else 130.0
        return FakeTimestamp(value)


class FakeRoomCard:
    def __init__(self, text_or_error, click_errors=None):
        self.text_or_error = text_or_error
        self.click_errors = list(click_errors or [])
        self.clicks = 0

    def is_visible(self) -> bool:
        return True

    def inner_text(self, **_kwargs) -> str:
        if isinstance(self.text_or_error, Exception):
            raise self.text_or_error
        return self.text_or_error

    def scroll_into_view_if_needed(self) -> None:
        return None

    def click(self, **_kwargs) -> None:
        self.clicks += 1
        if self.click_errors:
            raise self.click_errors.pop(0)


class FakeRoomButton:
    def __init__(self, x: int = 900, y: int = 100):
        self.clicks = 0
        self.x = x
        self.y = y

    def is_visible(self) -> bool:
        return True

    def bounding_box(self, **_kwargs):
        return {"x": self.x, "y": self.y, "width": 80, "height": 40}

    def click(self, **_kwargs) -> None:
        self.clicks += 1


class FakeDetailButtonPage:
    def __init__(self):
        self.card_button = FakeRoomButton(x=900, y=100)
        self.bottom_button = FakeRoomButton(x=1100, y=820)

    def get_by_role(self, role: str, name: str, exact: bool = True):
        if role != "button":
            return FakeLocatorList([])
        if name == "预约":
            return FakeLocatorList([self.card_button])
        if name == "立即预约":
            return FakeLocatorList([self.bottom_button])
        return FakeLocatorList([])


class FakeRoomCardWithButton(FakeRoomCard):
    def __init__(self, text_or_error):
        super().__init__(text_or_error)
        self.button = FakeRoomButton()

    def get_by_role(self, role: str, name: str, exact: bool = True):
        if role == "button" and name == "预约":
            return FakeLocatorList([self.button])
        return FakeLocatorList([])


class FakeRoomPage:
    def __init__(self, cards):
        self.cards = cards

    def locator(self, _selector: str):
        return FakeLocatorList(self.cards)

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None
