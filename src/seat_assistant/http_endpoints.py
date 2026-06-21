"""Fixed ZJU library reservation API endpoints and area mappings.

All endpoints are POST with JSON body. Query endpoints (date/seat/list) work
with pure HTTP + JWT Authorization header. Confirm endpoint requires browser
cookie session (call through page.evaluate).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable


ZJU_BASE_URL = "https://booking.lib.zju.edu.cn"

# Fixed API paths (all POST)
CAS_USER = "/api/cas/user"           # CAS login -> JWT token
INDEX_LIST = "/reserve/index/list"   # Area list with availability counts
QUICK_SELECT = "/reserve/index/quickSelect"  # Quick building-level counts
SEAT_DATE = "/api/Seat/date"         # Segments for a date
SEAT_SEAT = "/api/Seat/seat"         # Seat list in area/segment
SEAT_CONFIRM = "/api/Seat/confirm"   # Reserve (needs browser cookies)
SEAT_MAP = "/api/seat/map"           # Seat map layout

# Area ID mapping. Each key is (venue, floor, area) -> area_id (str).
# 23 areas across 4 buildings.
AREA_MAP: dict[tuple[str, str, str], str] = {
    # 主馆 (58-67)
    ("主馆", "二层", "二层南"): "58",
    ("主馆", "二层", "二层北"): "59",
    ("主馆", "三层", "三层东"): "60",
    ("主馆", "三层", "三层南"): "61",
    ("主馆", "三层", "三层北"): "62",
    ("主馆", "四层", "四层东"): "63",
    ("主馆", "四层", "四层南"): "64",
    ("主馆", "四层", "四层西"): "65",
    ("主馆", "四层", "四层北"): "66",
    ("主馆", "五层", "五层东"): "67",
    # 基础馆 (38-40, 21)
    ("基础馆", "负一层", "负一层书库"): "38",
    ("基础馆", "一层", "一层书库"): "39",
    ("基础馆", "二层", "二层书库"): "40",
    ("基础馆", "三层", "301信息共享空间"): "21",
    # 农医馆 (47-52)
    ("农医馆", "一层", "112李摩西阅览室"): "47",
    ("农医馆", "二层", "207中文图书阅览室"): "48",
    ("农医馆", "二层", "209中文图书阅览室"): "49",
    ("农医馆", "二层", "211中文图书阅览室"): "50",
    ("农医馆", "三层", "320外文图书阅览室"): "51",
    ("农医馆", "三层", "322中外文现刊阅览室"): "52",
    # 玉泉馆 (7, 9, 11)
    ("玉泉馆", "三层", "三层300阅览室"): "7",
    ("玉泉馆", "四层", "四层401阅览室"): "9",
    ("玉泉馆", "五层", "五层501阅览室"): "11",
}


def area_id(venue: str, floor: str, area: str) -> str | None:
    """Look up the numeric area ID for a venue/floor/area triplet."""
    return AREA_MAP.get((venue, floor, area))


def all_venue_floor_area() -> Iterable[tuple[str, str, str]]:
    """All known (venue, floor, area) triples."""
    return AREA_MAP.keys()


def all_area_ids() -> Iterable[str]:
    """All known area IDs."""
    return AREA_MAP.values()


@dataclass(frozen=True)
class HttpEndpointConfig:
    base_url: str = ZJU_BASE_URL

    @property
    def list_url(self) -> str:
        return self._url(INDEX_LIST)

    @property
    def quick_select_url(self) -> str:
        return self._url(QUICK_SELECT)

    @property
    def date_url(self) -> str:
        return self._url(SEAT_DATE)

    @property
    def seat_url(self) -> str:
        return self._url(SEAT_SEAT)

    @property
    def confirm_url(self) -> str:
        return self._url(SEAT_CONFIRM)

    @property
    def cas_user_url(self) -> str:
        return self._url(CAS_USER)

    def scan_url(self) -> str | None:
        return self.seat_url

    def submit_url(self) -> str | None:
        return self.confirm_url

    def verify_url(self) -> str | None:
        return self.seat_url

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return self.base_url.rstrip("/") + path


def endpoints_from_environment() -> HttpEndpointConfig | None:
    enabled = os.getenv("ZJU_SEAT_HTTP_ENABLED", "").casefold()
    if enabled not in {"1", "true", "yes", "on"}:
        return None
    base_url = os.getenv("ZJU_SEAT_HTTP_BASE_URL", "").strip() or ZJU_BASE_URL
    return HttpEndpointConfig(base_url=base_url)
