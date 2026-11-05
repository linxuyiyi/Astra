"""Shared UID resolution for Nexus and platform maintenance requests."""

from urllib.parse import unquote

from fastapi import Cookie, Header, HTTPException


def resolve_uid(verified_uid: str | None, owner_id: str | None, cookie: str | None) -> str:
    # The UI writes encodeURIComponent(uid) to the cookie; headers are already literal.
    value = (verified_uid or owner_id or unquote(cookie or "")).strip()
    if not value:
        raise HTTPException(status_code=401, detail="缺少 X-Verified-UID")
    return value


def request_uid(
    verified_uid: str | None = Header(default=None, alias="X-Verified-UID"),
    owner_id: str | None = Header(default=None, alias="X-Owner-Id"),
    nexus_uid: str | None = Cookie(default=None, alias="nexus_uid"),
) -> str:
    return resolve_uid(verified_uid, owner_id, nexus_uid)
