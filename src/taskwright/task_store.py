from __future__ import annotations

import json
import re
import secrets
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Attachment, Milestone, Note, Program, Project, Task


class WorkspaceError(RuntimeError):
    pass


def workspace_paths(workspace: Path) -> dict[str, Path]:
    return {
        "root": workspace,
        "programs": workspace / "programs",
        "legacy_program": workspace / "program.json",
        "tasks": workspace / "tasks",
        "milestones": workspace / "milestones",
        "assets": workspace / "assets",
    }


def ensure_workspace(workspace: Path) -> None:
    paths = workspace_paths(workspace)
    paths["root"].mkdir(parents=True, exist_ok=True)
    paths["programs"].mkdir(parents=True, exist_ok=True)
    paths["tasks"].mkdir(parents=True, exist_ok=True)
    paths["milestones"].mkdir(parents=True, exist_ok=True)
    paths["assets"].mkdir(parents=True, exist_ok=True)
    _migrate_legacy_program(paths["legacy_program"], paths["programs"])
    if not any(paths["programs"].glob("*.json")):
        save_json(paths["programs"] / "default.json", Program().model_dump(mode="json"))


def init_workspace(workspace: Path, with_sample: bool = True) -> None:
    ensure_workspace(workspace)
    if with_sample and not any((workspace / "tasks").glob("*.json")):
        sample = Task(
            id="TASK-0001",
            title="Create the first program milestone",
            status="working",
            priority="high",
            owner="",
            summary="Replace this sample with a real program task.",
            description="Use the side panel to edit this task. The JSON file is stored in tasks/TASK-0001.json.",
            tags=["sample", "planning"],
            start_date=datetime.now().date().isoformat(),
            due_date=None,
            percent_complete=25,
            checklist=[
                {"text": "Initialize workspace", "done": True},
                {"text": "Add real tasks", "done": False},
                {"text": "Attach screenshots or notes", "done": False},
            ],
            notes=[Note(body="This is a sample note. Add real progress notes from the task detail panel.")],
        )
        save_task(workspace, sample)


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WorkspaceError(f"Missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise WorkspaceError(f"Invalid JSON in {path}: {exc}") from exc


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_program(workspace: Path) -> Program:
    ensure_workspace(workspace)
    return Program.model_validate(load_json(primary_program_path(workspace)))


def _slugify_program_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "default"


def _migrate_legacy_program(legacy_path: Path, programs_dir: Path) -> None:
    if not legacy_path.exists():
        return
    if any(programs_dir.glob("*.json")):
        return
    legacy_data = load_json(legacy_path)
    legacy_program = Program.model_validate(legacy_data)
    target = programs_dir / f"{_slugify_program_name(legacy_program.name)}.json"
    save_json(target, legacy_program.model_dump(mode="json"))
    legacy_path.unlink(missing_ok=True)


def list_program_files(workspace: Path) -> list[Path]:
    paths = sorted((workspace / "programs").glob("*.json"), key=lambda p: p.name.lower())
    default = workspace / "programs" / "default.json"
    if default in paths:
        paths.remove(default)
        return [default, *paths]
    return paths


def primary_program_path(workspace: Path) -> Path:
    ensure_workspace(workspace)
    files = list_program_files(workspace)
    if files:
        return files[0]
    fallback = workspace / "programs" / "default.json"
    save_json(fallback, Program().model_dump(mode="json"))
    return fallback


def save_program(workspace: Path, program: Program) -> None:
    save_json(primary_program_path(workspace), program.model_dump(mode="json"))


DEFAULT_PROJECT_COLOR = "#2e6fd8"
PROJECT_PALETTE = [
    "#2e6fd8",
    "#338a52",
    "#c05746",
    "#8a5cd1",
    "#c08a2e",
    "#2e9bb0",
    "#b0457f",
    "#5c7a8a",
]


def available_projects(program: Program, tasks: list[Task]) -> list[Project]:
    by_name: dict[str, Project] = {p.name: p for p in (program.projects or [])}
    for task in tasks:
        if task.project and task.project not in by_name:
            by_name[task.project] = Project(name=task.project, color=DEFAULT_PROJECT_COLOR)
    return sorted(by_name.values(), key=lambda p: p.name.lower())


def project_colors(program: Program, tasks: list[Task]) -> dict[str, str]:
    return {p.name: p.color for p in available_projects(program, tasks)}


def register_project(workspace: Path, name: str) -> None:
    name = (name or "").strip()
    if not name:
        return
    program = load_program(workspace)
    if not any(p.name == name for p in program.projects):
        used = {p.color for p in program.projects}
        color = next((c for c in PROJECT_PALETTE if c not in used), DEFAULT_PROJECT_COLOR)
        program.projects.append(Project(name=name, color=color))
        save_program(workspace, program)


def upsert_project(workspace: Path, name: str, description: str = "", color: str = "") -> None:
    name = (name or "").strip()
    if not name:
        return
    program = load_program(workspace)
    color = (color or "").strip() or DEFAULT_PROJECT_COLOR
    for project in program.projects:
        if project.name == name:
            project.description = description
            project.color = color
            break
    else:
        program.projects.append(Project(name=name, description=description, color=color))
    save_program(workspace, program)


def load_task(workspace: Path, task_id: str) -> Task:
    return Task.model_validate(load_json(workspace / "tasks" / f"{task_id}.json"))


def load_all_tasks(workspace: Path) -> list[Task]:
    ensure_workspace(workspace)
    tasks: list[Task] = []
    for path in sorted((workspace / "tasks").glob("*.json")):
        tasks.append(Task.model_validate(load_json(path)))
    return tasks


def save_task(workspace: Path, task: Task) -> None:
    ensure_workspace(workspace)
    save_json(workspace / "tasks" / f"{task.id}.json", task.model_dump(mode="json"))


def _generate_task_id() -> str:
    raw = secrets.token_hex(8)  # 16 hex characters
    return "-".join(raw[i : i + 4] for i in range(0, 16, 4)).upper()


def next_task_id(workspace: Path) -> str:
    tasks_dir = workspace / "tasks"
    for _ in range(1000):
        candidate = _generate_task_id()
        if not (tasks_dir / f"{candidate}.json").exists():
            return candidate
    raise WorkspaceError("Unable to generate a unique task id")


def create_task(workspace: Path, title: str = "New task") -> Task:
    task = Task(id=next_task_id(workspace), title=title or "New task")
    save_task(workspace, task)
    return task


def delete_task(workspace: Path, task_id: str) -> None:
    path = workspace / "tasks" / f"{task_id}.json"
    if path.exists():
        path.unlink()


def add_note(workspace: Path, task_id: str, body: str) -> Task:
    task = load_task(workspace, task_id)
    if body.strip():
        task.notes.append(Note(body=body.strip()))
        save_task(workspace, task)
    return task


def add_attachment(workspace: Path, task_id: str, filename: str, content: bytes, content_type: str | None = None, description: str = "") -> Task:
    task = load_task(workspace, task_id)
    safe_name = Path(filename).name
    target_dir = workspace / "assets" / task_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / safe_name
    target_path.write_bytes(content)
    kind = "image" if (content_type or "").startswith("image/") else "file"
    task.attachments.append(
        Attachment(filename=safe_name, path=f"assets/{task_id}/{safe_name}", kind=kind, description=description.strip())
    )
    save_task(workspace, task)
    return task


# --- Milestones -------------------------------------------------------------


def load_milestone(workspace: Path, milestone_id: str) -> Milestone:
    return Milestone.model_validate(load_json(workspace / "milestones" / f"{milestone_id}.json"))


def load_all_milestones(workspace: Path) -> list[Milestone]:
    ensure_workspace(workspace)
    milestones: list[Milestone] = []
    for path in sorted((workspace / "milestones").glob("*.json")):
        milestones.append(Milestone.model_validate(load_json(path)))
    return milestones


def save_milestone(workspace: Path, milestone: Milestone) -> None:
    ensure_workspace(workspace)
    save_json(workspace / "milestones" / f"{milestone.id}.json", milestone.model_dump(mode="json"))


def next_milestone_id(workspace: Path) -> str:
    milestones_dir = workspace / "milestones"
    for _ in range(1000):
        candidate = "M-" + "-".join(
            secrets.token_hex(4)[i : i + 4] for i in range(0, 8, 4)
        ).upper()
        if not (milestones_dir / f"{candidate}.json").exists():
            return candidate
    raise WorkspaceError("Unable to generate a unique milestone id")


def create_milestone(workspace: Path, title: str = "New milestone") -> Milestone:
    milestone = Milestone(id=next_milestone_id(workspace), title=title or "New milestone")
    save_milestone(workspace, milestone)
    return milestone


def delete_milestone(workspace: Path, milestone_id: str) -> None:
    path = workspace / "milestones" / f"{milestone_id}.json"
    if path.exists():
        path.unlink()
    assets = workspace / "assets" / milestone_id
    if assets.exists():
        shutil.rmtree(assets, ignore_errors=True)


def add_milestone_note(workspace: Path, milestone_id: str, body: str) -> Milestone:
    milestone = load_milestone(workspace, milestone_id)
    if body.strip():
        milestone.notes.append(Note(body=body.strip()))
        save_milestone(workspace, milestone)
    return milestone


def add_milestone_attachment(
    workspace: Path,
    milestone_id: str,
    filename: str,
    content: bytes,
    content_type: str | None = None,
    description: str = "",
) -> Milestone:
    milestone = load_milestone(workspace, milestone_id)
    safe_name = Path(filename).name
    target_dir = workspace / "assets" / milestone_id
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / safe_name).write_bytes(content)
    kind = "image" if (content_type or "").startswith("image/") else "file"
    milestone.attachments.append(
        Attachment(
            filename=safe_name,
            path=f"assets/{milestone_id}/{safe_name}",
            kind=kind,
            description=description.strip(),
        )
    )
    save_milestone(workspace, milestone)
    return milestone


