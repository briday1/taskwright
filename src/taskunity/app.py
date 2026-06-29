from __future__ import annotations

import csv
import html as html_lib
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import markdown as markdown_lib
from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .models import ChecklistItem, Milestone, Note, Project, Task, TaskActivityEvent
from .render import (
    SORTS,
    STATUSES,
    build_calendar,
    dashboard_model,
    filter_tasks,
    hide_stale_closed_tasks,
    milestone_rollup,
    sort_tasks,
    tasks_to_jsonantt,
)
from .task_store import (
    add_milestone_attachment,
    add_milestone_note,
    add_task_activity_image,
    add_task_activity_note,
    add_task_to_milestone,
    available_projects,
    create_milestone,
    create_task,
    delete_milestone,
    delete_project,
    delete_task,
    ensure_workspace,
    git_lfs_init,
    git_lfs_status,
    git_status,
    git_sync,
    load_all_milestones,
    load_all_projects,
    load_all_tasks,
    load_milestone,
    load_project,
    load_task,
    load_workspace_config,
    log_progress_change,
    normalize_task_project_refs,
    project_colors,
    register_project,
    remove_task_from_milestone,
    save_milestone,
    save_project,
    save_task,
    save_workspace_config,
    upsert_project,
)

PACKAGE_DIR = Path(__file__).parent


def markdown_filter(text: str) -> str:
    return markdown_lib.markdown(text or "", extensions=["extra", "sane_lists"])


def build_task_activity_entries(task: Task | None) -> list[dict[str, object]]:
    if task is None:
        return []

    entries: list[dict[str, object]] = []
    for note in task.notes:
        entries.append(
            {
                "kind": "note",
                "created_at": note.created_at,
                "body": note.body,
                "filename": None,
                "path": None,
                "is_image": False,
                "progress_before": None,
                "progress_after": None,
            }
        )
    for attachment in task.attachments:
        entries.append(
            {
                "kind": "image" if attachment.kind == "image" else "file",
                "created_at": attachment.uploaded_at,
                "body": attachment.description,
                "filename": attachment.filename,
                "path": attachment.path,
                "is_image": attachment.kind == "image",
                "progress_before": None,
                "progress_after": None,
            }
        )
    for event in task.activity:
        if event.event_type == "progress_update":
            entries.append(
                {
                    "kind": "progress_update",
                    "created_at": event.created_at,
                    "body": None,
                    "filename": None,
                    "path": None,
                    "is_image": False,
                    "progress_before": event.progress_before,
                    "progress_after": event.progress_after,
                }
            )
        elif event.event_type == "image":
            image_name = event.image_filename or event.image_path or ""
            is_image = Path(image_name).suffix.lower() in {
                ".png",
                ".jpg",
                ".jpeg",
                ".gif",
                ".webp",
                ".bmp",
                ".svg",
            }
            entries.append(
                {
                    "kind": "image" if is_image else "file",
                    "created_at": event.created_at,
                    "body": event.note_text,
                    "filename": event.image_filename,
                    "path": event.image_path,
                    "is_image": is_image,
                    "progress_before": None,
                    "progress_after": None,
                }
            )
        else:
            entries.append(
                {
                    "kind": "note",
                    "created_at": event.created_at,
                    "body": event.note_text,
                    "filename": None,
                    "path": None,
                    "is_image": False,
                    "progress_before": None,
                    "progress_after": None,
                }
            )

    return sorted(entries, key=lambda item: str(item.get("created_at") or ""), reverse=True)


