from __future__ import annotations

from taskwright.models import Task
from taskwright.render import tasks_to_jsonantt


def test_tasks_to_jsonantt_groups_by_project_and_includes_task_layer() -> None:
    tasks = [
        Task(
            id="AAAA-BBBB-CCCC-DDDD",
            title="Spec API",
            project="Apollo",
            start_date="2026-06-01",
            due_date="2026-06-10",
        ),
        Task(
            id="1111-2222-3333-4444",
            title="Build UI",
            project="Apollo",
            depends_on=["AAAA-BBBB-CCCC-DDDD"],
        ),
        Task(
            id="EEEE-FFFF-GGGG-HHHH",
            title="Unassigned task",
            project="",
        ),
    ]

    data = tasks_to_jsonantt(
        tasks,
        title="My Board",
        project_colors={"Apollo": "#123456"},
        project_order=["Apollo", "Unassigned"],
    )

    assert data["title"] == "My Board"
    assert data["dateformat"] == "%Y-%m-%d"
    assert [layer["name"] for layer in data["tasks"]] == ["Apollo", "Unassigned"]
    assert data["tasks"][0]["color"] == "#123456"
    assert [task["name"] for task in data["tasks"][0]["tasks"]] == ["Spec API", "Build UI"]
    assert data["tasks"][0]["tasks"][1]["not_before"] == "AAAA-BBBB-CCCC-DDDD"


def test_tasks_to_jsonantt_omits_missing_dependency_targets() -> None:
    tasks = [
        Task(
            id="AAAA-BBBB-CCCC-DDDD",
            title="Task one",
            project="Apollo",
            depends_on=["MISSING-ID"],
        )
    ]

    data = tasks_to_jsonantt(tasks)
    first = data["tasks"][0]["tasks"][0]

    assert first["name"] == "Task one"
    assert "not_before" not in first
