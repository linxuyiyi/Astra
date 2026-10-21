import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("NEXUS_ASR_CONFIG", str(ROOT / "config.yaml"))
os.environ.setdefault("NEXUS_ASR_OFFLINE", "1")

from app.config import load_settings  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture(scope="session")
def settings():
    value = load_settings()
    value.server.rate_limit_per_minute = 0  # tests issue many requests quickly
    return value


@pytest.fixture(scope="session")
def client(settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
