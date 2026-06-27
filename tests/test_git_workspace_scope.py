from __future__ import annotations

import subprocess
from pathlib import Path

from taskunity.task_store import ensure_workspace, git_status, git_sync


def _run_git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def test_git_status_requires_workspace_repo_root(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _run_git(repo_root, "init")

    workspace = repo_root / "workspace"
    ensure_workspace(workspace)

    status = git_status(workspace)

    assert status["tracked"] is False
    assert status["message"] == "Git integration only works when the workspace folder is the repository root."


def test_git_sync_rejects_nested_workspace_repo(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _run_git(repo_root, "init")

    workspace = repo_root / "workspace"
    ensure_workspace(workspace)

    result = git_sync(workspace)

    assert result["ok"] is False
    assert result["message"] == "Git integration only works when the workspace folder is the repository root."


def test_git_status_allows_workspace_repo_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    ensure_workspace(workspace)
    _run_git(workspace, "init")

    status = git_status(workspace)

    assert status["tracked"] is True
    assert status["message"] == ""


def test_git_sync_bootstraps_upstream_for_fresh_repo(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    _run_git(tmp_path, "init", "--bare", str(remote))

    workspace = tmp_path / "workspace"
    ensure_workspace(workspace)
    _run_git(workspace, "init")
    _run_git(workspace, "config", "user.name", "Taskunity Test")
    _run_git(workspace, "config", "user.email", "taskunity@example.com")
    _run_git(workspace, "remote", "add", "origin", str(remote))

    result = git_sync(workspace)

    assert result["ok"] is True
    assert "set upstream" in result["message"].lower()
    upstream = _run_git(workspace, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    assert upstream.stdout.strip() == "origin/main"
