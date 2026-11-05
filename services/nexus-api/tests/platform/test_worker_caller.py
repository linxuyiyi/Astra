"""Worker-marked environments use the Gateway and leave SSH forwarding in place."""

from app.worker_agent.local_opencode import gateway_http_base


def test_gateway_http_base_is_the_origin():
    assert gateway_http_base("ws://127.0.0.1:8877/api/worker-gateway/connect") == "http://127.0.0.1:8877"
    assert gateway_http_base("wss://worker.example/connect") == "https://worker.example"
