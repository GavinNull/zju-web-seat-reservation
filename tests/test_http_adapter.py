import unittest
from datetime import date, datetime
from unittest.mock import patch

import httpx

from seat_assistant.domain import ReservationConfig, ReservationOutcome, SeatRule
from seat_assistant.engine import ScanResult
from seat_assistant.http_adapter import (
    HttpAdapterUnavailable,
    HttpLoginRequired,
    HttpSessionAdapter,
    HybridReservationAdapter,
    encrypt_payload,
)
from seat_assistant.http_endpoints import HttpEndpointConfig


def config(observation_mode: bool = True) -> ReservationConfig:
    return ReservationConfig(
        name="Morning",
        venue="\u4e3b\u9986",
        floor="\u4e09\u5c42",
        area="\u4e09\u5c42\u5317",
        reservation_date=date(2026, 6, 15),
        time_slot="08:30-22:30",
        starts_at=datetime(2026, 6, 14, 7, 59, 50),
        stops_at=datetime(2026, 6, 14, 8, 10),
        seat_rules=(SeatRule(priority=1, start=80, end=100),),
        observation_mode=observation_mode,
    )


ENDPOINTS = HttpEndpointConfig(base_url="https://booking.example")


class BrowserFallback:
    def __init__(self) -> None:
        self.scans = 0
        self.submits: list[int] = []
        self.verifications: list[int] = []
        self.closed = False
        self._proxy_submits: list[tuple[str, str]] = []

    def check_login(self) -> bool:
        return True

    def scan(self, _config: ReservationConfig) -> ScanResult:
        self.scans += 1
        return ScanResult((88,), "browser")

    def submit(self, _config: ReservationConfig, seat: int) -> ReservationOutcome:
        self.submits.append(seat)
        return ReservationOutcome.SUCCESS

    def verify_current_reservation(
        self, _config: ReservationConfig, seat: int
    ) -> bool:
        self.verifications.append(seat)
        return True

    def extract_token(self) -> str | None:
        return "bearer-test-jwt-token"

    def submit_via_proxy(
        self, auth: str, encrypted_payload: str
    ) -> ReservationOutcome:
        self._proxy_submits.append((auth, encrypted_payload))
        return ReservationOutcome.SUCCESS

    def close(self) -> None:
        self.closed = True


def _date_response() -> dict:
    return {
        "code": 1,
        "data": [
            {
                "day": "2026-06-15",
                "times": [
                    {"id": "1592185", "start": "08:30", "end": "22:30"},
                ],
            }
        ],
    }


def _seat_response(seats: list[dict] | None = None) -> dict:
    if seats is None:
        seats = [
            {"no": "Z3F091", "id": "6501", "status": "1"},
            {"no": "Z3F092", "id": "6502", "status": "1"},
            {"no": "Z3F093", "id": "6503", "status": "0"},
        ]
    return {"code": 1, "data": seats}


