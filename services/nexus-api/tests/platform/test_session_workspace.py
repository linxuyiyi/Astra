import json
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from app.api.workspace_routes import redact_secret_fields
from app.executors.errors import OpenCodeSessionNotFound
from app.worker_agent.workspace import prepare_workspace_local


class NativeServer:
    def __init__(self):
        self.sessions = {"ses_current": []}
        self.state = "idle"
        self.pending = []
        self.pending_permissions = []
        self.sent = []
        self.fail_after_accept = False
        self.created_options = []
        self.children_by_session = {}
        self.abort_count = 0
        self.auth = None
        self.base_url = "http://native"
        self.exchange_sessions: list[str | None] = []

    def _timeout(self):
        return 5

    async def health(self):
        return {"healthy": True, "version": "1.2.27"}

    async def session(self, directory, sid):
        if sid not in self.sessions:
            raise OpenCodeSessionNotFound(sid)
        return {"id": sid, "directory": str(directory)}

    async def status(self, directory, sid):
        await self.session(directory, sid)
        return self.state

    async def questions(self, directory):
        return self.pending

    async def permissions(self, directory):
        return []

    async def children(self, directory, sid):
        return self.children_by_session.get(sid, [])

    async def messages(self, directory, sid):
        await self.session(directory, sid)
        return self.sessions[sid]

    async def messages_page(self, directory, sid, *, limit, before=None):
        messages = await self.messages(directory, sid)
        return {"messages": messages, "next_before": None}

    async def exchange(self, method, path, *, params=None, body=None, session_id=None, task_id=None):
        self.exchange_sessions.append(session_id)
        async with httpx.AsyncClient(auth=self.auth, timeout=self._timeout()) as http:
            response = await http.request(method, self.base_url + path, params=params, json=body)
        headers = {key.lower(): value for key, value in response.headers.items()}
        return response.status_code, response.content, headers

    async def request(self, method, path, *, directory=None, body=None, session_id=None, task_id=None):
        if method == "POST" and path.endswith("/prompt_async"):
            sid = path.split("/")[2]
            self.sent.append((sid, body))
            self.sessions[sid].append(
                {"info": {"id": body["messageID"], "role": "user"}, "parts": body["parts"]}
            )
            # Real OpenCode persists the assistant placeholder synchronously before
            # prompt_async returns 204; mirror that so an in-flight turn always has an
            # assistant message. A turn that errors before execution (unknown model or
            # provider) persists no assistant message at all.
            self.sessions[sid].append(
                {"info": {"id": f"asst_{body['messageID']}", "role": "assistant"}, "parts": []}
            )
            self.state = "busy"
            if self.fail_after_accept:
                raise ConnectionError("ACK lost")
        if path == "/question":
            return self.pending
        if path == "/permission":
            return self.pending_permissions

    async def abort(self, directory, sid):
        await self.session(directory, sid)
        self.abort_count += 1
        self.state = "idle"
        self.pending = []
        return True

    async def create_session(self, directory, **kwargs):
        self.created_options.append(kwargs)
        sid = f"ses_{len(self.sessions)}"
        self.sessions[sid] = []
        return {"id": sid}


def test_redact_large_provider_catalog_stays_fast():
    secret = "gw-test-secret-value"
    catalog = {
        "data": [
            {
                "id": f"p{index}",
                "api": {
                    "settings": {"apiKey": secret, "baseURL": "http://gw"},
                    "headers": {"authorization": secret},
                },
                "models": [
                    {
                        "id": f"m{item}",
                        "api": {"settings": {"key": secret, "name": "keep"}},
                        "request": {"body": {"apiKey": secret, "model": "keep-model"}},
                    }
                    for item in range(20)
                ],
            }
            for index in range(200)
        ]
    }
    started = time.perf_counter()
    redact_secret_fields(catalog)
    assert time.perf_counter() - started < 1
    assert secret not in json.dumps(catalog)
    assert catalog["data"][0]["api"]["settings"]["baseURL"] == "http://gw"
    assert catalog["data"][0]["models"][0]["api"]["settings"]["name"] == "keep"
    assert catalog["data"][0]["models"][0]["request"]["body"] == {"model": "keep-model"}


async def _complete_current_turn(r):
    r.native.state = "idle"
    assistant = r.native.sessions["ses_current"][-1]
    assistant["parts"] = [
        {"type": "text", "text": "DONE"},
        {"type": "tool", "tool": "bash", "callID": "call_1", "state": {"status": "completed"}},
    ]
    assistant["info"].update(
        {
            "time": {"completed": 1},
            "finish": "stop",
            "tokens": {"input": 100, "output": 20, "cache": {"read": 5}},
        }
    )
    await r.service.refresh("T1")


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "a@b.c"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "t"],
        check=True,
        capture_output=True,
        text=True,
    )
    (repo / "a.txt").write_text("a\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "base"],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("invalid", ["plain", "nested", "symlink", "foreign", "missing"])
async def test_remote_worktree_shell_rejects_invalid_reuse(tmp_path, invalid):
    """The Agent prepares the task directory locally. Invalid reuse still fails."""
    root = tmp_path / "project"
    root.mkdir()
    _init_repo(root)
    target = tmp_path / "task"
    if invalid == "plain":
        target.mkdir()
    elif invalid == "nested":
        target = root / "nested"
        target.mkdir()
    elif invalid == "symlink":
        target.symlink_to(root, target_is_directory=True)
    elif invalid == "foreign":
        target.mkdir()
        _init_repo(target)
    with pytest.raises(RuntimeError):
        prepare_workspace_local(str(root), str(target), reuse_existing=True)


async def test_remote_worktree_shell_reuses_absolute_common_dir_and_config(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _init_repo(root)
    (root / "opencode.json").write_text('{"model":"company/model"}')
    target = tmp_path / "task"
    prepare_workspace_local(str(root), str(target))
    prepare_workspace_local(str(root), str(target), reuse_existing=True)
    assert (target / "opencode.json").read_text() == (root / "opencode.json").read_text()


FAST_503_ROUTES = (
    "session/ses_current/diff",
    "file/content?path=notes.txt",
    "find?pattern=canary",
)
