"""HTTP-first reservation adapter with browser-proxy fallback.

Scan (no cookies needed):
  POST /reserve/index/list  → area-level free counts
  POST /api/Seat/date       → segment IDs for the date
  POST /api/Seat/seat       → individual seat numbers + DB ids

Submit (needs browser cookies):
  page.evaluate(fetch('/api/Seat/confirm', {encrypted payload}))

AES-128-CBC encryption for the confirm payload:
  key = YYYYMMDD + reverse(YYYYMMDD)  (16 bytes)
  iv  = "ZZWBKJ_ZHIHUAWEI"           (16 bytes)
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx

from .domain import ReservationConfig, ReservationOutcome
from .engine import ScanResult
from .http_endpoints import HttpEndpointConfig, area_id
from .http_session import auth_body, auth_headers

CST = timezone(timedelta(hours=8))
AES_IV = "ZZWBKJ_ZHIHUAWEI".encode("utf-8")


class HttpAdapterUnavailable(RuntimeError):
    pass


class HttpLoginRequired(RuntimeError):
    pass


def _aes_key(dt: datetime) -> bytes:
    date_str = dt.strftime("%Y%m%d")
    return (date_str + date_str[::-1]).encode("utf-8")


def encrypt_payload(payload: dict[str, Any], dt: datetime | None = None) -> str:
    """AES-128-CBC encrypt a JSON payload for /api/Seat/confirm.

    Args:
        payload: Dict with 'seat_id' (str) and 'segment' (str).
        dt: China-time datetime for key derivation (default: now).
    """
    key = _aes_key(dt or datetime.now(CST))
    plaintext = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return base64.b64encode(_encrypt_aes_cbc(plaintext, key)).decode("ascii")


def _encrypt_aes_cbc(plaintext: bytes, key: bytes) -> bytes:
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
    except ImportError:
        return _encrypt_aes_cbc_with_cryptography(plaintext, key)
    cipher = AES.new(key, AES.MODE_CBC, iv=AES_IV)
    return cipher.encrypt(pad(plaintext, AES.block_size))


def _encrypt_aes_cbc_with_cryptography(plaintext: bytes, key: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise HttpAdapterUnavailable(
            "pycryptodome is required for reservation; pip install pycryptodome"
        )
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(AES_IV))
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


class HttpSessionAdapter:
    """Scan seats via pure HTTP using JWT bearer token.

    Query APIs (/reserve/*, /api/Seat/date, /api/Seat/seat) work with
    JWT token in Authorization header + body authorization field.
    No browser cookies needed.
    """

    def __init__(
        self,
        endpoints: HttpEndpointConfig,
        storage_state_provider: Callable[[], dict[str, Any] | None] | None = None,
        token_provider: Callable[[], str] | None = None,
        client: httpx.Client | None = None,
    ):
        self.endpoints = endpoints
        self.storage_state_provider = storage_state_provider
        self._token_provider = token_provider
        self.client = client or httpx.Client(timeout=httpx.Timeout(10.0))
        self._cached_token: str | None = None
        # Cache: display_number -> db_id from last scan
        self._seat_id_map: dict[int, str] = {}
        self._last_segment: str | None = None

    def check_login(self) -> bool:
        return bool(self._token)

    @property
    def _token(self) -> str:
        if self._cached_token is not None:
            return self._cached_token
        if self._token_provider is not None:
            self._cached_token = self._token_provider()
            return self._cached_token or ""
        return ""

    def invalidate_token(self) -> None:
        self._cached_token = None

    def scan(self, config: ReservationConfig) -> ScanResult:
        token = self._token
        if not token:
            raise HttpLoginRequired("no JWT token available")

        area = area_id(config.venue, config.floor, config.area)
        if area is None:
            raise HttpAdapterUnavailable(
                f"unknown area: {config.venue}/{config.floor}/{config.area}"
            )

        date_str = config.reservation_date.isoformat()
        segment, start_time, end_time = self._get_segment(
            token, area, date_str, config.time_slot
        )
        if segment is None:
            return ScanResult((), f"no segment found for {date_str} {config.time_slot}")

        self._last_segment = segment
        seats, seat_map = self._get_seats(
            token, area, segment, date_str, start_time, end_time
        )
        self._seat_id_map = seat_map

        if not seats:
            return ScanResult((), "no available seat")

        return ScanResult(tuple(sorted(set(seats))), "http scan")

    def submit(
        self, config: ReservationConfig, seat: int
    ) -> ReservationOutcome:
        raise HttpAdapterUnavailable(
            "submit requires browser proxy (cookies needed for /api/Seat/confirm)"
        )

    def verify_current_reservation(
        self, config: ReservationConfig, seat: int
    ) -> bool:
        token = self._token
        if not token:
            return False
        area = area_id(config.venue, config.floor, config.area)
        if area is None:
            return False
        date_str = config.reservation_date.isoformat()
        time_slot = config.time_slot
        start_time, end_time = (
            time_slot.split("-") if "-" in time_slot else ("08:00", "22:30")
        )
        segment, _, _ = self._get_segment(token, area, date_str, config.time_slot)
        if segment is None:
            return False
        _, seat_map = self._get_seats(
            token, area, segment, date_str, start_time, end_time
        )
        # Seat is reserved if it's NOT in the free list
        return seat not in {s for s in seat_map}

    def close(self) -> None:
        self.client.close()

    def _get_segment(
        self, token: str, area: str, date_str: str, time_slot: str
    ) -> tuple[str | None, str, str]:
        """Get segment ID and times for a date."""
        target_start = time_slot.split("-")[0] if "-" in time_slot else None
        try:
            response = self.client.post(
                self.endpoints.date_url,
                json=auth_body(token, build_id=area),
                headers=auth_headers(token),
            )
            payload = self._json(response)
            for day_data in payload.get("data", []):
                if day_data.get("day") == date_str:
                    for t in day_data.get("times", []):
                        if not isinstance(t, dict):
                            continue
                        t_start = t.get("start", "")
                        if target_start is None or t_start == target_start:
                            return t.get("id"), t_start, t.get("end", "")
                    # No exact match — return first segment
                    for t in day_data.get("times", []):
                        if isinstance(t, dict) and t.get("id"):
                            return t.get("id"), t.get("start", ""), t.get("end", "")
        except Exception:
            pass
        return None, "", ""

    def _get_seats(
        self,
        token: str,
        area: str,
        segment: str,
        date_str: str,
        start_time: str,
        end_time: str,
    ) -> tuple[list[int], dict[int, str]]:
        """Get available seats and a display_number->db_id map."""
        try:
            response = self.client.post(
                self.endpoints.seat_url,
                json=auth_body(
                    token,
                    area=area,
                    segment=segment,
                    day=date_str,
                    startTime=start_time,
                    endTime=end_time,
                ),
                headers=auth_headers(token),
            )
            payload = self._json(response)
            items = payload.get("data", [])
            if not isinstance(items, list):
                return [], {}

            seats: list[int] = []
            seat_map: dict[int, str] = {}
            for s in items:
                if not isinstance(s, dict):
                    continue
                if str(s.get("status")) != "1":
                    continue
                no = s.get("no", "")
                db_id = str(s.get("id", ""))
                number = _parse_seat_number(no)
                if number is not None:
                    seats.append(number)
                    if db_id:
                        seat_map[number] = db_id
            return seats, seat_map
        except Exception:
            return [], {}

    def _json(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code in {401, 403}:
            raise HttpLoginRequired("login required")
        if response.status_code == 429:
            raise HttpAdapterUnavailable("HTTP endpoint is rate limited")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise HttpAdapterUnavailable("HTTP response must be a JSON object")
        if _payload_requires_login(payload):
            raise HttpLoginRequired("login required")
        return payload


class BrowserProxySubmitAdapter:
    """Submit reservation through browser's cookie session.

    Opens a headless browser, loads the home page to establish cookies,
    then calls /api/Seat/confirm via page.evaluate(fetch(...)).
    """

    def __init__(
        self,
        browser_adapter: Any,
        endpoints: HttpEndpointConfig,
        token_provider: Callable[[], str] | None = None,
    ):
        self.browser_adapter = browser_adapter
        self.endpoints = endpoints
        self._token_provider = token_provider

    def submit(
        self, config: ReservationConfig, seat: int, seat_db_id: str, segment: str
    ) -> ReservationOutcome:
        token = self._token_provider() if self._token_provider else ""
        if not token:
            return ReservationOutcome.LOGIN_REQUIRED

        now = datetime.now(CST)
        inner = {"seat_id": seat_db_id, "segment": segment}
        encrypted = encrypt_payload(inner, now)

        return self.browser_adapter.submit_via_proxy(token, encrypted)

    def close(self) -> None:
        pass


class HybridReservationAdapter:
    """Two-layer reservation: HTTP scan + browser-proxy submit.

    Scan:  HTTP-first (fast, no browser DOM overhead). Falls back to
           browser DOM scan if HTTP fails.

    Submit: Browser proxy (page.evaluate fetch with cookies). Falls back
            to browser DOM submit (clicking UI) if proxy fails.
    """

    def __init__(
        self,
        browser_adapter: Any,
        http_adapter: HttpSessionAdapter,
        endpoints: HttpEndpointConfig,
        http_submit_enabled: bool = False,
    ):
        self.browser_adapter = browser_adapter
        self.http_adapter = http_adapter
        self.endpoints = endpoints
        self.http_submit_enabled = http_submit_enabled
        self._progress_reporter: Callable[
            [str, str, dict[str, object] | None], None
        ] | None = None
        self._browser_proxy: Any = None

    def set_progress_reporter(
        self,
        reporter: Callable[[str, str, dict[str, object] | None], None] | None,
    ) -> None:
        self._progress_reporter = reporter
        setter = getattr(self.browser_adapter, "set_progress_reporter", None)
        if callable(setter):
            setter(reporter)

    def check_login(self) -> bool:
        return bool(self.browser_adapter.check_login())

    def scan(self, config: ReservationConfig) -> ScanResult:
        try:
            self._progress("http_scan", "Scanning seats over HTTP")
            result = self.http_adapter.scan(config)
            self._progress(
                "scan_complete",
                "HTTP scan complete",
                {
                    "available_count": len(result.available_seats),
                    "available_seats": list(result.available_seats[:30]),
                    "message": result.message,
                },
            )
            return result
        except Exception as error:
            self._progress(
                "http_scan_fallback",
                "HTTP scan unavailable; falling back to browser",
                {"error": str(error)},
            )
            return self.browser_adapter.scan(config)

    def submit(
        self, config: ReservationConfig, seat: int
    ) -> ReservationOutcome:
        # Try browser-proxy submit (page.evaluate fetch)
        try:
            seat_db_id = self.http_adapter._seat_id_map.get(seat, "")
            segment = self.http_adapter._last_segment or ""
            if seat_db_id and segment:
                self._progress(
                    "http_submit",
                    "Submitting reservation via browser proxy",
                    {"seat": seat, "seat_id": seat_db_id, "segment": segment},
                )
                token = self.http_adapter._token
                if token:
                    now = datetime.now(CST)
                    inner = {"seat_id": seat_db_id, "segment": segment}
                    encrypted = encrypt_payload(inner, now)
                    outcome = self.browser_adapter.submit_via_proxy(
                        token, encrypted
                    )
                    if outcome is not ReservationOutcome.LOGIN_REQUIRED:
                        return outcome
        except Exception as error:
            self._progress(
                "http_submit_fallback",
                "Browser proxy submit failed; falling back to browser DOM",
                {"error": str(error), "seat": seat},
            )

        return self.browser_adapter.submit(config, seat)

    def verify_current_reservation(
        self, config: ReservationConfig, seat: int
    ) -> bool:
        try:
            return self.http_adapter.verify_current_reservation(config, seat)
        except Exception:
            pass
        return self.browser_adapter.verify_current_reservation(config, seat)

    def close(self) -> None:
        try:
            self.http_adapter.close()
        finally:
            self.browser_adapter.close()

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


def _parse_seat_number(label: str) -> int | None:
    """Extract numeric seat number from a label like 'Z3F272' or '272'.

    Seat labels follow the pattern: <building><floor>F<seat_number>
    e.g. 'Z3F272' -> 272. We use the rightmost digit group to skip
    the floor prefix digit.
    """
    import re
    groups = re.findall(r"\d+", str(label))
    if groups:
        return int(groups[-1])
    return None


def _payload_requires_login(payload: dict[str, Any]) -> bool:
    text = " ".join(
        str(payload.get(key, ""))
        for key in ("message", "msg", "error", "code", "status")
    ).casefold()
    return any(marker in text for marker in ("login", "unauthorized", "未登录"))
