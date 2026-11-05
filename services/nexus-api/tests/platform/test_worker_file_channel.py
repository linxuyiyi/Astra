"""File bytes stay on the phase-2 data channel. http remains an 8 MiB JSON body."""

import base64

import pytest

from app.runtime.worker_client import WorkerCommandError, command_result
from app.worker_gateway.data_channel import (
    MAX_BODY_BYTES,
    MAX_FILE_BYTES,
    MAX_FILE_TEXT_BYTES,
)
from app.worker_gateway.service import WorkerGateway


def test_command_result_keeps_legacy_text_and_attaches_raw_body():
    legacy = {"payload": {"stage": "result", "ok": False, "error_code": "符号链接指向任务 Workspace 之外"}}
    with pytest.raises(WorkerCommandError) as old:
        command_result(legacy)
    assert old.value.code == "符号链接指向任务 Workspace 之外"
    assert str(old.value) == "符号链接指向任务 Workspace 之外"
    framed = {
        "payload": {
            "stage": "result",
            "ok": False,
            "error_code": "symlink_escape",
            "error": "符号链接指向任务 Workspace 之外",
        }
    }
    with pytest.raises(WorkerCommandError) as current:
        command_result(framed)
    assert current.value.code == "symlink_escape"
    assert str(current.value) == "符号链接指向任务 Workspace 之外"
    raw = base64.b64encode(b"\xff").decode("ascii")
    result = command_result({"result": {"code": 0, "size": 1, "sha256": "abc"}, "body": raw})
    assert result["body"] == raw
    assert result["code"] == 0


def test_file_channel_limits_do_not_raise_the_http_cap():
    assert MAX_BODY_BYTES == 8 * 1024 * 1024
    assert MAX_FILE_BYTES == 20 * 1024 * 1024
    assert MAX_FILE_TEXT_BYTES > MAX_BODY_BYTES
    assert WorkerGateway._side_effect("upload_file", {})
    assert not WorkerGateway._side_effect("read_file", {})
    assert not WorkerGateway._side_effect("git_readonly", {})
    assert not WorkerGateway._side_effect("validate_path", {})
