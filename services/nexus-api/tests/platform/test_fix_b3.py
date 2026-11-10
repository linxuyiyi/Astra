"""修订号门禁、远程克隆、分配检查和共用 JSONC。"""

import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain import TaskStatus
from app.models import Base, Task
from app.worker_tools.jsonc import loads_jsonc as parse_jsonc

PAT = "ghp_fixb3tokenvalue"


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


def test_shared_jsonc_accepts_inline_comments_and_trailing_commas(tmp_path):
    text = """
    {
      // line
      "url": "http://example/a//b", /* block
      comment */
      "n": 1,
    }
    """
    assert parse_jsonc(text) == {"url": "http://example/a//b", "n": 1}
    assert parse_jsonc("[1, 2,]") == [1, 2]
    target = tmp_path / "opencode.jsonc"
    target.write_text('{ "skill": "review", }\n', encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "app/worker_tools/jsonc.py", str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"skill": "review"}


def _task(task_id, **overrides):
    values = dict(
        id=task_id,
        title=task_id,
        task_type="ISSUE",
        category_id="testing",
        status=TaskStatus.PENDING,
    )
    values.update(overrides)
    return Task(**values)


def _git_stub() -> str:
    return textwrap.dedent(
        """\
        #!/usr/bin/env python3
        import json, os, sys
        from pathlib import Path
        log = Path(os.environ["GIT_LOG"])
        state_path = Path(os.environ["GIT_STATE"])
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        args = sys.argv[1:]
        workspace = None
        while args and args[0] in {"-C", "-c"}:
            if args[0] == "-C":
                workspace = args[1]
            args = args[2:]
        command = args[0] if args else ""
        rest = args[1:]
        record = {"argv": sys.argv[1:], "command": command, "token": os.environ.get("NEXUS_GIT_TOKEN", "")}
        askpass = os.environ.get("GIT_ASKPASS")
        if askpass and Path(askpass).is_file():
            record["askpass"] = Path(askpass).read_text()
        token_path = os.environ.get("TOKEN_PATH")
        if token_path and Path(token_path).is_file():
            record["mode"] = oct(Path(token_path).stat().st_mode & 0o777)
        lines = json.loads(log.read_text()) if log.exists() else []
        lines.append(record)
        log.write_text(json.dumps(lines))
        repo = state.get(workspace or "", {})
        if command == "rev-parse" and "--git-dir" in rest:
            sys.exit(0 if repo else 1)
        if command == "rev-parse" and "--show-toplevel" in rest:
            sys.stdout.write(workspace or "")
            sys.exit(0 if repo else 1)
        if command == "rev-parse" and "HEAD" in rest:
            sys.exit(0 if repo.get("head") else 1)
        if command == "config":
            sys.stdout.write(repo.get("origin", ""))
            sys.exit(0)
        if command == "branch":
            sys.stdout.write(repo.get("branch", ""))
            sys.exit(0)
        if command == "symbolic-ref":
            branch = repo.get("branch", "")
            sys.stdout.write(branch + ("\\n" if branch else ""))
            sys.exit(0 if branch else 1)
        if command == "clone":
            branch = rest[rest.index("--branch") + 1]
            url, dest = rest[-2], rest[-1]
            state[dest] = {"origin": url, "branch": branch, "head": "cloned"}
        elif command == "init":
            state[workspace] = {"origin": "", "branch": "", "head": ""}
        elif command == "commit":
            repo["head"] = "init"
            state[workspace] = repo
        else:
            sys.exit(1)
        state_path.write_text(json.dumps(state))
        """
    )


