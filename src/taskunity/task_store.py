from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Attachment, Milestone, Note, Project, Task, TaskActivityEvent

DEFAULT_WORKSPACE_APP_NAME = "Taskunity"
DEFAULT_WORKSPACE_DESCRIPTION = "Local file-backed workspace/task tracker"


class WorkspaceError(RuntimeError):
    pass


# Compiled once: task/milestone IDs are uppercase hex words joined by dashes.
_ID_RE = re.compile(r'^[A-Z0-9][A-Z0-9\-]*[A-Z0-9]$|^[A-Z0-9]$')


def _safe_id(value: str, label: str = "id") -> str:
    """Validate *value* is a safe ID token, otherwise raise WorkspaceError.

    This is a lightweight pre-check.  Actual path confinement is enforced by
    ``_safe_subpath``; always prefer that function when constructing file paths.
    """
    clean = (value or "").strip()
    if not clean or not _ID_RE.match(clean) or ".." in clean or "/" in clean or "\\" in clean:
        raise WorkspaceError(f"Invalid {label}: {value!r}")
    return clean


def _safe_subpath(base: Path, *parts: str) -> Path:
    """Build ``base / parts`` and verify the result is confined within *base*.

    Uses ``os.path.normpath`` to collapse ``..`` segments so that a crafted
    component such as ``../../etc/passwd`` resolves outside the workspace and is
    rejected.  This is the canonical path-injection mitigation pattern.
    """
    joined = os.path.join(str(base), *[str(p) for p in parts])
    normed = os.path.normpath(joined)
    base_str = os.path.normpath(str(base))
    if normed != base_str and not normed.startswith(base_str + os.sep):
        raise WorkspaceError(f"Path traversal detected in: {parts!r}")
    return Path(normed)


def workspace_paths(workspace: Path) -> dict[str, Path]:
    return {
        "root": workspace,
        "projects": workspace / "projects",
        "tasks": workspace / "tasks",
        "milestones": workspace / "milestones",
        "assets": workspace / "assets",
    }


def ensure_workspace(workspace: Path) -> None:
    paths = workspace_paths(workspace)
    paths["root"].mkdir(parents=True, exist_ok=True)
    paths["projects"].mkdir(parents=True, exist_ok=True)
    paths["tasks"].mkdir(parents=True, exist_ok=True)
    paths["milestones"].mkdir(parents=True, exist_ok=True)
    paths["assets"].mkdir(parents=True, exist_ok=True)
    config = workspace / "config.json"
    if not config.exists():
        save_json(config, default_workspace_config(workspace))


def init_workspace(workspace: Path, with_sample: bool = True) -> None:
    ensure_workspace(workspace)
    return


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


def _slugify_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug or "default"


def _generate_project_id() -> str:
    raw = secrets.token_hex(8)
    return "P-" + "-".join(raw[i : i + 4] for i in range(0, 16, 4)).upper()


def next_project_id(workspace: Path) -> str:
    projects_dir = workspace / "projects"
    for _ in range(1000):
        candidate = _generate_project_id()
        if not (projects_dir / f"{candidate}.json").exists():
            return candidate
    raise WorkspaceError("Unable to generate a unique project id")


def _project_filename(project_id: str) -> str:
    clean = _safe_id(project_id, "project id")
    return f"{clean}.json"


def load_project(workspace: Path, project_id: str) -> Project:
    project = Project.model_validate(load_json(workspace / "projects" / _project_filename(project_id)))
    if not project.id:
        project.id = _safe_id(project_id, "project id")
    return project


def load_all_projects(workspace: Path) -> list[Project]:
    ensure_workspace(workspace)
    projects_dir = workspace / "projects"
    projects: list[Project] = []
    seen_ids: set[str] = set()
    for path in sorted(projects_dir.glob("*.json"), key=lambda p: p.name.lower()):
        raw = load_json(path)
        project = Project.model_validate(raw)
        if not project.id or project.id in seen_ids:
            project.id = next_project_id(workspace)
        seen_ids.add(project.id)
        canonical = projects_dir / _project_filename(project.id)
        if path != canonical:
            save_json(canonical, project.model_dump(mode="json"))
            try:
                path.unlink()
            except OSError:
                pass
        elif raw.get("id") != project.id:
            save_json(canonical, project.model_dump(mode="json"))
        projects.append(project)
    return sorted(projects, key=lambda p: p.name.lower())


