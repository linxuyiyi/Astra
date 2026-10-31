"""工作区打开耗时可复现。同一次打开不发会新建 Session 的请求。"""

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.api.workspace_routes import _present_bundles, workspace_ui_for

REPO = Path(__file__).resolve().parents[4]


def load_script():
    path = REPO / "tools/perf/workspace_open.py"
    spec = importlib.util.spec_from_file_location("workspace_open", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.server.paths.append(("GET", self.path.split("?", 1)[0]))
        if self.path.startswith("/api/events/stream"):
            body = b"event: update\ndata: {}\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        self.server.paths.append(("POST", self.path))
        self.send_response(500)
        self.end_headers()

    def log_message(self, _format, *_args):
        return


def test_open_timings_separate_cold_and_warm_and_only_read():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.paths = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        report = load_script().measure(
            f"http://127.0.0.1:{port}",
            "T1",
            "/opencode-workspace/1.18.33/index.html",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert set(report) == {"cold", "warm_first_open", "reopen", "note"}
    assert "P95" not in report["note"] or "不判断" in report["note"]
    for phase in ("cold", "warm_first_open", "reopen"):
        sample = report[phase]
        assert set(sample) == {
            "http_task_ms",
            "http_workspace_ms",
            "sse_ttfb_ms",
            "sse_first_event_ms",
            "iframe_ms",
            "total_ms",
        }
        assert all(value >= 0 for value in sample.values())
    methods = {method for method, _path in server.paths}
    assert methods == {"GET"}
    assert not any(path.endswith("/start") or path.endswith("/restart") for _method, path in server.paths)
    text = json.dumps(report)
    assert "warm_first_open" in text and "reopen" in text


def test_installed_bundle_is_cached_and_a_missing_one_appears_after_install(tmp_path, monkeypatch):
    version = "0.0.9"
    target = tmp_path / "index.html"
    monkeypatch.setattr(
        "app.api.workspace_routes.workspace_ui_bundle",
        lambda value: target if value == version else Path("/no/such/bundle/index.html"),
    )
    _present_bundles.discard(version)
    assert workspace_ui_for({"opencode_version": version})[1] is None
    target.write_text("workspace")
    assert workspace_ui_for({"opencode_version": version})[1] == f"/opencode-workspace/{version}/index.html"
    target.unlink()
    assert workspace_ui_for({"opencode_version": version})[1] == f"/opencode-workspace/{version}/index.html"
