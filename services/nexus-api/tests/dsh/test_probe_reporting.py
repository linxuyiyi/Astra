import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_invalid_gateway_credential_url_never_enters_report(tmp_path, monkeypatch, capsys):
    script = Path(__file__).resolve().parents[4] / "tools/e2e/dsh_acp_probe.py"
    spec = importlib.util.spec_from_file_location("dsh_probe_reporting_test", script)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    monkeypatch.setattr(probe, "inspect_installation", lambda *_: {"dsh": "0.2.1-rc.2"})
    monkeypatch.setenv("DSH_TEST_GATEWAY_KEY", "do-not-write-this-key-to-the-report")
    args = SimpleNamespace(
        root=tmp_path / "new-probe", timeout=1, model="route", key_env="DSH_TEST_GATEWAY_KEY",
        gateway_url="https://user:do-not-write-this-password@gateway/v1?token=private-query",
        context_window=32768, max_tokens=1000, thinking_format=None,
        dsh=tmp_path / "dsh", lock=tmp_path / "lock",
    )
    assert await probe.probe(args) == 1
    report = (args.root / "report.json").read_text()
    combined = report + capsys.readouterr().out
    for secret in ("do-not-write-this-key", "do-not-write-this-password", "private-query"):
        assert secret not in combined
    result = json.loads(report)
    assert result["gateway_url"] is None
    assert result["checks"]["gateway_catalog"]["status"] == "FAIL"
    assert result["checks"]["first_turn"]["status"] == "UNVERIFIED"
    assert result["overall"] == "FAIL" and result["production_cutover"] is False
