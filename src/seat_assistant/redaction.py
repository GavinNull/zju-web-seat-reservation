"""Redaction helpers for locally stored diagnostics."""

from __future__ import annotations

import re

_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(token|cookie|authorization|webhook|password|secret)"
        r"(\s*[:=]\s*)(?:bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_LONG_ID = re.compile(r"(?<!\d)\d{15,18}[0-9Xx]?(?!\d)")


def redact_text(value: str) -> str:
    """Remove common secrets and direct personal identifiers from text."""
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(_replace_secret, redacted)
    redacted = _EMAIL.sub("[REDACTED_EMAIL]", redacted)
    redacted = _PHONE.sub("[REDACTED_PHONE]", redacted)
    redacted = _LONG_ID.sub("[REDACTED_ID]", redacted)
    return redacted


def _replace_secret(match: re.Match[str]) -> str:
    if match.lastindex and match.lastindex >= 2:
        return f"{match.group(1)}{match.group(2)}[REDACTED]"
    return "[REDACTED]"