def save_project(workspace: Path, project: Project, *, previous_id: str = "") -> None:
    ensure_workspace(workspace)
    if not project.id:
        project.id = next_project_id(workspace)
    target = workspace / "projects" / _project_filename(project.id)
    save_json(target, project.model_dump(mode="json"))
    old = (previous_id or "").strip()
    if old and old != project.id:
        old_path = workspace / "projects" / _project_filename(old)
        if old_path.exists():
            old_path.unlink()


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


def available_projects(projects: list[Project], tasks: list[Task]) -> list[Project]:
    by_id: dict[str, Project] = {p.id: p for p in projects if p.id}
    by_name: dict[str, Project] = {p.name: p for p in projects}
    for task in tasks:
        if task.project_id and task.project_id in by_id:
            task.project = by_id[task.project_id].name
            continue
        if task.project and task.project not in by_name:
            by_name[task.project] = Project(name=task.project, color=DEFAULT_PROJECT_COLOR)
    merged = list(by_id.values())
    for project in by_name.values():
        if project.id and any(p.id == project.id for p in merged):
            continue
        if any(p.name == project.name for p in merged):
            continue
        merged.append(project)
    return sorted(merged, key=lambda p: p.name.lower())


def project_colors(projects: list[Project], tasks: list[Task]) -> dict[str, str]:
    colors: dict[str, str] = {}
    for project in available_projects(projects, tasks):
        if project.name:
            colors[project.name] = project.color
        if project.id:
            colors[project.id] = project.color
    return colors


def normalize_task_project_refs(workspace: Path) -> None:
    projects = load_all_projects(workspace)
    by_id = {p.id: p for p in projects if p.id}
    by_name = {p.name: p for p in projects}
    changed = False
    for task in load_all_tasks(workspace):
        original_id = task.project_id
        original_name = task.project
        if task.project_id and task.project_id in by_id:
            task.project = by_id[task.project_id].name
        elif task.project and task.project in by_name:
            task.project_id = by_name[task.project].id
            task.project = by_name[task.project].name
        elif task.project:
            register_project(workspace, task.project)
            refreshed = {p.name: p for p in load_all_projects(workspace)}
            if task.project in refreshed:
                task.project_id = refreshed[task.project].id
                task.project = refreshed[task.project].name
        if task.project_id != original_id or task.project != original_name:
            save_task(workspace, task)
            changed = True
    if changed:
        load_all_projects(workspace)


def register_project(workspace: Path, name: str) -> Project | None:
    name = (name or "").strip()
    if not name:
        return None
    projects = load_all_projects(workspace)
    existing = next((p for p in projects if p.name == name), None)
    if existing:
        return existing
    used = {p.color for p in projects}
    color = next((c for c in PROJECT_PALETTE if c not in used), DEFAULT_PROJECT_COLOR)
    project = Project(id=next_project_id(workspace), name=name, color=color)
    save_project(workspace, project)
    return project


def upsert_project(workspace: Path, name: str, description: str = "", color: str = "", project_id: str = "") -> Project | None:
    name = (name or "").strip()
    if not name:
        return None
    color = (color or "").strip() or DEFAULT_PROJECT_COLOR
    projects = load_all_projects(workspace)
    project = None
    if project_id:
        project = next((item for item in projects if item.id == project_id), None)
    if project is None:
        project = next((item for item in projects if item.name == name), None)
    if project is None:
        project = Project(id=next_project_id(workspace), name=name)
    project.description = description
    project.color = color
    project.name = name
    save_project(workspace, project)
    return project


def workspace_label(workspace: Path) -> str:
    config_name = (load_workspace_config(workspace).get("workspace_name") or "").strip()
    if config_name:
        return config_name
    return _workspace_label_from_path(workspace)


def _workspace_label_from_path(workspace: Path) -> str:
    label = (workspace.name or DEFAULT_WORKSPACE_APP_NAME).replace("_", " ").replace("-", " ").strip()
    return label.title() if label else DEFAULT_WORKSPACE_APP_NAME