def _parse_event_datetime(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_event_points(points: list[dict[str, object]], fallback_iso: str) -> list[dict[str, object]]:
    ordered: list[tuple[datetime, int, dict[str, object]]] = []
    fallback_dt = _parse_event_datetime(fallback_iso) or datetime.now()
    for index, point in enumerate(points):
        dt = _parse_event_datetime(str(point.get("created_at") or "")) or fallback_dt
        ordered.append((dt, index, point))

    ordered.sort(key=lambda item: (item[0], item[1]))
    normalized: list[dict[str, object]] = []
    last_dt: datetime | None = None
    for dt, _, point in ordered:
        if last_dt is not None and dt <= last_dt:
            dt = last_dt + timedelta(seconds=1)
        last_dt = dt
        normalized.append(
            {
                "x": dt.isoformat(timespec="seconds"),
                "y": point.get("y", 100),
                "label": point.get("label", ""),
                "event_type": point.get("event_type", "update"),
                "preview_title": point.get("preview_title", ""),
                "preview_body": point.get("preview_body", ""),
                "preview_path": point.get("preview_path", ""),
                "is_image": bool(point.get("is_image")),
            }
        )
    return normalized


def _clip_progress(value: int | None, fallback: int = 0) -> int:
    try:
        raw = int(value if value is not None else fallback)
    except (TypeError, ValueError):
        raw = fallback
    return max(0, min(100, raw))


def _summarize_text(value: str | None, max_len: int = 46) -> str:
    text = " ".join((value or "").strip().split())
    if not text:
        return ""
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _preview_text(value: str | None, max_len: int = 180) -> str:
    return _summarize_text(value, max_len)


def create_app(workspace: str | Path = ".") -> FastAPI:
    workspace = Path(workspace).resolve()
    ensure_workspace(workspace)
    initial_config = load_workspace_config(workspace)
    app_name = initial_config["app_name"]

    app = FastAPI(title=app_name)
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    templates.env.filters["markdown"] = markdown_filter

    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
    app.mount("/assets", StaticFiles(directory=str(workspace / "assets")), name="assets")
    app.mount("/task-files", StaticFiles(directory=str(workspace / "tasks")), name="task-files")

    VIEWS = {"list", "board", "gantt", "calendar", "projects", "milestones"}
    STALE_CLOSED_DAYS = 30

    def parse_toggle(value: str | None) -> bool:
        return (value or "").strip().lower() in {"1", "true", "yes", "on"}

    def parse_stale_days(value: str | int | None) -> int:
        try:
            days = int(str(value).strip())
        except (TypeError, ValueError):
            days = STALE_CLOSED_DAYS
        return max(1, days)

    def parse_calendar_month(value: str | int | None) -> int | None:
        try:
            month = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return month if 1 <= month <= 12 else None

    def ui_config() -> dict[str, str]:
        config = load_workspace_config(workspace)
        return {
            "app_name": config["app_name"],
            "workspace_name": config["workspace_name"],
            "workspace_description": config["workspace_description"],
            "export_title": config["export_title"],
        }

    def ai_config() -> dict[str, str]:
        config = load_workspace_config(workspace)
        return {
            "ai_enabled": config.get("ai_enabled", "0"),
            "ai_base_url": config.get("ai_base_url", ""),
            "ai_api_key": config.get("ai_api_key", ""),
            "ai_model": config.get("ai_model", ""),
            "ai_timeout_seconds": config.get("ai_timeout_seconds", "30"),
            "ai_max_tokens": config.get("ai_max_tokens", "2048"),
            "ai_temperature": config.get("ai_temperature", "0.7"),
        }

    def _ai_call(
        messages: list[dict[str, str]],
        cfg: dict[str, str],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict:
        """Call the configured OpenAI-compatible chat completions endpoint."""
        base_url = cfg["ai_base_url"].rstrip("/")
        api_key = cfg["ai_api_key"]
        model = cfg["ai_model"]
        timeout = max(5, min(120, int(cfg.get("ai_timeout_seconds") or "30")))
        if max_tokens is None:
            max_tokens = max(1, int(cfg.get("ai_max_tokens") or "2048"))
        if temperature is None:
            try:
                temperature = float(cfg.get("ai_temperature") or "0.7")
            except ValueError:
                temperature = 0.7

        payload = json.dumps({
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{base_url}/v1/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _ai_fetch_models(cfg: dict[str, str]) -> list[str]:
        """Fetch available models from the configured endpoint."""
        base_url = cfg["ai_base_url"].rstrip("/")
        api_key = cfg["ai_api_key"]
        timeout = max(5, min(30, int(cfg.get("ai_timeout_seconds") or "30")))
        req = urllib.request.Request(
            f"{base_url}/v1/models",
            headers={"Authorization": "Bearer " + api_key},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return sorted(m["id"] for m in data.get("data", []) if m.get("id"))

    def _parse_ai_suggestions(text: str) -> dict:
        """Try to parse structured suggestions from the AI response text.

        Looks for a JSON block fenced with ```json ... ``` or a bare JSON object.
        On parse failure, returns an empty suggestions dict so callers can degrade
        gracefully to plain text rendering.
        """
        suggestions: dict = {}
        # Try fenced code block first
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence_match:
            try:
                suggestions = json.loads(fence_match.group(1))
            except (json.JSONDecodeError, ValueError):
                pass
        # Fall back: try last bare JSON object in text
        if not suggestions:
            for match in re.finditer(r"\{[^{}]+\}", text, re.DOTALL):
                try:
                    parsed = json.loads(match.group())
                    if any(k in parsed for k in ("suggested_tasks", "suggested_checklist_items", "suggested_note")):
                        suggestions = parsed
                except (json.JSONDecodeError, ValueError):
                    continue
        return suggestions

    def _build_task_context(task: Task, all_tasks: list[Task]) -> str:
        task_by_id = {t.id: t for t in all_tasks}
        deps = [task_by_id[d].title for d in task.depends_on if d in task_by_id]
        checklist = [
            f"{'[x]' if item.done else '[ ]'} {item.text}"
            for item in task.checklist
        ]
        notes_preview = [n.body[:200] for n in task.notes[-3:]]
        data = {
            "id": task.id,
            "title": task.title,
            "status": task.status,
            "priority": task.priority,
            "project": task.project or task.project_id or "",
            "summary": task.summary,
            "description": task.description,
            "start_date": task.start_date or "",
            "due_date": task.due_date or "",
            "percent_complete": task.percent_complete,
            "tags": task.tags,
            "depends_on_titles": deps,
            "checklist": checklist,
            "recent_notes": notes_preview,
        }
        return json.dumps(data, indent=2)

    def _build_milestone_context(milestone: Milestone, all_tasks: list[Task]) -> str:
        tasks_by_id = {t.id: t for t in all_tasks}
        milestone_tasks = []
        for tid in milestone.task_ids:
            t = tasks_by_id.get(tid)
            if t:
                milestone_tasks.append({
                    "id": t.id,
                    "title": t.title,
                    "status": t.status,
                    "priority": t.priority,
                    "percent_complete": t.percent_complete,
                    "due_date": t.due_date or "",
                    "project": t.project or "",
                })
        notes_preview = [n.body[:200] for n in milestone.notes[-3:]]
        data = {
            "id": milestone.id,
            "title": milestone.title,
            "status": milestone.status,
            "summary": milestone.summary,
            "description": milestone.description,
            "start_date": milestone.start_date or "",
            "target_date": milestone.target_date or "",
            "task_count": len(milestone.task_ids),
            "tasks": milestone_tasks,
            "recent_notes": notes_preview,
        }
        return json.dumps(data, indent=2)

    _MAX_TASK_SUMMARY_LENGTH = 500

    SYSTEM_PROMPT = """\
You are a planning assistant integrated into Taskunity, a local task management app.
You help users plan, organise, and break down their work.
You only use the context provided — never invent data not given to you.
When suggesting actionable changes, include a JSON block in your response using this format:

```json
{
  "suggested_tasks": [
    {"title": "...", "summary": "...", "priority": "normal"}
  ],
  "suggested_checklist_items": ["item 1", "item 2"],
  "suggested_note": "..."
}
```

Only include the fields that are relevant. Omit the JSON block if no structured changes are needed.
Always provide a human-readable reply_markdown as plain text before or after the JSON block.
Keep responses concise and actionable."""


    def parse_calendar_year(value: str | int | None) -> int | None:
        try:
            year = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return year if 1900 <= year <= 3000 else None

    def _ai_error_html(message: str) -> str:
        escaped = (
            message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        return f'<div class="ai-msg ai-msg-error"><strong>Error:</strong> {escaped}</div>'

    def build_query(
        projects: list[str], date_from: str, date_to: str, q: str, view: str = "", sort: str = "",
        milestone: str = "", show_closed: bool = False, stale_days: int = STALE_CLOSED_DAYS,
        calendar_month: int | None = None, calendar_year: int | None = None, hide_done: bool = False,
        hide_old: bool | None = None, sort_dir: str = "",
    ) -> str:
        params: list[tuple[str, str]] = [("project", p) for p in projects if p]
        if date_from:
            params.append(("date_from", date_from))
        if date_to:
            params.append(("date_to", date_to))
        if q:
            params.append(("q", q))
        if milestone:
            params.append(("milestone", milestone))
        if sort and sort != "priority":
            params.append(("sort", sort))
            if sort_dir in {"asc", "desc"}:
                params.append(("sort_dir", sort_dir))
        if hide_old is None:
            hide_old = not show_closed
        if hide_old:
            params.append(("hide_old", "1"))
        elif show_closed:
            params.append(("show_closed", "1"))
        if hide_done:
            params.append(("hide_done", "1"))
        if stale_days != STALE_CLOSED_DAYS:
            params.append(("stale_days", str(stale_days)))
        if calendar_month is not None:
            params.append(("calendar_month", str(calendar_month)))
        if calendar_year is not None:
            params.append(("calendar_year", str(calendar_year)))
        if view:
            params.append(("view", view))
        return urllib.parse.urlencode(params)

    def context(
        request: Request,
        selected_task: Task | None = None,
        *,
        projects: list[str] | None = None,
        date_from: str = "",
        date_to: str = "",
        q: str = "",
        sort: str = "priority",
        sort_dir: str = "",
        view: str = "list",
        milestone: str = "",
        show_closed: bool = False,
        hide_old: bool | None = None,
        hide_done: bool = False,
        stale_days: int = STALE_CLOSED_DAYS,
        calendar_month: int | None = None,
        calendar_year: int | None = None,
        git_message: str = "",
    ) -> dict:
        projects = [p for p in (projects or []) if p]
        q = (q or "").strip()
        sort = sort if sort in SORTS else "priority"
        query_params = request.query_params
        default_sort_dirs = {
            "priority": "asc",
            "due_date": "asc",
            "title": "asc",
            "status": "asc",
            "percent_complete": "desc",
            "project": "asc",
        }
        sort_dir = (sort_dir or query_params.get("sort_dir") or "").strip().lower()
        if sort_dir not in {"asc", "desc"}:
            sort_dir = default_sort_dirs.get(sort, "asc")
        view = view if view in VIEWS else "list"
        if hide_old is None:
            if query_params.get("hide_old") is not None:
                hide_old = parse_toggle(query_params.get("hide_old"))
            else:
                hide_old = not show_closed
        show_closed = not bool(hide_old)
        hide_done = hide_done or parse_toggle(query_params.get("hide_done"))
        today = date.today()
        focus_month = parse_calendar_month(calendar_month or query_params.get("calendar_month")) or today.month
        focus_year = parse_calendar_year(calendar_year or query_params.get("calendar_year")) or today.year
        prev_year = focus_year - 1 if focus_month == 1 else focus_year
        prev_month = 12 if focus_month == 1 else focus_month - 1
        next_year = focus_year + 1 if focus_month == 12 else focus_year
        next_month = 1 if focus_month == 12 else focus_month + 1
        year_prev_month = focus_month
        year_prev_year = focus_year - 1
        year_next_month = focus_month
        year_next_year = focus_year + 1
        config = ui_config()
        normalize_task_project_refs(workspace)
        all_projects = load_all_projects(workspace)
        project_name_by_id = {p.id: p.name for p in all_projects if p.id}
        project_by_name = {p.name: p for p in all_projects}
        resolved_projects: list[str] = []
        for p in projects:
            if p in project_name_by_id:
                resolved_projects.append(p)
                continue
            legacy = project_by_name.get(p)
            if legacy and legacy.id:
                resolved_projects.append(legacy.id)
                continue
            resolved_projects.append(p)
        projects = resolved_projects

        all_tasks = load_all_tasks(workspace)
        for task in all_tasks:
            if task.project_id and task.project_id in project_name_by_id:
                task.project = project_name_by_id[task.project_id]
        if selected_task is not None and selected_task.project_id and selected_task.project_id in project_name_by_id:
            selected_task.project = project_name_by_id[selected_task.project_id]
        milestones = load_all_milestones(workspace)
        tasks_by_id = {t.id: t for t in all_tasks}
        panel_task_id = (selected_task.id if selected_task else (request.query_params.get("panel_task") or "").strip())
        if selected_task is None and panel_task_id:
            selected_task = tasks_by_id.get(panel_task_id)
            if selected_task is None:
                panel_task_id = ""
        milestone_rollups = {m.id: milestone_rollup(m, tasks_by_id) for m in milestones}

        selected_milestone = None
        rollup = None
        candidate_tasks = all_tasks
        milestone = (milestone or "").strip()
        if milestone:
            selected_milestone = next((m for m in milestones if m.id == milestone), None)
            if selected_milestone is not None:
                rollup = milestone_rollup(selected_milestone, tasks_by_id)
                allowed = set(selected_milestone.task_ids)
                candidate_tasks = [t for t in all_tasks if t.id in allowed]
            else:
                milestone = ""

        filtered = sort_tasks(filter_tasks(candidate_tasks, projects, date_from, date_to, q), sort, sort_dir)
        if hide_done:
            filtered = [t for t in filtered if t.status != "done"]
        hidden_closed_count = 0
        if hide_old:
            filtered, hidden_closed_count = hide_stale_closed_tasks(filtered, stale_days)
        colors = project_colors(all_projects, all_tasks)
        project_rollups: dict[str, dict[str, int]] = {}
        for project in all_projects:
            rows = [
                t
                for t in all_tasks
                if (project.id and t.project_id == project.id) or ((not t.project_id) and t.project == project.name)
            ]
            total = len(rows)
            done = sum(1 for t in rows if t.status == "done")
            working = sum(1 for t in rows if t.status == "working")
            progress = round(sum(t.percent_complete for t in rows) / total) if total else 0
            project_rollups[project.id] = {
                "total": total,
                "done": done,
                "working": working,
                "progress": progress,
            }

        pills = []
        if selected_milestone is not None:
            pills.append(
                {
                    "label": f"Milestone: {selected_milestone.title}",
                    "color": "",
                    "remove": build_query(projects, date_from, date_to, q, view, sort, show_closed=show_closed, stale_days=stale_days, hide_done=hide_done, hide_old=hide_old, sort_dir=sort_dir),
                }
            )
        for p in projects:
            project_label = project_name_by_id.get(p, p)
            others = [x for x in projects if x != p]
            pills.append(
                {
                    "label": f"Project: {project_label}",
                    "color": colors.get(p, ""),
                    "remove": build_query(others, date_from, date_to, q, view, sort, milestone, show_closed, stale_days, hide_done=hide_done, hide_old=hide_old, sort_dir=sort_dir),
                }
            )
        if date_from:
            pills.append(
                {
                    "label": f"From {date_from}",
                    "color": "",
                    "remove": build_query(projects, "", date_to, q, view, sort, milestone, show_closed, stale_days, hide_done=hide_done, hide_old=hide_old, sort_dir=sort_dir),
                }
            )
        if date_to:
            pills.append(
                {
                    "label": f"To {date_to}",
                    "color": "",
                    "remove": build_query(projects, date_from, "", q, view, sort, milestone, show_closed, stale_days, hide_done=hide_done, hide_old=hide_old, sort_dir=sort_dir),
                }
            )
        if q:
            pills.append(
                {
                    "label": f'Search: "{q}"',
                    "color": "",
                    "remove": build_query(projects, date_from, date_to, "", view, sort, milestone, show_closed, stale_days, hide_done=hide_done, hide_old=hide_old, sort_dir=sort_dir),
                }
            )

        if hide_old:
            pills.append(
                {
                    "label": f"Hide old stuff ({stale_days}d+)",
                    "color": "",
                    "remove": build_query(projects, date_from, date_to, q, view, sort, milestone, show_closed=True, stale_days=stale_days, calendar_month=focus_month, calendar_year=focus_year, hide_done=hide_done, hide_old=False, sort_dir=sort_dir),
                }
            )

        if hide_done:
            pills.append(
                {
                    "label": "Hide done",
                    "color": "",
                    "remove": build_query(projects, date_from, date_to, q, view, sort, milestone, show_closed, stale_days, focus_month, focus_year, hide_done=False, hide_old=hide_old, sort_dir=sort_dir),
                }
            )

        return {
            "request": request,
            "app_name": config["app_name"],
            "workspace_name": config["workspace_name"],
            "model": dashboard_model(filtered),
            "statuses": STATUSES,
            "selected_task": selected_task,
            "milestones": milestones,
            "selected_milestone": selected_milestone,
            "rollup": rollup,
            "milestone_rollups": milestone_rollups,
            "workspace": workspace,
            "projects": all_projects,
            "project_rollups": project_rollups,
            "project_colors": colors,
            "project_name_by_id": project_name_by_id,
            "sorts": SORTS,
            "calendar": build_calendar(filtered, date_from, date_to, focus_month, focus_year),
            "git": git_status(workspace),
            "git_lfs": git_lfs_status(workspace),
            "git_message": git_message,
            "task_activity_entries": build_task_activity_entries(selected_task),
            "task_index": [
                {
                    "id": t.id,
                    "title": t.title,
                    "status": t.status,
                    "project": t.project,
                    "due_date": t.due_date or "",
                }
                for t in sort_tasks(all_tasks, "title", "asc")
            ],
            "task_titles": {t.id: t.title for t in all_tasks},
            **ai_config(),
            "filters": {
                "projects": projects,
                "date_from": date_from,
                "date_to": date_to,
                "q": q,
                "sort": sort,
                "sort_dir": sort_dir,
                "view": view,
                "milestone": milestone,
                "stale_days": stale_days,
                "calendar_month": focus_month,
                "calendar_year": focus_year,
                "query": build_query(projects, date_from, date_to, q, "", sort, milestone, show_closed, stale_days, focus_month, focus_year, hide_done, hide_old=hide_old, sort_dir=sort_dir),
                "query_no_sort": build_query(projects, date_from, date_to, q, "", "", milestone, show_closed, stale_days, focus_month, focus_year, hide_done, hide_old=hide_old),
                "calendar_query": build_query(projects, date_from, date_to, q, "calendar", sort, milestone, show_closed, stale_days, focus_month, focus_year, hide_done, hide_old=hide_old, sort_dir=sort_dir),
                "calendar_prev_query": build_query(projects, date_from, date_to, q, "calendar", sort, milestone, show_closed, stale_days, prev_month, prev_year, hide_done, hide_old=hide_old, sort_dir=sort_dir),
                "calendar_next_query": build_query(projects, date_from, date_to, q, "calendar", sort, milestone, show_closed, stale_days, next_month, next_year, hide_done, hide_old=hide_old, sort_dir=sort_dir),
                "calendar_year_prev_query": build_query(projects, date_from, date_to, q, "calendar", sort, milestone, show_closed, stale_days, year_prev_month, year_prev_year, hide_done, hide_old=hide_old, sort_dir=sort_dir),
                "calendar_year_next_query": build_query(projects, date_from, date_to, q, "calendar", sort, milestone, show_closed, stale_days, year_next_month, year_next_year, hide_done, hide_old=hide_old, sort_dir=sort_dir),
                "panel_task": panel_task_id,
                "show_closed": show_closed,
                "hide_old": hide_old,
                "hide_done": hide_done,
                "hidden_closed_count": hidden_closed_count,
                "toggle_closed_query": build_query(projects, date_from, date_to, q, view, sort, milestone, not show_closed, stale_days, focus_month, focus_year, hide_done, hide_old=not hide_old, sort_dir=sort_dir),
                "sort_default_dirs": default_sort_dirs,
                "pills": pills,
            },
        }

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        project: list[str] = Query(default=[]),
        date_from: str = "",
        date_to: str = "",
        q: str = "",
        sort: str = "priority",
        sort_dir: str = "",
        view: str = "list",
        milestone: str = "",
        show_closed: str = "",
        hide_old: str = "",
        hide_done: str = "",
        stale_days: str = "",
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "index.html",
            context(
                request,
                projects=project,
                date_from=date_from,
                date_to=date_to,
                q=q,
                sort=sort,
                sort_dir=sort_dir,
                view=view,
                milestone=milestone,
                show_closed=parse_toggle(show_closed),
                hide_old=parse_toggle(hide_old),
                hide_done=parse_toggle(hide_done),
                stale_days=parse_stale_days(stale_days),
            ),
        )

    @app.get("/partials/main", response_class=HTMLResponse)
    def main_partial(
        request: Request,
        project: list[str] = Query(default=[]),
        date_from: str = "",
        date_to: str = "",
        q: str = "",
        sort: str = "priority",
        sort_dir: str = "",
        view: str = "list",
        milestone: str = "",
        show_closed: str = "",
        hide_old: str = "",
        hide_done: str = "",
        stale_days: str = "",
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                projects=project,
                date_from=date_from,
                date_to=date_to,
                q=q,
                sort=sort,
                sort_dir=sort_dir,
                view=view,
                milestone=milestone,
                show_closed=parse_toggle(show_closed),
                hide_old=parse_toggle(hide_old),
                hide_done=parse_toggle(hide_done),
                stale_days=parse_stale_days(stale_days),
            ),
        )

    @app.get("/tasks/{task_id}/panel", response_class=HTMLResponse)
    def task_panel(
        request: Request,
        task_id: str,
        project: list[str] = Query(default=[]),
        date_from: str = "",
        date_to: str = "",
        q: str = "",
        view: str = "list",
        milestone: str = "",
        show_closed: str = "",
        hide_old: str = "",
        hide_done: str = "",
        stale_days: str = "",
    ) -> HTMLResponse:
        task = load_task(workspace, task_id)
        return templates.TemplateResponse(
            request,
            "partials/task_panel.html",
            context(
                request,
                task,
                projects=project,
                date_from=date_from,
                date_to=date_to,
                q=q,
                view=view,
                milestone=milestone,
                show_closed=parse_toggle(show_closed),
                hide_old=parse_toggle(hide_old),
                hide_done=parse_toggle(hide_done),
                stale_days=parse_stale_days(stale_days),
            ),
        )

    @app.post("/tasks/create", response_class=HTMLResponse)
    def create_task_route(
        request: Request,
        title: str = Form("New task"),
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_hide_done: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        task = create_task(workspace, title)
        if f_milestone:
            add_task_to_milestone(workspace, f_milestone, task.id)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                task,
                projects=f_project,
                date_from=f_from,
                date_to=f_to,
                q=f_q,
                view=f_view,
                milestone=f_milestone,
                show_closed=parse_toggle(f_show_closed),
                hide_done=parse_toggle(f_hide_done),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/tasks/{task_id}/save", response_class=HTMLResponse)
    async def save_task_route(
        request: Request,
        task_id: str,
        title: str = Form(...),
        status: str = Form(""),
        priority: str = Form(""),
        project: str = Form(""),
        summary: str = Form(""),
        description: str = Form(""),
        tags: str = Form(""),
        start_date: str = Form(""),
        due_date: str = Form(""),
        completed_date: str = Form(""),
        percent_complete: str = Form(""),
        depends_on: str = Form(""),
        checklist_text: str = Form(""),
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_hide_done: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        task = load_task(workspace, task_id)
        task.title = title
        status_value = (status or "").strip().lower()
        if status_value in set(STATUSES):
            task.status = status_value
        priority_value = (priority or "").strip().lower()
        if priority_value in {"low", "normal", "high", "critical"}:
            task.priority = priority_value
        project_value = (project or "").strip()
        projects_all = load_all_projects(workspace)
        by_id = {p.id: p for p in projects_all if p.id}
        by_name = {p.name: p for p in projects_all}
        selected_project = by_id.get(project_value) or by_name.get(project_value)
        if selected_project is None and project_value:
            selected_project = register_project(workspace, project_value)
        if selected_project is not None:
            task.project_id = selected_project.id
            task.project = selected_project.name
        else:
            task.project_id = ""
            task.project = ""
        task.summary = summary
        task.description = description
        task.tags = [x.strip() for x in tags.split(",") if x.strip()]
        task.start_date = start_date or None
        task.due_date = due_date or None
        task.completed_date = completed_date or None
        percent_raw = str(percent_complete or "").strip()
        if percent_raw:
            try:
                new_progress = max(0, min(int(percent_raw), 100))
            except ValueError:
                new_progress = task.percent_complete
            old_progress = task.percent_complete
            task.percent_complete = new_progress
            log_progress_change(workspace, task, old_progress, task.percent_complete)
        task.depends_on = [x.strip() for x in depends_on.split(",") if x.strip()]
        checklist = []
        for line in checklist_text.splitlines():
            line = line.strip()
            if not line:
                continue
            done = line.startswith("[x]") or line.startswith("[X]")
            text = line[3:].strip() if line[:3].lower() in {"[x]", "[ ]"} else line
            checklist.append(ChecklistItem(text=text, done=done))
        task.checklist = checklist
        save_task(workspace, task)
        if task.project:
            register_project(workspace, task.project)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                task,
                projects=f_project,
                date_from=f_from,
                date_to=f_to,
                q=f_q,
                view=f_view,
                milestone=f_milestone,
                show_closed=parse_toggle(f_show_closed),
                hide_done=parse_toggle(f_hide_done),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/tasks/{task_id}/update", response_class=HTMLResponse)
    async def update_task_activity_route(
        request: Request,
        task_id: str,
        progress_after: str = Form(""),
        status: str = Form(""),
        priority: str = Form(""),
        body: str = Form(""),
        attachment: UploadFile | None = File(None),
        description: str = Form(""),
        save_title: str | None = Form(None),
        save_project: str | None = Form(None),
        save_summary: str | None = Form(None),
        save_task_description: str | None = Form(None),
        save_tags: str | None = Form(None),
        save_start_date: str | None = Form(None),
        save_due_date: str | None = Form(None),
        save_completed_date: str | None = Form(None),
        save_depends_on: str | None = Form(None),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_hide_done: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        task = load_task(workspace, task_id)

        if save_title is not None:
            task.title = save_title
        if save_project is not None:
            project_value = save_project.strip()
            projects_all = load_all_projects(workspace)
            by_id = {p.id: p for p in projects_all if p.id}
            by_name = {p.name: p for p in projects_all}
            selected_project = by_id.get(project_value) or by_name.get(project_value)
            if selected_project is None and project_value:
                selected_project = register_project(workspace, project_value)
            if selected_project is not None:
                task.project_id = selected_project.id
                task.project = selected_project.name
            else:
                task.project_id = ""
                task.project = ""
        if save_summary is not None:
            task.summary = save_summary
        if save_task_description is not None:
            task.description = save_task_description
        if save_tags is not None:
            task.tags = [x.strip() for x in save_tags.split(",") if x.strip()]
        if save_start_date is not None:
            task.start_date = save_start_date or None
        if save_due_date is not None:
            task.due_date = save_due_date or None
        if save_completed_date is not None:
            task.completed_date = save_completed_date or None
        if save_depends_on is not None:
            task.depends_on = [x.strip() for x in save_depends_on.split(",") if x.strip()]

        progress_raw = str(progress_after or "").strip()
        if progress_raw:
            try:
                new_progress = max(0, min(int(progress_raw), 100))
            except ValueError:
                new_progress = task.percent_complete
            old_progress = task.percent_complete
            task.percent_complete = new_progress
            log_progress_change(workspace, task, old_progress, task.percent_complete)

        status_before = task.status
        priority_before = task.priority
        status_value = (status or "").strip().lower()
        if status_value in set(STATUSES):
            task.status = status_value
        priority_value = (priority or "").strip().lower()
        if priority_value in {"low", "normal", "high", "critical"}:
            task.priority = priority_value

        if task.status == "done" and not task.completed_date:
            task.completed_date = date.today().isoformat()
        elif status_before == "done" and task.status != "done":
            task.completed_date = None

        context_parts: list[str] = []
        if status_before != task.status:
            context_parts.append(f"Status {status_before} → {task.status}")
        if priority_before != task.priority:
            context_parts.append(f"Priority {priority_before} → {task.priority}")
        if context_parts:
            task.activity.append(
                TaskActivityEvent(
                    event_type="note",
                    note_text=" · ".join(context_parts),
                )
            )

        note_text = (body or "").strip()
        if note_text:
            task.activity.append(TaskActivityEvent(event_type="note", note_text=note_text))

        save_task(workspace, task)
        register_project(workspace, task.project)

        if attachment and (attachment.filename or "").strip():
            task = add_task_activity_image(
                workspace,
                task_id,
                attachment.filename or "attachment.bin",
                await attachment.read(),
                attachment.content_type,
                description,
            )

        return templates.TemplateResponse(
            request,
            "partials/task_panel.html",
            context(
                request,
                task,
                view=f_view,
                milestone=f_milestone,
                show_closed=parse_toggle(f_show_closed),
                hide_done=parse_toggle(f_hide_done),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/tasks/{task_id}/checklist/add", response_class=HTMLResponse)
    async def checklist_add_route(
        request: Request,
        task_id: str,
        item_text: str = Form(""),
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        task = load_task(workspace, task_id)
        text = item_text.strip()
        if text:
            task.checklist.append(ChecklistItem(text=text, done=False))
            save_task(workspace, task)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                task,
                projects=f_project,
                date_from=f_from,
                date_to=f_to,
                q=f_q,
                view=f_view,
                milestone=f_milestone,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/tasks/{task_id}/checklist/{item_index}/toggle", response_class=HTMLResponse)
    async def checklist_toggle_route(
        request: Request,
        task_id: str,
        item_index: int,
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        task = load_task(workspace, task_id)
        if 0 <= item_index < len(task.checklist):
            task.checklist[item_index].done = not task.checklist[item_index].done
            save_task(workspace, task)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                task,
                projects=f_project,
                date_from=f_from,
                date_to=f_to,
                q=f_q,
                view=f_view,
                milestone=f_milestone,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/tasks/{task_id}/checklist/{item_index}/delete", response_class=HTMLResponse)
    async def checklist_delete_route(
        request: Request,
        task_id: str,
        item_index: int,
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        task = load_task(workspace, task_id)
        if 0 <= item_index < len(task.checklist):
            task.checklist.pop(item_index)
            save_task(workspace, task)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                task,
                projects=f_project,
                date_from=f_from,
                date_to=f_to,
                q=f_q,
                view=f_view,
                milestone=f_milestone,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/tasks/{task_id}/raw", response_class=HTMLResponse)
    async def save_raw_json(
        request: Request,
        task_id: str,
        raw_json: str = Form(...),
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        parsed = json.loads(raw_json)
        task = Task.model_validate(parsed)
        save_task(workspace, task)
        register_project(workspace, task.project)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                task,
                projects=f_project,
                date_from=f_from,
                date_to=f_to,
                q=f_q,
                view=f_view,
                milestone=f_milestone,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/tasks/{task_id}/note", response_class=HTMLResponse)
    async def add_note_route(request: Request, task_id: str, body: str = Form("")) -> HTMLResponse:
        task = add_task_activity_note(workspace, task_id, body)
        return templates.TemplateResponse(request, "partials/task_panel.html", context(request, task))

    @app.post("/tasks/{task_id}/attachment", response_class=HTMLResponse)
    async def upload_attachment_route(
        request: Request,
        task_id: str,
        attachment: UploadFile = File(...),
        description: str = Form(""),
    ) -> HTMLResponse:
        task = add_task_activity_image(
            workspace,
            task_id,
            attachment.filename or "attachment.bin",
            await attachment.read(),
            attachment.content_type,
            description,
        )
        return templates.TemplateResponse(request, "partials/task_panel.html", context(request, task))

    @app.get("/tasks/{task_id}/burndown.json")
    def task_burndown_json(task_id: str) -> Response:
        task = load_task(workspace, task_id)
        fallback_iso = (
            task.extra.get("created_at")
            or task.start_date
            or task.due_date
            or task.completed_date
            or datetime.now().isoformat(timespec="seconds")
        )

        progress_updates = [event for event in task.activity if event.event_type == "progress_update"]
        progress_updates.sort(key=lambda item: item.created_at)
        first_before = progress_updates[0].progress_before if progress_updates else None
        current_progress = _clip_progress(first_before, task.percent_complete)

        raw_events: list[dict[str, object]] = []
        for note in task.notes:
            summary = _summarize_text(note.body)
            label = "Note added"
            if summary:
                label = f"Note: {summary}"
            raw_events.append(
                {
                    "created_at": note.created_at,
                    "event_type": "note",
                    "label": label,
                    "preview_title": "Note",
                    "preview_body": _preview_text(note.body),
                    "preview_path": "",
                    "is_image": False,
                    "progress_before": None,
                    "progress_after": None,
                }
            )
        for attachment in task.attachments:
            filename = (attachment.filename or "Attachment").strip() or "Attachment"
            raw_events.append(
                {
                    "created_at": attachment.uploaded_at,
                    "event_type": "attachment",
                    "label": f"Attachment: {filename}",
                    "preview_title": filename,
                    "preview_body": _preview_text(attachment.description),
                    "preview_path": attachment.path,
                    "is_image": attachment.kind == "image",
                    "progress_before": None,
                    "progress_after": None,
                }
            )
        for event in task.activity:
            if event.event_type == "progress_update":
                before = _clip_progress(event.progress_before, current_progress)
                after = _clip_progress(event.progress_after, before)
                raw_events.append(
                    {
                        "created_at": event.created_at,
                        "event_type": "progress_update",
                        "label": f"Progress {before}% → {after}%",
                        "preview_title": "Progress update",
                        "preview_body": "",
                        "preview_path": "",
                        "is_image": False,
                        "progress_before": before,
                        "progress_after": after,
                    }
                )
            elif event.event_type == "image":
                filename = (event.image_filename or "Attachment").strip() or "Attachment"
                image_name = event.image_filename or event.image_path or ""
                is_image = Path(image_name).suffix.lower() in {
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".gif",
                    ".webp",
                    ".bmp",
                    ".svg",
                }
                raw_events.append(
                    {
                        "created_at": event.created_at,
                        "event_type": "attachment",
                        "label": f"Attachment: {filename}",
                        "preview_title": filename,
                        "preview_body": _preview_text(event.note_text),
                        "preview_path": event.image_path or "",
                        "is_image": is_image,
                        "progress_before": None,
                        "progress_after": None,
                    }
                )
            else:
                summary = _summarize_text(event.note_text)
                label = "Note added"
                if summary:
                    label = f"Note: {summary}"
                raw_events.append(
                    {
                        "created_at": event.created_at,
                        "event_type": "note",
                        "label": label,
                        "preview_title": "Note",
                        "preview_body": _preview_text(event.note_text),
                        "preview_path": "",
                        "is_image": False,
                        "progress_before": None,
                        "progress_after": None,
                    }
                )

        raw_events.sort(
            key=lambda item: (
                _parse_event_datetime(str(item.get("created_at") or "")) or datetime.max,
                str(item.get("event_type") or ""),
            )
        )

        points: list[dict[str, object]] = []
        for event in raw_events:
            if event.get("event_type") == "progress_update":
                current_progress = _clip_progress(
                    event.get("progress_after") if isinstance(event.get("progress_after"), int) else None,
                    current_progress,
                )
            points.append(
                {
                    "created_at": str(event.get("created_at") or fallback_iso),
                    "y": 100 - current_progress,
                    "label": str(event.get("label") or "Update"),
                    "event_type": str(event.get("event_type") or "update"),
                    "preview_title": str(event.get("preview_title") or ""),
                    "preview_body": str(event.get("preview_body") or ""),
                    "preview_path": str(event.get("preview_path") or ""),
                    "is_image": bool(event.get("is_image")),
                }
            )

        if not points:
            points.append(
                {
                    "created_at": fallback_iso,
                    "y": 100 - _clip_progress(task.percent_complete),
                    "label": f"Current progress: {_clip_progress(task.percent_complete)}%",
                    "event_type": "snapshot",
                    "preview_title": "Current snapshot",
                    "preview_body": "",
                    "preview_path": "",
                    "is_image": False,
                }
            )

        normalized_points = _normalize_event_points(points, fallback_iso)
        return Response(
            json.dumps({"task_id": task_id, "title": task.title, "points": normalized_points}),
            media_type="application/json",
        )

    @app.post("/tasks/{task_id}/complete", response_class=HTMLResponse)
    async def complete_task_route(
        request: Request,
        task_id: str,
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        task = load_task(workspace, task_id)
        if task.status == "done":
            task.status = "working"
            task.completed_date = None
        else:
            task.status = "done"
            old_progress = task.percent_complete
            task.percent_complete = 100
            log_progress_change(workspace, task, old_progress, task.percent_complete)
            if not task.completed_date:
                task.completed_date = date.today().isoformat()
        save_task(workspace, task)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                task,
                projects=f_project,
                date_from=f_from,
                date_to=f_to,
                q=f_q,
                view=f_view,
                milestone=f_milestone,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/tasks/{task_id}/delete")
    async def delete_task_route(
        task_id: str,
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> RedirectResponse:
        delete_task(workspace, task_id)
        params: list[tuple[str, str]] = [("project", p) for p in f_project if p]
        if f_from:
            params.append(("date_from", f_from))
        if f_to:
            params.append(("date_to", f_to))
        if f_q:
            params.append(("q", f_q))
        if f_milestone:
            params.append(("milestone", f_milestone))
        if parse_toggle(f_show_closed):
            params.append(("show_closed", "1"))
        if parse_stale_days(f_stale_days) != STALE_CLOSED_DAYS:
            params.append(("stale_days", str(parse_stale_days(f_stale_days))))
        params.append(("view", f_view))
        return RedirectResponse("/?" + urllib.parse.urlencode(params), status_code=303)

    @app.post("/projects", response_class=HTMLResponse)
    def add_project_route(
        request: Request,
        name: str = Form(...),
        description: str = Form(""),
        color: str = Form("#2e6fd8"),
        project_id: str = Form(""),
    ) -> HTMLResponse:
        upsert_project(workspace, name, description.strip(), color, project_id=project_id)
        return templates.TemplateResponse(request, "partials/main.html", context(request, view="projects"))

    # --- Milestones ---------------------------------------------------------

    @app.post("/milestones/create", response_class=HTMLResponse)
    def create_milestone_route(
        request: Request,
        title: str = Form("New milestone"),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        milestone = create_milestone(workspace, title)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                view="list",
                milestone=milestone.id,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.get("/milestones/{milestone_id}/panel", response_class=HTMLResponse)
    def milestone_panel_route(
        request: Request,
        milestone_id: str,
        project: list[str] = Query(default=[]),
        date_from: str = "",
        date_to: str = "",
        q: str = "",
        view: str = "list",
        show_closed: str = "",
        stale_days: str = "",
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "partials/milestone_panel.html",
            context(
                request,
                projects=project,
                date_from=date_from,
                date_to=date_to,
                q=q,
                view=view,
                milestone=milestone_id,
                show_closed=parse_toggle(show_closed),
                stale_days=parse_stale_days(stale_days),
            ),
        )

    @app.get("/milestones/{milestone_id}/burndown.json")
    def milestone_burndown_json(milestone_id: str) -> Response:
        milestone = load_milestone(workspace, milestone_id)
        all_tasks = load_all_tasks(workspace)
        tasks_by_id = {task.id: task for task in all_tasks}
        milestone_tasks = [tasks_by_id[task_id] for task_id in milestone.task_ids if task_id in tasks_by_id]

        fallback_iso = (
            milestone.start_date
            or milestone.target_date
            or milestone.extra.get("created_at")
            or datetime.now().isoformat(timespec="seconds")
        )

        task_progress: dict[str, int] = {}
        for task in milestone_tasks:
            updates = [event for event in task.activity if event.event_type == "progress_update"]
            updates.sort(key=lambda item: item.created_at)
            baseline = updates[0].progress_before if updates else task.percent_complete
            task_progress[task.id] = _clip_progress(baseline, task.percent_complete)

        raw_events: list[dict[str, object]] = []

        for note in milestone.notes:
            summary = _summarize_text(note.body)
            label = "Milestone note"
            if summary:
                label = f"Milestone note: {summary}"
            raw_events.append(
                {
                    "created_at": note.created_at,
                    "event_type": "note",
                    "task_id": None,
                    "label": label,
                    "preview_title": "Milestone note",
                    "preview_body": _preview_text(note.body),
                    "preview_path": "",
                    "is_image": False,
                    "progress_after": None,
                }
            )
        for attachment in milestone.attachments:
            filename = (attachment.filename or "Attachment").strip() or "Attachment"
            raw_events.append(
                {
                    "created_at": attachment.uploaded_at,
                    "event_type": "attachment",
                    "task_id": None,
                    "label": f"Milestone attachment: {filename}",
                    "preview_title": filename,
                    "preview_body": _preview_text(attachment.description),
                    "preview_path": attachment.path,
                    "is_image": attachment.kind == "image",
                    "progress_after": None,
                }
            )

        for task in milestone_tasks:
            prefix = task.title.strip() or task.id
            for note in task.notes:
                summary = _summarize_text(note.body)
                label = f"{prefix}: note"
                if summary:
                    label = f"{prefix}: {summary}"
                raw_events.append(
                    {
                        "created_at": note.created_at,
                        "event_type": "note",
                        "task_id": task.id,
                        "label": label,
                        "preview_title": f"{prefix} note",
                        "preview_body": _preview_text(note.body),
                        "preview_path": "",
                        "is_image": False,
                        "progress_after": None,
                    }
                )
            for attachment in task.attachments:
                filename = (attachment.filename or "Attachment").strip() or "Attachment"
                raw_events.append(
                    {
                        "created_at": attachment.uploaded_at,
                        "event_type": "attachment",
                        "task_id": task.id,
                        "label": f"{prefix}: attachment {filename}",
                        "preview_title": filename,
                        "preview_body": _preview_text(attachment.description),
                        "preview_path": attachment.path,
                        "is_image": attachment.kind == "image",
                        "progress_after": None,
                    }
                )
            for event in task.activity:
                if event.event_type == "progress_update":
                    before = _clip_progress(event.progress_before, task_progress.get(task.id, task.percent_complete))
                    after = _clip_progress(event.progress_after, before)
                    raw_events.append(
                        {
                            "created_at": event.created_at,
                            "event_type": "progress_update",
                            "task_id": task.id,
                            "label": f"{prefix}: {before}% → {after}%",
                            "preview_title": f"{prefix} progress",
                            "preview_body": "",
                            "preview_path": "",
                            "is_image": False,
                            "progress_after": after,
                        }
                    )
                elif event.event_type == "image":
                    filename = (event.image_filename or "Attachment").strip() or "Attachment"
                    image_name = event.image_filename or event.image_path or ""
                    is_image = Path(image_name).suffix.lower() in {
                        ".png",
                        ".jpg",
                        ".jpeg",
                        ".gif",
                        ".webp",
                        ".bmp",
                        ".svg",
                    }
                    raw_events.append(
                        {
                            "created_at": event.created_at,
                            "event_type": "attachment",
                            "task_id": task.id,
                            "label": f"{prefix}: attachment {filename}",
                            "preview_title": filename,
                            "preview_body": _preview_text(event.note_text),
                            "preview_path": event.image_path or "",
                            "is_image": is_image,
                            "progress_after": None,
                        }
                    )
                else:
                    summary = _summarize_text(event.note_text)
                    label = f"{prefix}: note"
                    if summary:
                        label = f"{prefix}: {summary}"
                    raw_events.append(
                        {
                            "created_at": event.created_at,
                            "event_type": "note",
                            "task_id": task.id,
                            "label": label,
                            "preview_title": f"{prefix} note",
                            "preview_body": _preview_text(event.note_text),
                            "preview_path": "",
                            "is_image": False,
                            "progress_after": None,
                        }
                    )

        raw_events.sort(
            key=lambda item: (
                _parse_event_datetime(str(item.get("created_at") or "")) or datetime.max,
                str(item.get("event_type") or ""),
                str(item.get("task_id") or ""),
            )
        )

        def avg_remaining() -> int:
            if not task_progress:
                return 100
            return round(sum(100 - progress for progress in task_progress.values()) / len(task_progress))

        points: list[dict[str, object]] = []
        for event in raw_events:
            if event.get("event_type") == "progress_update":
                task_id = str(event.get("task_id") or "")
                if task_id in task_progress:
                    task_progress[task_id] = _clip_progress(
                        event.get("progress_after") if isinstance(event.get("progress_after"), int) else None,
                        task_progress[task_id],
                    )
            points.append(
                {
                    "created_at": str(event.get("created_at") or fallback_iso),
                    "y": avg_remaining(),
                    "label": str(event.get("label") or "Update"),
                    "event_type": str(event.get("event_type") or "update"),
                    "preview_title": str(event.get("preview_title") or ""),
                    "preview_body": str(event.get("preview_body") or ""),
                    "preview_path": str(event.get("preview_path") or ""),
                    "is_image": bool(event.get("is_image")),
                }
            )

        if not points and milestone_tasks:
            remaining = avg_remaining()
            points.append(
                {
                    "created_at": fallback_iso,
                    "y": remaining,
                    "label": f"Current average remaining: {remaining}%",
                    "event_type": "snapshot",
                    "preview_title": "Current snapshot",
                    "preview_body": "",
                    "preview_path": "",
                    "is_image": False,
                }
            )

        normalized_points = _normalize_event_points(points, fallback_iso)

        return Response(
            json.dumps({"milestone_id": milestone_id, "title": milestone.title, "points": normalized_points}),
            media_type="application/json",
        )

    @app.post("/milestones/{milestone_id}/save", response_class=HTMLResponse)
    def save_milestone_route(
        request: Request,
        milestone_id: str,
        title: str = Form(...),
        status: str = Form("active"),
        color: str = Form("#3567e0"),
        summary: str = Form(""),
        description: str = Form(""),
        start_date: str = Form(""),
        target_date: str = Form(""),
        f_view: str = Form("list"),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        milestone = load_milestone(workspace, milestone_id)
        milestone.title = title
        milestone.status = status if status in {"planned", "active", "done"} else "active"
        milestone.color = (color or "").strip() or "#3567e0"
        milestone.summary = summary
        milestone.description = description
        milestone.start_date = start_date or None
        milestone.target_date = target_date or None
        save_milestone(workspace, milestone)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                view=f_view,
                milestone=milestone.id,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/milestones/{milestone_id}/note", response_class=HTMLResponse)
    def milestone_note_route(
        request: Request,
        milestone_id: str,
        body: str = Form(""),
        save_title: str | None = Form(None),
        save_status: str | None = Form(None),
        save_color: str | None = Form(None),
        save_summary: str | None = Form(None),
        save_description: str | None = Form(None),
        save_projects: list[str] = Form(default=[]),
        save_projects_present: str = Form(""),
        save_start_date: str | None = Form(None),
        save_target_date: str | None = Form(None),
        f_view: str = Form("list"),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        milestone = load_milestone(workspace, milestone_id)
        if save_title is not None:
            milestone.title = save_title
        if save_status is not None:
            milestone.status = save_status if save_status in {"planned", "active", "done"} else milestone.status
        if save_color is not None:
            milestone.color = (save_color or "").strip() or "#3567e0"
        if save_summary is not None:
            milestone.summary = save_summary
        if save_description is not None:
            milestone.description = save_description
        if save_start_date is not None:
            milestone.start_date = save_start_date or None
        if save_target_date is not None:
            milestone.target_date = save_target_date or None
        if parse_toggle(save_projects_present):
            milestone.projects = [p.strip() for p in save_projects if p.strip()]
        save_milestone(workspace, milestone)
        add_milestone_note(workspace, milestone_id, body)
        return templates.TemplateResponse(
            request,
            "partials/milestone_panel.html",
            context(
                request,
                view=f_view,
                milestone=milestone_id,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/milestones/{milestone_id}/attachment", response_class=HTMLResponse)
    async def milestone_attachment_route(
        request: Request,
        milestone_id: str,
        attachment: UploadFile = File(...),
        description: str = Form(""),
        save_title: str | None = Form(None),
        save_status: str | None = Form(None),
        save_color: str | None = Form(None),
        save_summary: str | None = Form(None),
        save_description: str | None = Form(None),
        save_projects: list[str] = Form(default=[]),
        save_projects_present: str = Form(""),
        save_start_date: str | None = Form(None),
        save_target_date: str | None = Form(None),
        f_view: str = Form("list"),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        milestone = load_milestone(workspace, milestone_id)
        if save_title is not None:
            milestone.title = save_title
        if save_status is not None:
            milestone.status = save_status if save_status in {"planned", "active", "done"} else milestone.status
        if save_color is not None:
            milestone.color = (save_color or "").strip() or "#3567e0"
        if save_summary is not None:
            milestone.summary = save_summary
        if save_description is not None:
            milestone.description = save_description
        if save_start_date is not None:
            milestone.start_date = save_start_date or None
        if save_target_date is not None:
            milestone.target_date = save_target_date or None
        if parse_toggle(save_projects_present):
            milestone.projects = [p.strip() for p in save_projects if p.strip()]
        save_milestone(workspace, milestone)
        add_milestone_attachment(
            workspace,
            milestone_id,
            attachment.filename or "attachment.bin",
            await attachment.read(),
            attachment.content_type,
            description,
        )
        return templates.TemplateResponse(
            request,
            "partials/milestone_panel.html",
            context(
                request,
                view=f_view,
                milestone=milestone_id,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/milestones/{milestone_id}/tasks/new", response_class=HTMLResponse)
    def milestone_new_task_route(
        request: Request,
        milestone_id: str,
        title: str = Form("New task"),
        f_view: str = Form("list"),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        task = create_task(workspace, title)
        add_task_to_milestone(workspace, milestone_id, task.id)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                task,
                view=f_view,
                milestone=milestone_id,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/milestones/{milestone_id}/tasks/{task_id}/add", response_class=HTMLResponse)
    def milestone_add_task_route(
        request: Request,
        milestone_id: str,
        task_id: str,
        f_view: str = "list",
        f_show_closed: str = "",
        f_stale_days: str = "",
    ) -> HTMLResponse:
        add_task_to_milestone(workspace, milestone_id, task_id)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                view=f_view,
                milestone=milestone_id,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/milestones/{milestone_id}/tasks/{task_id}/remove", response_class=HTMLResponse)
    def milestone_remove_task_route(
        request: Request,
        milestone_id: str,
        task_id: str,
        f_view: str = "list",
        f_show_closed: str = "",
        f_stale_days: str = "",
    ) -> HTMLResponse:
        remove_task_from_milestone(workspace, milestone_id, task_id)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                view=f_view,
                milestone=milestone_id,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/milestones/{milestone_id}/delete")
    async def delete_milestone_route(milestone_id: str) -> RedirectResponse:
        delete_milestone(workspace, milestone_id)
        return RedirectResponse("/?view=milestones", status_code=303)

    @app.post("/git/sync", response_class=HTMLResponse)
    def git_sync_route(
        request: Request,
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("list"),
        f_sort: str = Form("priority"),
        f_sort_dir: str = Form(""),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        result = git_sync(workspace)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                projects=f_project,
                date_from=f_from,
                date_to=f_to,
                q=f_q,
                sort=f_sort,
                sort_dir=f_sort_dir,
                view=f_view,
                milestone=f_milestone,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
                git_message=result["message"],
            ),
        )

    @app.post("/git/lfs/init", response_class=HTMLResponse)
    def git_lfs_init_route(
        request: Request,
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("list"),
        f_sort: str = Form("priority"),
        f_sort_dir: str = Form(""),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
        f_calendar_month: str = Form(""),
        f_calendar_year: str = Form(""),
    ) -> HTMLResponse:
        result = git_lfs_init(workspace)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                projects=f_project,
                date_from=f_from,
                date_to=f_to,
                q=f_q,
                sort=f_sort,
                sort_dir=f_sort_dir,
                view=f_view,
                milestone=f_milestone,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
                calendar_month=parse_calendar_month(f_calendar_month),
                calendar_year=parse_calendar_year(f_calendar_year),
                git_message=result["message"],
            ),
        )

    @app.get("/export/csv")
    def export_csv(
        project: list[str] = Query(default=[]),
        date_from: str = "",
        date_to: str = "",
        q: str = "",
        show_closed: str = "",
        stale_days: str = "",
    ) -> Response:
        normalize_task_project_refs(workspace)
        projects_all = load_all_projects(workspace)
        project_name_by_id = {p.id: p.name for p in projects_all if p.id}
        tasks = filter_tasks(load_all_tasks(workspace), project, date_from, date_to, q)
        for task in tasks:
            if task.project_id and task.project_id in project_name_by_id:
                task.project = project_name_by_id[task.project_id]
        if not parse_toggle(show_closed):
            tasks, _ = hide_stale_closed_tasks(tasks, parse_stale_days(stale_days))
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "id",
                "title",
                "project",
                "status",
                "priority",
                "percent_complete",
                "start_date",
                "due_date",
                "completed_date",
                "tags",
                "summary",
            ]
        )
        for task in tasks:
            writer.writerow(
                [
                    task.id,
                    task.title,
                    task.project,
                    task.status,
                    task.priority,
                    task.percent_complete,
                    task.start_date or "",
                    task.due_date or "",
                    task.completed_date or "",
                    ", ".join(task.tags),
                    task.summary,
                ]
            )
        return Response(
            buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=taskunity-export.csv"},
        )

    @app.get("/export/json")
    def export_json(
        project: list[str] = Query(default=[]),
        date_from: str = "",
        date_to: str = "",
        q: str = "",
        show_closed: str = "",
        stale_days: str = "",
    ) -> Response:
        normalize_task_project_refs(workspace)
        tasks = filter_tasks(load_all_tasks(workspace), project, date_from, date_to, q)
        if not parse_toggle(show_closed):
            tasks, _ = hide_stale_closed_tasks(tasks, parse_stale_days(stale_days))
        projects = load_all_projects(workspace)
        project_name_by_id = {p.id: p.name for p in projects if p.id}
        for task in tasks:
            if task.project_id and task.project_id in project_name_by_id:
                task.project = project_name_by_id[task.project_id]
        ordered_project_names = [p.name for p in available_projects(projects, tasks)]
        config = ui_config()
        data = tasks_to_jsonantt(
            tasks,
            title=config["export_title"],
            project_colors=project_colors(projects, tasks),
            project_order=ordered_project_names,
        )
        return Response(
            json.dumps(data, indent=2, ensure_ascii=False),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=taskunity-export.json"},
        )

    @app.get("/healthz")
    def healthz() -> Response:
        return Response("ok", media_type="text/plain")

    # --- Settings -----------------------------------------------------------

    @app.post("/settings/save", response_class=HTMLResponse)
    def settings_save_route(
        request: Request,
        ai_enabled: str = Form("0"),
        ai_base_url: str = Form(""),
        ai_api_key: str = Form(""),
        ai_model: str = Form(""),
        ai_timeout_seconds: str = Form("30"),
        ai_max_tokens: str = Form("2048"),
        ai_temperature: str = Form("0.7"),
    ) -> HTMLResponse:
        save_workspace_config(workspace, {
            "ai_enabled": "1" if ai_enabled in {"1", "on", "true", "yes"} else "0",
            "ai_base_url": ai_base_url.strip(),
            "ai_api_key": ai_api_key.strip(),
            "ai_model": ai_model.strip(),
            "ai_timeout_seconds": ai_timeout_seconds.strip() or "30",
            "ai_max_tokens": ai_max_tokens.strip() or "2048",
            "ai_temperature": ai_temperature.strip() or "0.7",
        })
        cfg = ai_config()
        return HTMLResponse(
            '<div id="ai-settings-status" class="ai-save-ok">✓ AI settings saved.</div>'
            f'<input type="hidden" id="ai-enabled-state" value="{cfg["ai_enabled"]}">'
        )

    @app.get("/ai/models", response_class=HTMLResponse)
    def ai_models_route(request: Request) -> HTMLResponse:
        cfg = ai_config()
        if cfg["ai_enabled"] != "1":
            return HTMLResponse('<option value="">AI not enabled</option>')
        if not cfg["ai_base_url"]:
            return HTMLResponse('<option value="">No endpoint configured</option>')
        try:
            models = _ai_fetch_models(cfg)
        except Exception:
            return HTMLResponse('<option value="">Could not load models</option>')
        current = cfg["ai_model"]
        opts = "\n".join(
            f'<option value="{html_lib.escape(m)}"{"selected" if m == current else ""}>{html_lib.escape(m)}</option>'
            for m in models
        )
        return HTMLResponse(opts or '<option value="">No models found</option>')

    # --- Project panel ------------------------------------------------------

    @app.get("/projects/{project_id}/panel", response_class=HTMLResponse)
    def project_panel_route(
        request: Request,
        project_id: str,
        view: str = "projects",
        stale_days: str = "",
        show_closed: str = "",
    ) -> HTMLResponse:
        try:
            project = load_project(workspace, project_id)
        except Exception:
            return HTMLResponse('<div class="empty-panel"><h2>Project not found</h2></div>')
        all_tasks = load_all_tasks(workspace)
        project_name_by_id = {p.id: p.name for p in load_all_projects(workspace) if p.id}
        for task in all_tasks:
            if task.project_id and task.project_id in project_name_by_id:
                task.project = project_name_by_id[task.project_id]
        project_tasks = [
            t for t in all_tasks
            if (project.id and t.project_id == project.id) or ((not t.project_id) and t.project == project.name)
        ]
        return templates.TemplateResponse(
            request,
            "partials/project_panel.html",
            {
                "request": request,
                "selected_project": project,
                "project_tasks": project_tasks,
                "filters": {
                    "view": view,
                    "stale_days": parse_stale_days(stale_days),
                    "show_closed": parse_toggle(show_closed),
                },
            },
        )

    @app.post("/projects/{project_id}/save", response_class=HTMLResponse)
    def project_save_route(
        request: Request,
        project_id: str,
        name: str = Form(...),
        description: str = Form(""),
        color: str = Form("#2e6fd8"),
        f_view: str = Form("projects"),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
        f_show_closed: str = Form(""),
    ) -> HTMLResponse:
        try:
            project = load_project(workspace, project_id)
        except Exception:
            project = Project(id=project_id, name=name)
        project.name = name.strip() or project.name
        project.description = description.strip()
        project.color = color.strip() or "#2e6fd8"
        save_project(workspace, project)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(request, view=f_view, stale_days=parse_stale_days(f_stale_days), show_closed=parse_toggle(f_show_closed)),
        )

    @app.post("/projects/{project_id}/delete", response_class=HTMLResponse)
    def project_delete_route(
        request: Request,
        project_id: str,
        f_view: str = Form("projects"),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
        f_show_closed: str = Form(""),
    ) -> HTMLResponse:
        delete_project(workspace, project_id)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(request, view=f_view, stale_days=parse_stale_days(f_stale_days), show_closed=parse_toggle(f_show_closed)),
        )

    # --- AI Assistant -------------------------------------------------------

    @app.get("/ai/panel/task/{task_id}", response_class=HTMLResponse)
    def ai_task_panel_route(
        request: Request,
        task_id: str,
        view: str = "list",
        milestone: str = "",
        show_closed: str = "",
        stale_days: str = "",
    ) -> HTMLResponse:
        cfg = ai_config()
        try:
            task = load_task(workspace, task_id)
        except Exception:
            return HTMLResponse('<div class="empty-panel"><h2>Task not found</h2></div>')
        all_tasks = load_all_tasks(workspace)
        ctx_json = _build_task_context(task, all_tasks)
        return templates.TemplateResponse(
            request,
            "partials/assistant_panel.html",
            {
                "request": request,
                "ai_cfg": cfg,
                "context_type": "task",
                "entity_id": task_id,
                "entity_title": task.title,
                "context_json": ctx_json,
                "filters": {
                    "view": view,
                    "milestone": milestone,
                    "show_closed": parse_toggle(show_closed),
                    "stale_days": parse_stale_days(stale_days),
                },
            },
        )

    @app.get("/ai/panel/milestone/{milestone_id}", response_class=HTMLResponse)
    def ai_milestone_panel_route(
        request: Request,
        milestone_id: str,
        view: str = "list",
        show_closed: str = "",
        stale_days: str = "",
    ) -> HTMLResponse:
        cfg = ai_config()
        try:
            milestone = load_milestone(workspace, milestone_id)
        except Exception:
            return HTMLResponse('<div class="empty-panel"><h2>Milestone not found</h2></div>')
        all_tasks = load_all_tasks(workspace)
        ctx_json = _build_milestone_context(milestone, all_tasks)
        return templates.TemplateResponse(
            request,
            "partials/assistant_panel.html",
            {
                "request": request,
                "ai_cfg": cfg,
                "context_type": "milestone",
                "entity_id": milestone_id,
                "entity_title": milestone.title,
                "context_json": ctx_json,
                "filters": {
                    "view": view,
                    "milestone": milestone_id,
                    "show_closed": parse_toggle(show_closed),
                    "stale_days": parse_stale_days(stale_days),
                },
            },
        )

    @app.post("/ai/chat", response_class=HTMLResponse)
    async def ai_chat_route(
        request: Request,
        context_type: str = Form(...),
        entity_id: str = Form(...),
        user_message: str = Form(...),
        context_json: str = Form(""),
        history: str = Form("[]"),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        cfg = ai_config()

        if cfg["ai_enabled"] != "1":
            return HTMLResponse(_ai_error_html("AI is not enabled. Configure it in ⚙ Settings."))
        if not cfg["ai_base_url"]:
            return HTMLResponse(_ai_error_html("No AI endpoint configured. Set Base URL in ⚙ Settings."))
        if not cfg["ai_model"]:
            return HTMLResponse(_ai_error_html("No AI model configured. Set Model in ⚙ Settings."))

        # Parse conversation history
        try:
            history_msgs: list[dict[str, str]] = json.loads(history or "[]")
            if not isinstance(history_msgs, list):
                history_msgs = []
        except (json.JSONDecodeError, ValueError):
            history_msgs = []

        # Build messages
        user_content = f"Context ({context_type}):\n```json\n{context_json}\n```\n\nUser: {user_message}"
        messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        # Include prior turns (skip system messages already in history)
        for msg in history_msgs:
            if msg.get("role") in {"user", "assistant"}:
                messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": user_content})

        try:
            response = _ai_call(messages, cfg)
            reply_text = response["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:300]
            return HTMLResponse(_ai_error_html(f"HTTP {exc.code}: {exc.reason} — {body}"))
        except urllib.error.URLError as exc:
            return HTMLResponse(_ai_error_html(f"Connection error: {exc.reason}"))
        except Exception:
            return HTMLResponse(_ai_error_html("An unexpected error occurred. Please check your endpoint settings."))

        suggestions = _parse_ai_suggestions(reply_text)
        # Strip the JSON block from the display text
        display_text = re.sub(r"```(?:json)?\s*\{.*?\}\s*```", "", reply_text, flags=re.DOTALL).strip()

        new_history = list(history_msgs)
        new_history.append({"role": "user", "content": user_message})
        new_history.append({"role": "assistant", "content": reply_text})
        history_json = json.dumps(new_history)

        has_tasks = bool(suggestions.get("suggested_tasks"))
        has_checklist = bool(suggestions.get("suggested_checklist_items")) and context_type == "task"
        has_note = bool(suggestions.get("suggested_note"))
        tasks_json = json.dumps(suggestions.get("suggested_tasks", []))
        checklist_json = json.dumps(suggestions.get("suggested_checklist_items", []))
        note_text = str(suggestions.get("suggested_note", ""))

        rendered_md = markdown_lib.markdown(display_text, extensions=["extra", "sane_lists"])
        return HTMLResponse(
            templates.get_template("partials/ai_message.html").render({
                "reply_html": rendered_md,
                "has_tasks": has_tasks,
                "has_checklist": has_checklist,
                "has_note": has_note,
                "tasks_json": tasks_json,
                "checklist_json": checklist_json,
                "note_text": note_text,
                "entity_id": entity_id,
                "context_type": context_type,
                "history_json": history_json,
                "f_view": f_view,
                "f_milestone": f_milestone,
                "f_show_closed": f_show_closed,
                "f_stale_days": f_stale_days,
            })
        )

    @app.post("/ai/apply/tasks/{milestone_id}", response_class=HTMLResponse)
    def ai_apply_tasks_route(
        request: Request,
        milestone_id: str,
        tasks_json: str = Form("[]"),
        f_view: str = Form("list"),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        try:
            suggested: list[dict] = json.loads(tasks_json)
        except (json.JSONDecodeError, ValueError):
            suggested = []
        created = []
        for item in suggested:
            if not isinstance(item, dict) or not item.get("title"):
                continue
            task = create_task(workspace, item["title"])
            task.summary = str(item.get("summary", ""))[:_MAX_TASK_SUMMARY_LENGTH]
            if item.get("priority") in {"low", "normal", "high", "critical"}:
                task.priority = item["priority"]
            save_task(workspace, task)
            # add to milestone if valid
            try:
                add_task_to_milestone(workspace, milestone_id, task.id)
            except Exception:
                pass
            created.append(task.title)
        n = len(created)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                view=f_view,
                milestone=milestone_id,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
                git_message=f"Created {n} task{'s' if n != 1 else ''} from AI suggestions." if n else "No tasks created.",
            ),
        )

    @app.post("/ai/apply/checklist/{task_id}", response_class=HTMLResponse)
    def ai_apply_checklist_route(
        request: Request,
        task_id: str,
        checklist_json: str = Form("[]"),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        try:
            items: list[str] = json.loads(checklist_json)
        except (json.JSONDecodeError, ValueError):
            items = []
        try:
            task = load_task(workspace, task_id)
        except Exception:
            return HTMLResponse('<div class="empty-panel"><h2>Task not found</h2></div>')
        added = 0
        for item in items:
            text = str(item).strip()
            if text:
                task.checklist.append(ChecklistItem(text=text))
                added += 1
        if added:
            save_task(workspace, task)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                selected_task=task,
                view=f_view,
                milestone=f_milestone,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
                git_message=f"Added {added} checklist item{'s' if added != 1 else ''} from AI suggestions.",
            ),
        )

    @app.post("/ai/apply/note/{task_id}", response_class=HTMLResponse)
    def ai_apply_note_route(
        request: Request,
        task_id: str,
        note_text: str = Form(""),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        body = note_text.strip()
        if body:
            try:
                task = load_task(workspace, task_id)
                task.notes.append(Note(body=body))
                save_task(workspace, task)
                selected = task
            except Exception:
                selected = None
        else:
            selected = None
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                selected_task=selected,
                view=f_view,
                milestone=f_milestone,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    return app

