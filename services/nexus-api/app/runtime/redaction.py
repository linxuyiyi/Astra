"""Minimal recursive redaction for persisted maintenance events and errors."""

from __future__ import annotations

import re
from typing import Any

_PATTERNS = (
    (re.compile(r"agw_[A-Za-z0-9_-]+"), "[已隐藏]"),
    (re.compile(r"wtk_[A-Za-z0-9_-]+"), "[已隐藏]"),
    (re.compile(r"Bearer\s+\S+", re.IGNORECASE), "Bearer [已隐藏]"),
    (re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key)\s*[=:]\s*[^\s,;]+"), r"\1=[已隐藏]"),
    (re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1[已隐藏]@"),
)


def redact_text(value: Any, secrets: tuple[str, ...] | list[str] = ()) -> str:
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "[已隐藏]")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_data(value: Any, secrets: tuple[str, ...] | list[str] = ()) -> Any:
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, dict):
        return {str(key): redact_data(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_data(item, secrets) for item in value]
    return value
