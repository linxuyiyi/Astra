"""开发启动脚本只转发给 Python，健康检查只接受 HTTP 2xx。"""

from __future__ import annotations

import importlib.util
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
WRAPPERS = (
    "start_astra.sh",
    "start_agent_service.sh",
    "start_task_mcp.sh",
    "start_nexus_api.sh",
)


def _load_procman():
    spec = importlib.util.spec_from_file_location("procman_under_test", ROOT / "ops" / "dev" / "procman.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dev_wrappers_delegate_to_python():
    assert not (ROOT / "ops" / "dev" / "daemon.sh").exists()
    for name in WRAPPERS:
        source = (ROOT / "ops" / "dev" / name).read_text(encoding="utf-8")
        assert "ops/dev/" in source
        assert ".py" in source
        for banned in ("setsid", "nohup", "lsof", "ps -p", "curl"):
            assert banned not in source, name


def test_http_ready_requires_2xx():
    procman = _load_procman()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                code = 200 if self.headers.get("X-User-ID") == "nexus" else 401
            elif self.path == "/bad":
                code = 500
            else:
                code = 302
            self.send_response(code)
            self.end_headers()

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        assert not procman.http_ready(f"http://127.0.0.1:{port}/bad")
        assert not procman.http_ready(f"http://127.0.0.1:{port}/health")
        assert not procman.http_ready(f"http://127.0.0.1:{port}/redirect")
        assert procman.http_ready(
            f"http://127.0.0.1:{port}/health",
            headers={"X-User-ID": "nexus"},
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
