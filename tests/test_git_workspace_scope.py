from __future__ import annotations

import subprocess
from pathlib import Path

from taskwright.task_store import ensure_workspace, git_status, git_sync


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