def default_workspace_config(workspace: Path) -> dict[str, str]:
    workspace_name = _workspace_label_from_path(workspace)
    return {
        "app_name": DEFAULT_WORKSPACE_APP_NAME,
        "workspace_name": workspace_name,
        "workspace_description": DEFAULT_WORKSPACE_DESCRIPTION,
        "export_title": workspace_name,
    }


def load_workspace_config(workspace: Path) -> dict[str, str]:
    ensure_workspace(workspace)
    defaults = default_workspace_config(workspace)
    raw = load_json(workspace / "config.json")
    config: dict[str, str] = {}
    for key, fallback in defaults.items():
        value = raw.get(key)
        stripped = value.strip() if isinstance(value, str) else ""
        config[key] = stripped or fallback
    return config


def _task_dir(workspace: Path, task_id: str) -> Path:
    clean = _safe_id(task_id, "task id")
    return _safe_subpath(workspace / "tasks", clean)


def _task_json_path(workspace: Path, task_id: str) -> Path:
    return _safe_subpath(_task_dir(workspace, task_id), "task.json")


def _task_legacy_json_path(workspace: Path, task_id: str) -> Path:
    clean = _safe_id(task_id, "task id")
    return _safe_subpath(workspace / "tasks", f"{clean}.json")


def _task_assets_dir(workspace: Path, task_id: str) -> Path:
    return _safe_subpath(_task_dir(workspace, task_id), "assets")


def _task_activity_log_path(workspace: Path, task_id: str) -> Path:
    return _safe_subpath(_task_dir(workspace, task_id), "activity.log")


def _task_public_asset_relpath(task_id: str, filename: str) -> str:
    clean = _safe_id(task_id, "task id")
    return f"task-files/{clean}/assets/{Path(filename).name}"


def _normalize_task_asset_paths(task: Task) -> bool:
    changed = False
    legacy_prefix = f"assets/{_safe_id(task.id, 'task id')}/"
    for attachment in task.attachments:
        path = attachment.path or ""
        if path.startswith(legacy_prefix):
            tail = path[len(legacy_prefix) :]
            attachment.path = _task_public_asset_relpath(task.id, tail)
            changed = True
    for event in task.activity:
        path = event.image_path or ""
        if path.startswith(legacy_prefix):
            tail = path[len(legacy_prefix) :]
            event.image_path = _task_public_asset_relpath(task.id, tail)
            changed = True
    return changed


def _write_task_activity_log(workspace: Path, task: Task) -> None:
    log_path = _task_activity_log_path(workspace, task.id)
    lines: list[str] = []
    for note in task.notes:
        lines.append(f"{note.created_at} | note | {note.body}")
    for attachment in task.attachments:
        lines.append(
            f"{attachment.uploaded_at} | attachment | {attachment.filename} | {attachment.path}"
        )
    for event in task.activity:
        if event.event_type == "progress_update":
            lines.append(
                f"{event.created_at} | progress_update | {event.progress_before}->{event.progress_after}"
            )
        elif event.event_type == "image":
            details = event.image_filename or event.image_path or ""
            lines.append(f"{event.created_at} | image | {details}")
        else:
            lines.append(f"{event.created_at} | note | {event.note_text or ''}")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _migrate_task_storage(workspace: Path, task: Task) -> None:
    legacy = _task_legacy_json_path(workspace, task.id)
    target_json = _task_json_path(workspace, task.id)
    target_json.parent.mkdir(parents=True, exist_ok=True)
    _normalize_task_asset_paths(task)

    if legacy.exists() and not target_json.exists():
        save_json(target_json, task.model_dump(mode="json", exclude={"project"}))
        try:
            legacy.unlink()
        except OSError:
            pass

    legacy_assets = _safe_subpath(workspace / "assets", _safe_id(task.id, "task id"))
    new_assets = _task_assets_dir(workspace, task.id)
    if legacy_assets.exists() and not new_assets.exists():
        new_assets.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy_assets), str(new_assets))


