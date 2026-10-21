"""Shared UID resolution for Nexus and platform maintenance requests."""

from urllib.parse import unquote

from fastapi import HTTPException


def resolve_uid(verified_uid: str | None, owner_id: str | None, cookie: str | None) -> str:
    # The UI writes encodeURIComponent(uid) to the cookie; headers are already literal.
    value = (verified_uid or owner_id or unquote(cookie or "")).strip()
    if not value:
        raise HTTPException(status_code=401, detail="缺少 X-Verified-UID")
    return value
