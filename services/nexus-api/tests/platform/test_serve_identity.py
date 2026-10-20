import tempfile
from pathlib import Path

from app.models import OpenCodeExecutionEnvironment
from app.runtime.environment import ExecutionEnvironmentRuntime
from tests.platform.test_runtime import fake_agent_launcher


async def test_healthy_serve_check_compares_path_and_owner_without_writing(tmp_path, monkeypatch):
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    binary = fake_agent_launcher(tmp_path, 'print("ok")\n', "fake-opencode")
    port = 59991
    environment = OpenCodeExecutionEnvironment(
        employee_id="astra-check",
        opencode_server_port=port,
        mode="LOCAL",
        name="本地检查",
        opencode_path=str(binary),
        workspace_root=str(workspace),
    )
    owner = Path(tempfile.gettempdir()) / "nexus-employee-serve" / f"{port}.json"
    if owner.exists():
        owner.unlink()

    class FakeClient:
        def __init__(self, _settings, base_url=None):
            self.base_url = base_url

        async def health(self):
            return {"healthy": True, "version": "1.18.35"}

        async def request(self, method, path, **_kwargs):
            assert method == "GET"
            assert path == "/path"
            return {"directory": str(workspace.resolve())}

    monkeypatch.setattr("app.executors.opencode_server.OpenCodeServerClient", FakeClient)
    checks = await ExecutionEnvironmentRuntime().test(environment)
    server = checks[-1]
    assert server.passed is True
    assert "Workspace 匹配" in server.detail
    assert not owner.exists()

    class ForeignOwner(FakeClient):
        async def request(self, method, path, **_kwargs):
            return {"directory": str((tmp_path / "other").resolve())}

    monkeypatch.setattr("app.executors.opencode_server.OpenCodeServerClient", ForeignOwner)
    mismatched = await ExecutionEnvironmentRuntime().test(environment)
    assert mismatched[-1].passed is False
    assert "不一致" in mismatched[-1].detail
    assert not owner.exists()
