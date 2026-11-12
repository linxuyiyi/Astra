"""Model the native durable message lookup for tests with an HTTP AgentScope double."""

import json

import httpx


class AgentChatTransport(httpx.MockTransport):
    def __init__(self, handler):
        messages = set()

        def route(request):
            path = request.url.path
            if path.startswith("/nexus/session-messages/"):
                key = (request.url.params["session_id"], path.rsplit("/", 1)[-1])
                return httpx.Response(204 if key in messages else 404)
            response = handler(request)
            if path == "/chat/" and response.status_code < 300:
                body = json.loads(request.content)
                inputs = body.get("input")
                for item in inputs if isinstance(inputs, list) else [inputs]:
                    if isinstance(item, dict) and item.get("metadata", {}).get("nexus_deduplicate"):
                        messages.add((body["session_id"], item["id"]))
            return response

        super().__init__(route)