def add_task_to_milestone(workspace: Path, milestone_id: str, task_id: str) -> Milestone:
    milestone = load_milestone(workspace, milestone_id)
    if task_id and task_id not in milestone.task_ids:
        if (workspace / "tasks" / f"{task_id}.json").exists():
            milestone.task_ids.append(task_id)
            save_milestone(workspace, milestone)
    return milestone


def remove_task_from_milestone(workspace: Path, milestone_id: str, task_id: str) -> Milestone:
    milestone = load_milestone(workspace, milestone_id)
    if task_id in milestone.task_ids:
        milestone.task_ids.remove(task_id)
        save_milestone(workspace, milestone)
    return milestone


def move_task_in_milestone(workspace: Path, milestone_id: str, task_id: str, direction: str) -> Milestone:
    milestone = load_milestone(workspace, milestone_id)
    ids = milestone.task_ids
    if task_id in ids:
        idx = ids.index(task_id)
        swap = idx - 1 if direction == "up" else idx + 1
        if 0 <= swap < len(ids):
            ids[idx], ids[swap] = ids[swap], ids[idx]
            save_milestone(workspace, milestone)
    return milestone


def copy_starter_files(target: Path) -> None:
    init_workspace(target, with_sample=True)
    readme = target / "README.md"
    if not readme.exists():
        readme.write_text(
            "# My Taskwright Workspace\n\n"
            "Run `taskwright serve` from this folder to launch the local dashboard.\n",
            encoding="utf-8",
        )
    gitignore = target / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(".venv/\n__pycache__/\n*.pyc\n", encoding="utf-8")


