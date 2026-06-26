from __future__ import annotations

import csv
import io
import json
import urllib.parse
from datetime import date
from pathlib import Path

import markdown as markdown_lib
from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .models import ChecklistItem, Task
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
    add_attachment,
    add_milestone_attachment,
    add_milestone_note,
    add_note,
    add_task_to_milestone,
    available_projects,
    create_milestone,
    create_task,
    delete_milestone,
    delete_task,
    ensure_workspace,
    git_status,
    git_sync,
    load_all_milestones,
    load_all_tasks,
    load_all_projects,
    load_milestone,
    load_workspace_config,
    load_task,
    project_colors,
    register_project,
    remove_task_from_milestone,
    save_milestone,
    save_task,
    upsert_project,
    workspace_label,
)

PACKAGE_DIR = Path(__file__).parent


def markdown_filter(text: str) -> str:
    return markdown_lib.markdown(text or "", extensions=["extra", "sane_lists"])


def create_app(workspace: str | Path = ".") -> FastAPI:
    workspace = Path(workspace).resolve()
    ensure_workspace(workspace)
    initial_config = load_workspace_config(workspace)
    app_name = initial_config["app_name"] or "Taskwright"

    app = FastAPI(title=app_name)
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    templates.env.filters["markdown"] = markdown_filter

    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
    app.mount("/assets", StaticFiles(directory=str(workspace / "assets")), name="assets")

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
        app_name = config["app_name"] or "Taskwright"
        workspace_name = config["workspace_name"] or workspace_label(workspace)
        export_title = config["export_title"] or workspace_name
        return {
            "app_name": app_name,
            "workspace_name": workspace_name,
            "workspace_description": config["workspace_description"],
            "export_title": export_title,
        }

    def parse_calendar_year(value: str | int | None) -> int | None:
        try:
            year = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return year if 1900 <= year <= 3000 else None

    def build_query(
        projects: list[str], date_from: str, date_to: str, q: str, view: str = "", sort: str = "",
        milestone: str = "", show_closed: bool = False, stale_days: int = STALE_CLOSED_DAYS,
        calendar_month: int | None = None, calendar_year: int | None = None,
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
        if show_closed:
            params.append(("show_closed", "1"))
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
        view: str = "board",
        milestone: str = "",
        show_closed: bool = False,
        stale_days: int = STALE_CLOSED_DAYS,
        calendar_month: int | None = None,
        calendar_year: int | None = None,
        git_message: str = "",
    ) -> dict:
        projects = [p for p in (projects or []) if p]
        q = (q or "").strip()
        sort = sort if sort in SORTS else "priority"
        view = view if view in VIEWS else "board"
        query_params = request.query_params
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
        all_tasks = load_all_tasks(workspace)
        milestones = load_all_milestones(workspace)
        tasks_by_id = {t.id: t for t in all_tasks}
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

        filtered = sort_tasks(filter_tasks(candidate_tasks, projects, date_from, date_to, q), sort)
        hidden_closed_count = 0
        if not show_closed:
            filtered, hidden_closed_count = hide_stale_closed_tasks(filtered, stale_days)
        all_projects = load_all_projects(workspace)
        colors = project_colors(all_projects, all_tasks)

        pills = []
        if selected_milestone is not None:
            pills.append(
                {
                    "label": f"Milestone: {selected_milestone.title}",
                    "color": "",
                    "remove": build_query(projects, date_from, date_to, q, view, sort, show_closed=show_closed, stale_days=stale_days),
                }
            )
        for p in projects:
            others = [x for x in projects if x != p]
            pills.append(
                {
                    "label": f"Project: {p}",
                    "color": colors.get(p, ""),
                    "remove": build_query(others, date_from, date_to, q, view, sort, milestone, show_closed, stale_days),
                }
            )
        if date_from:
            pills.append(
                {
                    "label": f"From {date_from}",
                    "color": "",
                    "remove": build_query(projects, "", date_to, q, view, sort, milestone, show_closed, stale_days),
                }
            )
        if date_to:
            pills.append(
                {
                    "label": f"To {date_to}",
                    "color": "",
                    "remove": build_query(projects, date_from, "", q, view, sort, milestone, show_closed, stale_days),
                }
            )
        if q:
            pills.append(
                {
                    "label": f'Search: "{q}"',
                    "color": "",
                    "remove": build_query(projects, date_from, date_to, "", view, sort, milestone, show_closed, stale_days),
                }
            )

        if show_closed:
            pills.append(
                {
                    "label": f"Show old stuff ({stale_days}d+)",
                    "color": "",
                    "remove": build_query(projects, date_from, date_to, q, view, sort, milestone, False, stale_days, focus_month, focus_year),
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
            "project_colors": colors,
            "sorts": SORTS,
            "calendar": build_calendar(filtered, date_from, date_to, focus_month, focus_year),
            "git": git_status(workspace),
            "git_message": git_message,
            "task_index": [
                {
                    "id": t.id,
                    "title": t.title,
                    "status": t.status,
                    "project": t.project,
                    "due_date": t.due_date or "",
                }
                for t in sort_tasks(all_tasks, "title")
            ],
            "task_titles": {t.id: t.title for t in all_tasks},
            "filters": {
                "projects": projects,
                "date_from": date_from,
                "date_to": date_to,
                "q": q,
                "sort": sort,
                "view": view,
                "milestone": milestone,
                "stale_days": stale_days,
                "calendar_month": focus_month,
                "calendar_year": focus_year,
                "query": build_query(projects, date_from, date_to, q, "", sort, milestone, show_closed, stale_days, focus_month, focus_year),
                "query_no_sort": build_query(projects, date_from, date_to, q, "", "", milestone, show_closed, stale_days, focus_month, focus_year),
                "calendar_query": build_query(projects, date_from, date_to, q, "calendar", sort, milestone, show_closed, stale_days, focus_month, focus_year),
                "calendar_prev_query": build_query(projects, date_from, date_to, q, "calendar", sort, milestone, show_closed, stale_days, prev_month, prev_year),
                "calendar_next_query": build_query(projects, date_from, date_to, q, "calendar", sort, milestone, show_closed, stale_days, next_month, next_year),
                "calendar_year_prev_query": build_query(projects, date_from, date_to, q, "calendar", sort, milestone, show_closed, stale_days, year_prev_month, year_prev_year),
                "calendar_year_next_query": build_query(projects, date_from, date_to, q, "calendar", sort, milestone, show_closed, stale_days, year_next_month, year_next_year),
                "show_closed": show_closed,
                "hidden_closed_count": hidden_closed_count,
                "toggle_closed_query": build_query(projects, date_from, date_to, q, view, sort, milestone, not show_closed, stale_days, focus_month, focus_year),
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
        view: str = "board",
        milestone: str = "",
        show_closed: str = "",
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
                view=view,
                milestone=milestone,
                show_closed=parse_toggle(show_closed),
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
        view: str = "board",
        milestone: str = "",
        show_closed: str = "",
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
                view=view,
                milestone=milestone,
                show_closed=parse_toggle(show_closed),
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
        view: str = "board",
        milestone: str = "",
        show_closed: str = "",
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
        f_view: str = Form("board"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
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
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/tasks/{task_id}/save", response_class=HTMLResponse)
    async def save_task_route(
        request: Request,
        task_id: str,
        title: str = Form(...),
        status: str = Form("backlog"),
        priority: str = Form("normal"),
        project: str = Form(""),
        summary: str = Form(""),
        description: str = Form(""),
        tags: str = Form(""),
        start_date: str = Form(""),
        due_date: str = Form(""),
        completed_date: str = Form(""),
        percent_complete: int = Form(0),
        depends_on: str = Form(""),
        checklist_text: str = Form(""),
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("board"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        task = load_task(workspace, task_id)
        task.title = title
        task.status = status  # pydantic validation occurs at save roundtrip in raw mode; keep simple for forms
        task.priority = priority
        task.project = project.strip()
        task.summary = summary
        task.description = description
        task.tags = [x.strip() for x in tags.split(",") if x.strip()]
        task.start_date = start_date or None
        task.due_date = due_date or None
        task.completed_date = completed_date or None
        task.percent_complete = max(0, min(int(percent_complete), 100))
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

    @app.post("/tasks/{task_id}/checklist/add", response_class=HTMLResponse)
    async def checklist_add_route(
        request: Request,
        task_id: str,
        item_text: str = Form(""),
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("board"),
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
        f_view: str = Form("board"),
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
        f_view: str = Form("board"),
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
        f_view: str = Form("board"),
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
        task = add_note(workspace, task_id, body)
        return templates.TemplateResponse(request, "partials/task_panel.html", context(request, task))

    @app.post("/tasks/{task_id}/attachment", response_class=HTMLResponse)
    async def upload_attachment_route(
        request: Request,
        task_id: str,
        attachment: UploadFile = File(...),
        description: str = Form(""),
    ) -> HTMLResponse:
        task = add_attachment(
            workspace,
            task_id,
            attachment.filename or "attachment.bin",
            await attachment.read(),
            attachment.content_type,
            description,
        )
        return templates.TemplateResponse(request, "partials/task_panel.html", context(request, task))

    @app.post("/tasks/{task_id}/complete", response_class=HTMLResponse)
    async def complete_task_route(
        request: Request,
        task_id: str,
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("board"),
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
            task.percent_complete = 100
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
        f_view: str = Form("board"),
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
    ) -> HTMLResponse:
        upsert_project(workspace, name, description.strip(), color)
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
                view="board",
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
        view: str = "board",
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

    @app.post("/milestones/{milestone_id}/save", response_class=HTMLResponse)
    def save_milestone_route(
        request: Request,
        milestone_id: str,
        title: str = Form(...),
        status: str = Form("active"),
        color: str = Form("#3567e0"),
        summary: str = Form(""),
        description: str = Form(""),
        projects: list[str] = Form(default=[]),
        start_date: str = Form(""),
        target_date: str = Form(""),
        f_view: str = Form("board"),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        milestone = load_milestone(workspace, milestone_id)
        milestone.title = title
        milestone.status = status if status in {"planned", "active", "done"} else "active"
        milestone.color = (color or "").strip() or "#3567e0"
        milestone.summary = summary
        milestone.description = description
        milestone.projects = [p.strip() for p in projects if p.strip()]
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
        f_view: str = Form("board"),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
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
        f_view: str = Form("board"),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
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
        f_view: str = Form("board"),
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
        f_view: str = "board",
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
        f_view: str = "board",
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
        f_view: str = Form("board"),
        f_sort: str = Form("priority"),
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
                view=f_view,
                milestone=f_milestone,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
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
        tasks = filter_tasks(load_all_tasks(workspace), project, date_from, date_to, q)
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
            headers={"Content-Disposition": "attachment; filename=taskwright-export.csv"},
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
        tasks = filter_tasks(load_all_tasks(workspace), project, date_from, date_to, q)
        if not parse_toggle(show_closed):
            tasks, _ = hide_stale_closed_tasks(tasks, parse_stale_days(stale_days))
        projects = load_all_projects(workspace)
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
            headers={"Content-Disposition": "attachment; filename=taskwright-export.json"},
        )

    @app.get("/healthz")
    def healthz() -> Response:
        return Response("ok", media_type="text/plain")

    return app