def load_task(workspace: Path, task_id: str) -> Task:
    ensure_workspace(workspace)
    clean = _safe_id(task_id, "task id")
    folder_json = _task_json_path(workspace, clean)
    legacy_json = _task_legacy_json_path(workspace, clean)
    if folder_json.exists():
        task = Task.model_validate(load_json(folder_json))
        if _normalize_task_asset_paths(task):
            save_task(workspace, task)
        return task
    if legacy_json.exists():
        task = Task.model_validate(load_json(legacy_json))
        _migrate_task_storage(workspace, task)
        _write_task_activity_log(workspace, task)
        return task
    raise WorkspaceError(f"Missing task: {task_id}")


def load_all_tasks(workspace: Path) -> list[Task]:
    ensure_workspace(workspace)
    tasks: list[Task] = []
    tasks_dir = workspace / "tasks"
    ids: set[str] = set()
    for path in tasks_dir.glob("*.json"):
        if path.name.endswith("task.json"):
            continue
        ids.add(path.stem)
    for path in tasks_dir.iterdir():
        if path.is_dir():
            task_json = path / "task.json"
            if task_json.exists():
                ids.add(path.name)

    for task_id in sorted(ids):
        try:
            task = load_task(workspace, task_id)
        except WorkspaceError:
            continue
        tasks.append(task)
    return tasks


def save_task(workspace: Path, task: Task) -> None:
    ensure_workspace(workspace)
    clean = _safe_id(task.id, "task id")
    task.id = clean
    _task_dir(workspace, clean).mkdir(parents=True, exist_ok=True)
    save_json(_task_json_path(workspace, clean), task.model_dump(mode="json", exclude={"project"}))
    legacy_json = _task_legacy_json_path(workspace, clean)
    if legacy_json.exists():
        try:
            legacy_json.unlink()
        except OSError:
            pass
    _write_task_activity_log(workspace, task)


def _generate_task_id() -> str:
    raw = secrets.token_hex(8)  # 16 hex characters
    return "-".join(raw[i : i + 4] for i in range(0, 16, 4)).upper()


def next_task_id(workspace: Path) -> str:
    tasks_dir = workspace / "tasks"
    for _ in range(1000):
        candidate = _generate_task_id()
        if not (tasks_dir / f"{candidate}.json").exists() and not (tasks_dir / candidate).exists():
            return candidate
    raise WorkspaceError("Unable to generate a unique task id")


def create_task(workspace: Path, title: str = "New task") -> Task:
    task = Task(id=next_task_id(workspace), title=title or "New task")
    save_task(workspace, task)
    return task


def delete_task(workspace: Path, task_id: str) -> None:
    clean = _safe_id(task_id, "task id")
    legacy = _task_legacy_json_path(workspace, clean)
    if legacy.exists():
        legacy.unlink()
    task_dir = _task_dir(workspace, clean)
    if task_dir.exists():
        shutil.rmtree(task_dir, ignore_errors=True)
    assets = _safe_subpath(workspace / "assets", clean)
    if assets.exists():
        shutil.rmtree(assets, ignore_errors=True)


def add_note(workspace: Path, task_id: str, body: str) -> Task:
    task = load_task(workspace, task_id)
    if body.strip():
        task.notes.append(Note(body=body.strip()))
        save_task(workspace, task)
    return task


