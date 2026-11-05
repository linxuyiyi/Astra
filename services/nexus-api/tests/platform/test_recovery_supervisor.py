import asyncio

import pytest

from app.runtime import recovery


async def test_supervisor_survives_transient_database_failure(monkeypatch):
    attempts = []

    class Session:
        async def __aenter__(self):
            attempts.append(1)
            raise RuntimeError("database unavailable")

        async def __aexit__(self, *args):
            return False

    async def tick(seconds):
        if len(attempts) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(recovery, "SessionLocal", Session)
    monkeypatch.setattr(recovery.asyncio, "sleep", tick)
    with pytest.raises(asyncio.CancelledError):
        await recovery.supervise_sessions()
    assert len(attempts) == 2
