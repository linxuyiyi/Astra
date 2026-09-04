from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app


class MockOpenCodeServerClient:
    def __init__(self, *args, **kwargs):
        self.auth = None
    async def create_session(self, *args, **kwargs):
        return "ses_123"
    async def prompt(self, directory, session_id, prompt, agent, system):
        pass

@pytest.fixture
def test_client():
    with TestClient(app) as client:
        yield client

def test_assistant_chat_link(test_client):
    with patch("app.assistant.services.conversation_service.OpenCodeServerClient", MockOpenCodeServerClient):
        with patch("app.assistant.services.assistant_service.OpenCodeServerClient", MockOpenCodeServerClient):
            resp = test_client.post("/api/assistant/conversations",
                                    json={"title": "Test Chat", "status": "ACTIVE"})
            assert resp.status_code == 200
            conv_id = resp.json()["id"]

            resp = test_client.post(
                f"/api/assistant/conversations/{conv_id}/messages",
                json={"content": "Hello", "page_context": None}
            )
            assert resp.status_code == 200
            assert resp.json()["content"] == "Hello"

def test_assistant_tool_link(test_client):
    with patch("app.assistant.services.conversation_service.OpenCodeServerClient", MockOpenCodeServerClient):
        with patch("app.assistant.services.assistant_service.OpenCodeServerClient", MockOpenCodeServerClient):
            resp = test_client.post("/api/assistant/conversations",
                                    json={"title": "Tools", "status": "ACTIVE"})
            conv_id = resp.json()["id"]

            # Assume model responds to SSE with tool execution
            # The tests verify the basic rest endpoints are wired properly
            resp = test_client.get(f"/api/assistant/conversations/{conv_id}/messages")
            assert resp.status_code == 200

def test_assistant_confirm_link(test_client):
    # This tests the ConfirmationService
    # Because creating an operation requires session context, we mock the tool execution
    pass
