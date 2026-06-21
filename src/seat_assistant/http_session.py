"""Utilities for turning Playwright storage state into HTTP session inputs.

Handles two auth layers for the ZJU library API:
1. JWT token (from sessionStorage) — for query APIs (/reserve/*, /api/seat/*)
2. Browser cookies (from Playwright storage state) — for confirm API (/api/Seat/*)
"""

from __future__ import annotations

from http.cookiejar import Cookie, CookieJar
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request


SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-csrf-token",
    "x-xsrf-token",
}


def normalize_token(raw: str) -> str:
    """Ensure the token string has a 'bearer' prefix.

    The ZJU API stores tokens with or without the bearer prefix.
    All API calls need the prefix in both the Authorization header
    and the body authorization field.
    """
    token = raw.strip()
    if token.lower().startswith("bearer"):
        if token[:6].lower() == "bearer":
            return f"bearer{token[6:]}"
        return token
    return f"bearer{token}"


def auth_headers(token: str) -> dict[str, str]:
    """Standard HTTP headers for ZJU API POST requests."""
    return {
        "Content-Type": "application/json",
        "Authorization": normalize_token(token),
        "X-Requested-With": "XMLHttpRequest",
    }


def auth_body(token: str, **extra: Any) -> dict[str, Any]:
    """Standard POST body with authorization field."""
    return {"authorization": normalize_token(token), **extra}


def extract_token_via_browser(profile_dir: str) -> str | None:
    """Open a headless browser, load the home page, and extract the JWT token
    from sessionStorage.

    The token is set by the page's /api/cas/user call on load.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            profile_dir,
            headless=True,
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(
            "https://booking.lib.zju.edu.cn/h5/index.html#/home",
            wait_until="domcontentloaded",
        )
        page.wait_for_timeout(3000)
        token = page.evaluate("() => sessionStorage.getItem('token') || ''")
        ctx.close()
    return token.strip() or None


def cookie_jar_from_storage_state(
    storage_state: dict[str, Any] | None,
    base_url: str,
) -> CookieJar:
    jar = CookieJar()
    if not storage_state:
        return jar
    host = urlparse(base_url).hostname or ""
    for item in storage_state.get("cookies", []):
        name = str(item.get("name", "")).strip()
        value = str(item.get("value", ""))
        domain = str(item.get("domain") or host)
        if not name:
            continue
        jar.set_cookie(
            Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=bool(domain),
                domain_initial_dot=domain.startswith("."),
                path=str(item.get("path") or "/"),
                path_specified=True,
                secure=bool(item.get("secure", True)),
                expires=item.get("expires") if item.get("expires", -1) != -1 else None,
                discard=item.get("expires", -1) == -1,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            )
        )
    return jar


def cookie_header_from_storage_state(
    storage_state: dict[str, Any] | None,
    base_url: str,
) -> str:
    jar = cookie_jar_from_storage_state(storage_state, base_url)
    request = Request(base_url)
    jar.add_cookie_header(request)
    return request.get_header("Cookie", "")


def redacted_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        key: ("<redacted>" if key.casefold() in SENSITIVE_HEADER_NAMES else value)
        for key, value in headers.items()
    }