def _git(workspace: Path, *args: str, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def git_status(workspace: Path) -> dict[str, Any]:
    info: dict[str, Any] = {
        "tracked": False,
        "branch": "",
        "upstream": None,
        "ahead": 0,
        "behind": 0,
        "dirty": 0,
        "message": "",
    }
    try:
        inside = _git(workspace, "rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return info
        info["tracked"] = True
        info["branch"] = _git(workspace, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        upstream = _git(workspace, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        if upstream.returncode == 0:
            info["upstream"] = upstream.stdout.strip()
            counts = _git(workspace, "rev-list", "--left-right", "--count", "@{u}...HEAD")
            if counts.returncode == 0:
                parts = counts.stdout.split()
                if len(parts) == 2:
                    info["behind"] = int(parts[0])
                    info["ahead"] = int(parts[1])
        status = _git(workspace, "status", "--porcelain", "--", ".")
        if status.returncode == 0:
            info["dirty"] = len([line for line in status.stdout.splitlines() if line.strip()])
    except (OSError, subprocess.SubprocessError) as exc:
        info["message"] = str(exc)
    return info


def git_sync(workspace: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "message": ""}
    status = git_status(workspace)
    if not status["tracked"]:
        result["message"] = "This workspace is not inside a git repository."
        return result
    try:
        if status["dirty"]:
            _git(workspace, "add", "-A", "--", ".")
            commit = _git(
                workspace, "commit", "-m", f"taskwright: sync workspace ({datetime.now():%Y-%m-%d %H:%M})"
            )
            if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr).lower():
                result["message"] = "Commit failed: " + (commit.stderr.strip() or commit.stdout.strip())
                return result
        if not status["upstream"]:
            result["ok"] = True
            result["message"] = "Committed locally. No upstream is configured, so nothing was pushed."
            return result
        pull = _git(workspace, "pull", "--no-edit")
        if pull.returncode != 0:
            result["message"] = "Pull failed: " + (pull.stderr.strip() or pull.stdout.strip())
            return result
        push = _git(workspace, "push")
        if push.returncode != 0:
            result["message"] = "Push failed: " + (push.stderr.strip() or push.stdout.strip())
            return result
        result["ok"] = True
        result["message"] = f"Synced with {status['upstream']}."
    except (OSError, subprocess.SubprocessError) as exc:
        result["message"] = str(exc)
    return result
