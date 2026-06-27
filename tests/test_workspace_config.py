from __future__ import annotations

import json
from pathlib import Path

from taskwright.task_store import ensure_workspace, load_workspace_config, save_json, workspace_label


def test_ensure_workspace_writes_default_config_file(tmp_path: Path) -> None:
    workspace = tmp_path / "my-workboard"
    ensure_workspace(workspace)

    config_path = workspace / "config.json"
    assert config_path.exists()

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["app_name"] == "Taskwright"
    assert data["workspace_name"] == "My Workboard"
    assert data["workspace_description"] == "Local file-backed workspace/task tracker"
    assert data["export_title"] == "My Workboard"


def test_workspace_label_prefers_config_workspace_name(tmp_path: Path) -> None:
    workspace = tmp_path / "default-name"
    ensure_workspace(workspace)
    save_json(
        workspace / "config.json",
        {
            "app_name": "Taskwright",
            "workspace_name": "My Custom Workspace",
            "workspace_description": "Editable description",
            "export_title": "Custom Export",
        },
    )

    assert workspace_label(workspace) == "My Custom Workspace"
    assert load_workspace_config(workspace)["export_title"] == "Custom Export"