class HttpAdapterTests(unittest.TestCase):
    def test_http_scan_returns_available_seats_from_real_api_format(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            body = request.read().decode()
            if "/api/Seat/date" in str(request.url):
                return httpx.Response(200, json=_date_response())
            if "/api/Seat/seat" in str(request.url):
                return httpx.Response(200, json=_seat_response())
            return httpx.Response(404)

        adapter = HttpSessionAdapter(
            ENDPOINTS,
            token_provider=lambda: "bearer-test-token",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        result = adapter.scan(config())

        self.assertEqual(result.available_seats, (91, 92))
        self.assertIn("http", result.message)

        # Verify the seat API was called with correct POST body
        seat_call = [r for r in requests if "/api/Seat/seat" in str(r.url)]
        self.assertTrue(seat_call)
        body = seat_call[0].read().decode()
        self.assertIn("62", body)  # area_id for 主馆/三层/三层北
        self.assertIn("1592185", body)  # segment

    def test_scan_fails_without_token(self) -> None:
        adapter = HttpSessionAdapter(ENDPOINTS)
        with self.assertRaises(HttpLoginRequired):
            adapter.scan(config())

    def test_scan_returns_empty_for_unknown_area(self) -> None:
        adapter = HttpSessionAdapter(
            ENDPOINTS,
            token_provider=lambda: "bearer-test",
        )
        cfg = ReservationConfig(
            name="Test",
            venue="Unknown",
            floor="X",
            area="Y",
            reservation_date=date(2026, 6, 15),
            time_slot="08:00-22:00",
            starts_at=datetime(2026, 6, 14, 7, 59, 50),
            stops_at=datetime(2026, 6, 14, 8, 10),
            seat_rules=(SeatRule(priority=1, start=1, end=999),),
        )
        with self.assertRaises(HttpAdapterUnavailable):
            adapter.scan(cfg)

    def test_hybrid_scan_falls_back_to_browser_when_http_fails(self) -> None:
        browser = BrowserFallback()
        adapter = HybridReservationAdapter(
            browser,
            HttpSessionAdapter(ENDPOINTS),
            ENDPOINTS,
        )

        result = adapter.scan(config())

        self.assertEqual(result.available_seats, (88,))
        self.assertEqual(browser.scans, 1)

    def test_hybrid_submit_uses_browser_proxy_when_seat_map_available(self) -> None:
        browser = BrowserFallback()

        # Pre-populate the HTTP adapter's seat map and segment
        http = HttpSessionAdapter(
            ENDPOINTS,
            token_provider=lambda: "bearer-test",
        )
        http._seat_id_map = {91: "6501"}
        http._last_segment = "1592185"
        http._cached_token = "bearer-test"

        adapter = HybridReservationAdapter(browser, http, ENDPOINTS)

        outcome = adapter.submit(config(observation_mode=False), 91)

        self.assertEqual(outcome, ReservationOutcome.SUCCESS)
        self.assertEqual(len(browser._proxy_submits), 1)
        self.assertEqual(browser.submits, [])

    def test_hybrid_submit_falls_back_to_browser_dom(self) -> None:
        browser = BrowserFallback()
        http = HttpSessionAdapter(ENDPOINTS, token_provider=lambda: "bearer-test")
        # No seat map populated -> proxy submit won't work, falls back to DOM

        adapter = HybridReservationAdapter(browser, http, ENDPOINTS)

        outcome = adapter.submit(config(observation_mode=False), 88)

        self.assertEqual(outcome, ReservationOutcome.SUCCESS)
        self.assertEqual(browser.submits, [88])

    def test_close_closes_browser_and_http_client(self) -> None:
        browser = BrowserFallback()
        client = httpx.Client(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200))
        )
        adapter = HybridReservationAdapter(
            browser,
            HttpSessionAdapter(ENDPOINTS, client=client),
            ENDPOINTS,
        )

        adapter.close()

        self.assertTrue(browser.closed)
        self.assertTrue(client.is_closed)


class EncryptPayloadTests(unittest.TestCase):
    def test_encrypt_produces_base64_output(self) -> None:
        payload = {"seat_id": "99999", "segment": "1592185"}
        result = encrypt_payload(payload)
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_encrypt_is_deterministic_for_same_datetime(self) -> None:
        payload = {"seat_id": "6525", "segment": "1592185"}
        dt = datetime(2026, 6, 22, 8, 0, 0)
        first = encrypt_payload(payload, dt)
        second = encrypt_payload(payload, dt)
        self.assertEqual(first, second)

    def test_encrypt_differs_by_date(self) -> None:
        payload = {"seat_id": "6525", "segment": "1592185"}
        dt1 = datetime(2026, 6, 21, 8, 0, 0)
        dt2 = datetime(2026, 6, 22, 8, 0, 0)
        first = encrypt_payload(payload, dt1)
        second = encrypt_payload(payload, dt2)
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
