from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import taskunity.app as app_module
from taskunity.app import create_app
from taskunity.task_store import create_task, ensure_workspace, save_task, upsert_project


def _make_client(workspace: Path) -> TestClient:
    ensure_workspace(workspace)
    return TestClient(create_app(workspace))


def test_git_sync_route_preserves_open_task_panel(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    task = create_task(workspace, "Keep task panel open")

    monkeypatch.setattr(app_module, "git_sync", lambda _: {"ok": True, "message": "Synced cleanly."})

    client = _make_client(workspace)
    response = client.post(
        "/git/sync",
        data={
            "f_view": "list",
            "f_panel_task": task.id,
        },
    )

    assert response.status_code == 200
    assert 'class="git-toast success"' in response.text
    assert "Synced cleanly." in response.text
    assert "Keep task panel open" in response.text


def test_git_sync_route_preserves_calendar_filters_and_error_state(monkeypatch, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(app_module, "git_sync", lambda _: {"ok": False, "message": "Push failed."})

    client = _make_client(workspace)
    response = client.post(
        "/git/sync",
        data={
            "f_view": "calendar",
            "f_hide_done": "1",
            "f_calendar_month": "5",
            "f_calendar_year": "2027",
        },
    )

    assert response.status_code == 200
    assert 'class="git-toast error"' in response.text
    assert 'name="f_calendar_month" value="5"' in response.text
    assert 'name="f_calendar_year" value="2027"' in response.text
    assert 'name="f_hide_done" value="1"' in response.text


def test_projects_view_click_filters_to_task_list_without_show_only_button(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = upsert_project(workspace, "Apollo", description="Moonshot")
    assert project is not None

    task = create_task(workspace, "Linked task")
    task.project_id = project.id
    task.project = project.name
    save_task(workspace, task)

    client = _make_client(workspace)

    projects_response = client.get("/partials/main?view=projects")
    assert projects_response.status_code == 200
    assert 'class="project-open-form"' in projects_response.text
    assert 'name="view" value="list"' in projects_response.text
    assert f'name="project" value="{project.id}"' in projects_response.text
    assert 'title="Edit Apollo"' in projects_response.text

    panel_response = client.get(f"/projects/{project.id}/panel?view=projects")
    assert panel_response.status_code == 200
    assert "Show only this project" not in panel_response.text
