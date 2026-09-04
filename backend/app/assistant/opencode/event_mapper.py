import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from app.executors.opencode_server import OpenCodeServerClient, server_event_session_id


class EventMapper:
    def __init__(self, opencode_client: OpenCodeServerClient):
        self.client = opencode_client

    async def stream_and_map(
        self, opencode_session_id: str, conversation_id: str, directory: Path
    ) -> AsyncGenerator[str, None]:
        async for event in self.client.events(directory):
            # Check if this event belongs to the current session
            if server_event_session_id(event) != opencode_session_id:
                continue

            mapped_event = self._map_event(event, conversation_id)
            if mapped_event:
                yield f"data: {json.dumps(mapped_event)}\n\n"

    def _map_event(self, opencode_event: dict[str, Any], conversation_id: str) -> dict[str, Any] | None:
        event_type = opencode_event.get("type")
        if not event_type:
            return None

        properties = opencode_event.get("properties", {})
        info = properties.get("info", {})

        base_event = {
            "conversation_id": conversation_id,
            "timestamp": opencode_event.get("timestamp"),
            "data": properties
        }

        # Mapping OpenCode events to Assistant events
        if event_type == "message.updated":
            base_event["event"] = "assistant.message.delta"
            base_event["message_id"] = info.get("id")
        elif event_type == "message.part.updated":
            # Just emit a delta to frontend for parts
            base_event["event"] = "assistant.message.delta"
            base_event["message_id"] = info.get("id")
        elif event_type == "session.idle":
            base_event["event"] = "assistant.message.completed"
        elif event_type == "tool.execute.before":
            base_event["event"] = "assistant.tool.started"
            base_event["message_id"] = properties.get("toolCallID")
        elif event_type == "tool.execute.after":
            base_event["event"] = "assistant.tool.completed"
            base_event["message_id"] = properties.get("toolCallID")
        elif event_type == "session.status":
            base_event["event"] = "assistant.status"
        else:
            base_event["event"] = f"assistant.{event_type}"

        return base_event
