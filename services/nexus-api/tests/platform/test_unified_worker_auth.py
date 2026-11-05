"""统一服务必须保留 Worker 协议鉴权，而非要求浏览器 Cookie。"""

import httpx
import pytest

from nexus_api.service import create_service


@pytest.mark.asyncio
async def test_task_wake_lookup_authenticates_as_service(monkeypatch):
    from app.config.settings import get_settings
    from app.models import TaskWakeOutbox
    from app.runtime.task_wakeup import HttpWakeSender, WakeRetry

    monkeypatch.setenv("NEXUS_API_BASE", "http://test")
    monkeypatch.setenv("NEXUS_INTERNAL_KEY", "qa-internal-key")
    get_settings.cache_clear()
    row = TaskWakeOutbox(
        origin_employee_id="no-such-employee", source="opencode", external_task_id="no-session"
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_service()), base_url="http://test"
        ) as client:
            # The source lookup must pass service auth before reporting a missing source.
            with pytest.raises(WakeRetry):
                await HttpWakeSender(client).send(row)
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_library_audit_identity_records_temporary_uid():
    from tests.nexus.clients import session_headers

    headers = session_headers("qa-library-reviewer")
    headers.update({"X-Astra-User-Id": "forged", "X-Astra-Role": "admin"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_service()), base_url="http://test"
    ) as client:
        response = await client.get("/api/library/permissions", headers=headers)
        assert response.status_code == 200
        assert response.json()["user_id"] == "qa-library-reviewer"