def add_attachment(workspace: Path, task_id: str, filename: str, content: bytes, content_type: str | None = None, description: str = "") -> Task:
    task = load_task(workspace, task_id)
    safe_name = Path(filename).name  # cross-platform: strips any leading path component
    target_dir = _task_assets_dir(workspace, task.id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = _safe_subpath(target_dir, safe_name)
    target_path.write_bytes(content)
    kind = "image" if (content_type or "").startswith("image/") else "file"
    rel = _task_public_asset_relpath(task.id, safe_name)
    task.attachments.append(
        Attachment(filename=safe_name, path=rel, kind=kind, description=description.strip())
    )
    save_task(workspace, task)
    return task


def add_task_activity_note(workspace: Path, task_id: str, body: str) -> Task:
    task = load_task(workspace, task_id)
    if body.strip():
        task.activity.append(TaskActivityEvent(event_type="note", note_text=body.strip()))
        save_task(workspace, task)
    return task


def add_task_activity_image(
    workspace: Path,
    task_id: str,
    filename: str,
    content: bytes,
    content_type: str | None = None,
    description: str = "",
) -> Task:
    task = load_task(workspace, task_id)
    safe_name = Path(filename).name  # cross-platform: strips any leading path component
    target_dir = _task_assets_dir(workspace, task.id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = _safe_subpath(target_dir, safe_name)
    target_path.write_bytes(content)
    rel = _task_public_asset_relpath(task.id, safe_name)
    description_text = description.strip() or None
    task.activity.append(
        TaskActivityEvent(
            event_type="image",
            image_path=rel,
            image_filename=safe_name,
            note_text=description_text,
        )
    )
    save_task(workspace, task)
    return task


def log_progress_change(workspace_path: Path, task: Task, old_progress: int, new_progress: int) -> None:
    if old_progress != new_progress:
        task.activity.append(
            TaskActivityEvent(
                event_type="progress_update",
                progress_before=old_progress,
                progress_after=new_progress,
            )
        )


# --- Milestones -------------------------------------------------------------


def load_milestone(workspace: Path, milestone_id: str) -> Milestone:
    return Milestone.model_validate(load_json(_safe_subpath(workspace / "milestones", f"{milestone_id}.json")))


def load_all_milestones(workspace: Path) -> list[Milestone]:
    ensure_workspace(workspace)
    milestones: list[Milestone] = []
    for path in sorted((workspace / "milestones").glob("*.json")):
        milestones.append(Milestone.model_validate(load_json(path)))
    return milestones


def save_milestone(workspace: Path, milestone: Milestone) -> None:
    ensure_workspace(workspace)
    save_json(
        _safe_subpath(workspace / "milestones", f"{milestone.id}.json"),
        milestone.model_dump(mode="json", exclude={"projects"}),
    )


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
    path = _safe_subpath(workspace / "milestones", f"{milestone_id}.json")
    if path.exists():
        path.unlink()
    assets = _safe_subpath(workspace / "assets", milestone_id)
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
    safe_name = Path(filename).name  # cross-platform: strips any leading path component
    target_dir = _safe_subpath(workspace / "assets", milestone_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = _safe_subpath(target_dir, safe_name)
    target_path.write_bytes(content)
    rel = os.path.relpath(str(target_path), str(workspace))
    if rel.startswith(".."):
        raise WorkspaceError(f"Attachment path escapes workspace: {rel!r}")
    kind = "image" if (content_type or "").startswith("image/") else "file"
    milestone.attachments.append(
        Attachment(
            filename=safe_name,
            path=rel,
            kind=kind,
            description=description.strip(),
        )
    )
    save_milestone(workspace, milestone)
    return milestone


def add_task_to_milestone(workspace: Path, milestone_id: str, task_id: str) -> Milestone:
    milestone = load_milestone(workspace, milestone_id)
    if task_id and task_id not in milestone.task_ids:
        task_exists = _task_json_path(workspace, task_id).exists() or _task_legacy_json_path(workspace, task_id).exists()
        if task_exists:
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
    init_workspace(target, with_sample=False)
    readme = target / "README.md"
    if not readme.exists():
        readme.write_text(
            "# My Taskunity Workspace\n\n"
            "Run `taskunity serve` from this folder to launch the local dashboard.\n",
            encoding="utf-8",
        )


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
        "repo_root": None,
    }
    try:
        inside = _git(workspace, "rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            info["message"] = "Workspace is not inside a git repository."
            return info
        top_level = _git(workspace, "rev-parse", "--show-toplevel")
        if top_level.returncode != 0:
            return info
        info["repo_root"] = Path(top_level.stdout.strip()).resolve()
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
        result["message"] = status["message"] or "This workspace is not inside a git repository."
        return result
    try:
        branch = status["branch"] or _git(workspace, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"

        if status["dirty"]:
            _git(workspace, "add", "-A", "--", ".")
            commit = _git(
                workspace, "commit", "-m", f"taskunity: sync workspace ({datetime.now():%Y-%m-%d %H:%M})"
            )
            if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr).lower():
                result["message"] = "Commit failed: " + (commit.stderr.strip() or commit.stdout.strip())
                return result

        # Fresh/empty repo safety: if there is no HEAD commit yet, create one so push can succeed.
        has_head = _git(workspace, "rev-parse", "--verify", "HEAD").returncode == 0
        if not has_head:
            init_commit = _git(
                workspace,
                "commit",
                "--allow-empty",
                "-m",
                f"taskunity: initialize workspace ({datetime.now():%Y-%m-%d %H:%M})",
            )
            if init_commit.returncode != 0 and "nothing to commit" not in (init_commit.stdout + init_commit.stderr).lower():
                result["message"] = "Commit failed: " + (init_commit.stderr.strip() or init_commit.stdout.strip())
                return result

        upstream = status["upstream"]
        if upstream:
            upstream_ref = _git(workspace, "show-ref", "--verify", "--quiet", f"refs/remotes/{upstream}")
            if upstream_ref.returncode != 0:
                upstream = None

        if not upstream:
            origin = _git(workspace, "remote", "get-url", "origin")
            if origin.returncode != 0:
                result["ok"] = True
                result["message"] = "Committed locally. No upstream is configured and no 'origin' remote was found."
                return result
            set_upstream = _git(workspace, "push", "-u", "origin", branch)
            if set_upstream.returncode != 0:
                result["message"] = "Push failed: " + (set_upstream.stderr.strip() or set_upstream.stdout.strip())
                return result
            result["ok"] = True
            result["message"] = f"Synced and set upstream to origin/{branch}."
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


def git_lfs_available(workspace: Path) -> bool:
    """Return True if git-lfs is installed and accessible."""
    try:
        result = subprocess.run(
            ["git", "lfs", "version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def git_lfs_init(workspace: Path) -> dict[str, Any]:
    """Initialize git-lfs in the workspace: run `git lfs install` and track assets."""
    result: dict[str, Any] = {"ok": False, "message": ""}
    if not git_lfs_available(workspace):
        result["message"] = "git-lfs is not installed or not on PATH."
        return result
    status = git_status(workspace)
    if not status["tracked"]:
        result["message"] = status["message"] or "Workspace is not a git repository."
        return result
    try:
        install = _git(workspace, "lfs", "install", "--local")
        if install.returncode != 0:
            result["message"] = "git lfs install failed: " + (install.stderr.strip() or install.stdout.strip())
            return result
        track = _git(workspace, "lfs", "track", "assets/**")
        if track.returncode != 0:
            result["message"] = "git lfs track failed: " + (track.stderr.strip() or track.stdout.strip())
            return result
        add = _git(workspace, "add", ".gitattributes")
        if add.returncode != 0:
            result["message"] = "git add .gitattributes failed"
            return result
        commit = _git(workspace, "commit", "-m", "chore: initialize git-lfs tracking for assets")
        if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr).lower():
            result["message"] = "Commit failed: " + (commit.stderr.strip() or commit.stdout.strip())
            return result
        result["ok"] = True
        result["message"] = "git-lfs initialized. Assets directory is now tracked with LFS."
    except (OSError, subprocess.SubprocessError) as exc:
        result["message"] = str(exc)
    return result


def git_lfs_status(workspace: Path) -> dict[str, Any]:
    """Return LFS status information for the workspace."""
    info: dict[str, Any] = {"available": False, "enabled": False, "tracking_assets": False, "message": ""}
    info["available"] = git_lfs_available(workspace)
    if not info["available"]:
        return info
    status = git_status(workspace)
    if not status["tracked"]:
        return info
    try:
        repo_root = status.get("repo_root") or workspace
        lfs_hooks = repo_root / ".git" / "hooks" / "pre-push"
        info["enabled"] = lfs_hooks.exists()
        gitattributes = workspace / ".gitattributes"
        if gitattributes.exists():
            content = gitattributes.read_text(encoding="utf-8", errors="replace")
            info["tracking_assets"] = "assets/**" in content or "assets/" in content
    except (OSError, IOError) as exc:
        info["message"] = str(exc)
    return info