def _run_workspace(tmp_path: Path, payload: dict, *, token: str | None, state: dict, workspace: Path):
    import asyncio

    from app.runtime.remote_workspace import git_workspace
    from tests.platform.deployment_target import LocalTarget

    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    git = bindir / "git"
    stub = _git_stub()
    stub = stub.replace('command = args[0] if args else ""', 'command = args[0] if args else ""')
    stub = stub.replace(
        'repo = state.get(workspace or "", {})',
        'repo = state.get(workspace or "", {})\n'
        'if command == "--version": print("git version 2.43.0"); sys.exit(0)\n'
        'if command == "status": sys.exit(0)',
    )
    stub = stub.replace('sys.stdout.write(workspace or "")', 'sys.stdout.write(workspace if repo else "")')
    stub = stub.replace(
        'if command == "rev-parse" and "HEAD" in rest:',
        'if command == "rev-parse" and "HEAD" in rest:\n    sys.stdout.write(repo.get("head", ""))',
    )
    git.write_text(stub)
    git.chmod(0o755)
    token_file = tmp_path / "secret.token"
    if token is not None:
        token_file.write_text(token)
    log, state_path = tmp_path / "git.log", tmp_path / "git-state.json"
    state_path.write_text(json.dumps(state))
    env = {
        "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
        "GIT_LOG": str(log),
        "GIT_STATE": str(state_path),
    }
    if token is not None:
        env["TOKEN_PATH"] = str(token_file)
    worker = LocalTarget(tmp_path, workspace=workspace, env=env)
    try:
        asyncio.run(
            git_workspace(
                worker,
                url=payload.get("repository", {}).get("url", ""),
                branch=payload.get("repository", {}).get("branch", "main"),
                token_path=str(token_file) if token is not None else "",
            )
        )
        completed = subprocess.CompletedProcess([], 0, "", "")
    except Exception as exc:
        completed = subprocess.CompletedProcess([], 1, "", str(exc))
    calls = json.loads(log.read_text()) if log.exists() else []
    return completed, calls, token_file


def test_remote_install_clones_with_askpass_and_checks_an_existing_workspace(tmp_path):
    workspace = tmp_path / "srv" / "app"
    payload = {"repository": {"url": "https://git.example/org/repo.git", "branch": "dev"}}
    completed, calls, token_file = _run_workspace(
        tmp_path / "clone", payload, token=PAT, state={}, workspace=workspace
    )
    assert completed.returncode == 0, completed.stderr
    assert not token_file.exists()
    cloned = next(item for item in calls if item["command"] == "clone")
    assert PAT not in " ".join(cloned["argv"])
    assert cloned["token"] == ""
    assert PAT not in cloned["askpass"]
    assert str(token_file) in cloned["askpass"]
    assert cloned["mode"] == oct(stat.S_IRUSR | stat.S_IWUSR)
    assert all(item["command"] != "init" for item in calls)
    stored = json.loads((tmp_path / "clone" / "git-state.json").read_text(encoding="utf-8"))
    assert stored[str(workspace)]["origin"] == "https://git.example/org/repo.git"
    assert stored[str(workspace)]["branch"] == "dev"

    mismatch, mismatch_calls, mismatch_token = _run_workspace(
        tmp_path / "mismatch",
        payload,
        token=PAT,
        state={str(workspace): {"origin": "https://git.example/org/other.git", "branch": "dev", "head": "x"}},
        workspace=workspace,
    )
    assert mismatch.returncode != 0
    assert "origin" in mismatch.stderr
    assert all(item["command"] != "clone" for item in mismatch_calls)
    assert not mismatch_token.exists()

    occupied = tmp_path / "dirty" / "app"
    occupied.mkdir(parents=True)
    (occupied / "README").write_text("x", encoding="utf-8")
    dirty, dirty_calls, _token = _run_workspace(
        tmp_path / "dirty-run", payload, token=PAT, state={}, workspace=occupied
    )
    assert dirty.returncode != 0
    assert "不为空" in dirty.stderr
    assert all(item["command"] != "clone" for item in dirty_calls)
    assert all(item["command"] != "init" for item in dirty_calls)

    empty, empty_calls, empty_token = _run_workspace(
        tmp_path / "empty", {}, token=None, state={}, workspace=tmp_path / "plain"
    )
    assert empty.returncode == 0, empty.stderr
    assert any(item["command"] == "init" for item in empty_calls)
    assert all(item["command"] != "clone" for item in empty_calls)
    assert not empty_token.exists()
