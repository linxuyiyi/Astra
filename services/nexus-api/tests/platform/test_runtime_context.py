from app.runtime.opencode_workspace import discover_workspace_config


def test_workspace_config_is_discovered_from_nested_directory(tmp_path):
    workspace = tmp_path / "workspaces" / "astra-01"
    workspace.mkdir(parents=True)
    (tmp_path / "workspaces" / "opencode.json").write_text("{}", encoding="utf-8")
    assert discover_workspace_config(workspace) == tmp_path / "workspaces" / "opencode.json"


def test_workspace_config_is_not_inherited_above_a_git_root(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    task_worktree = repo / "tasks" / "T1"
    task_worktree.mkdir(parents=True)
    (tmp_path / "opencode.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("app.runtime.opencode_workspace.global_config_path", lambda: None)
    # A per-task git worktree is its own project root: configs above it must not leak in.
    assert discover_workspace_config(task_worktree) is None


def test_missing_workspace_config_falls_back_to_global(monkeypatch, tmp_path):
    workspace = tmp_path / "workspaces" / "astra-01"
    workspace.mkdir(parents=True)
    global_config = tmp_path / "global-opencode.json"
    global_config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("app.runtime.opencode_workspace.global_config_path", lambda: global_config)
    assert discover_workspace_config(workspace) == global_config
