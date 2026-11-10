"""SSH exec 的本地辅助入口；原生 HTTP/SSE 不经过这里。"""

import base64
import json
import os
import sys
import time
from uuid import uuid4

from app.worker_tools.files import FILE_COMMANDS, FileCommandError, execute_file_command
from app.worker_tools.inspection import inspect_runtime_from_payload
from app.worker_tools.supervisor import assert_native_idle
from app.worker_tools.workspace import prepare_workspace_local, update_workspace_local


def execute(name, data, content=None):
    root = os.environ["NEXUS_WORKER_WORKSPACE_ROOT"]
    url = os.environ["NEXUS_WORKER_OPENCODE_URL"]
    if name in FILE_COMMANDS:
        result, body = execute_file_command(
            {"name": name, "data": data},
            allowed_root=root,
            request_id=uuid4().hex,
            deadline=time.time() + 60,
            content=content,
        )
        if body is not None:
            result["body"] = base64.b64encode(body).decode()
        return result
    if name == "prepare_workspace":
        return prepare_workspace_local(
            data["root"], data["target"], reuse_existing=data["reuse_existing"], allowed_root=root
        )
    if name == "inspect_runtime":
        return inspect_runtime_from_payload(url, root, data)
    if name == "update_workspace":
        from urllib.parse import urlsplit

        assert_native_idle(root, urlsplit(url).port)
        return update_workspace_local(root, branch=data["branch"])
    raise ValueError(f"不支持的 SSH 操作: {name}")


def main():
    # Secrets are read from the managed file, never placed in command arguments.
    from app.worker_tools.__main__ import load_env_file

    os.environ.update(load_env_file(sys.argv[1]))
    data = json.loads(sys.stdin.buffer.readline())
    content = sys.stdin.buffer.read() if data.pop("upload", False) else None
    try:
        result = execute(sys.argv[2], data, content)
    except FileCommandError as exc:
        print(json.dumps({"error_code": exc.code, "error": str(exc)}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
