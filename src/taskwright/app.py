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
    sort_tasks,
)
from .task_store import (
    add_attachment,
    add_note,
    available_projects,
    create_task,
    delete_task,
    ensure_workspace,
    git_status,
    git_sync,
    load_all_tasks,
    load_program,
    load_task,
    project_colors,
    register_project,
    save_task,
    upsert_project,
)

PACKAGE_DIR = Path(__file__).parent


def markdown_filter(text: str) -> str:
    return markdown_lib.markdown(text or "", extensions=["extra", "sane_lists"])


def create_app(workspace: str | Path = ".") -> FastAPI:
    workspace = Path(workspace).resolve()
    ensure_workspace(workspace)

    app = FastAPI(title="Taskwright")
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    templates.env.filters["markdown"] = markdown_filter

    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
    app.mount("/assets", StaticFiles(directory=str(workspace / "assets")), name="assets")

    VIEWS = {"list", "board", "gantt", "calendar", "projects"}

    def build_query(
        projects: list[str], date_from: str, date_to: str, q: str, view: str = "", sort: str = ""
    ) -> str:
        params: list[tuple[str, str]] = [("project", p) for p in projects if p]
        if date_from:
            params.append(("date_from", date_from))
        if date_to:
            params.append(("date_to", date_to))
        if q:
            params.append(("q", q))
        if sort and sort != "priority":
            params.append(("sort", sort))
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
        git_message: str = "",
    ) -> dict:
        projects = [p for p in (projects or []) if p]
        q = (q or "").strip()
        sort = sort if sort in SORTS else "priority"
        view = view if view in VIEWS else "board"
        all_tasks = load_all_tasks(workspace)
        program = load_program(workspace)
        filtered = sort_tasks(filter_tasks(all_tasks, projects, date_from, date_to, q), sort)
        colors = project_colors(program, all_tasks)

        pills = []
        for p in projects:
            others = [x for x in projects if x != p]
            pills.append(
                {
                    "label": f"Project: {p}",
                    "color": colors.get(p, ""),
                    "remove": build_query(others, date_from, date_to, q, view, sort),
                }
            )
        if date_from:
            pills.append(
                {"label": f"From {date_from}", "color": "", "remove": build_query(projects, "", date_to, q, view, sort)}
            )
        if date_to:
            pills.append(
                {"label": f"To {date_to}", "color": "", "remove": build_query(projects, date_from, "", q, view, sort)}
            )
        if q:
            pills.append(
                {"label": f'Search: "{q}"', "color": "", "remove": build_query(projects, date_from, date_to, "", view, sort)}
            )

        return {
            "request": request,
            "program": program,
            "model": dashboard_model(filtered),
            "statuses": STATUSES,
            "selected_task": selected_task,
            "workspace": workspace,
            "projects": available_projects(program, all_tasks),
            "project_colors": colors,
            "sorts": SORTS,
            "calendar": build_calendar(filtered, date_from, date_to),
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
                "query": build_query(projects, date_from, date_to, q, "", sort),
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
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "index.html",
            context(request, projects=project, date_from=date_from, date_to=date_to, q=q, sort=sort, view=view),
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
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(request, projects=project, date_from=date_from, date_to=date_to, q=q, sort=sort, view=view),
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
    ) -> HTMLResponse:
        task = load_task(workspace, task_id)
        return templates.TemplateResponse(
            request,
            "partials/task_panel.html",
            context(request, task, projects=project, date_from=date_from, date_to=date_to, q=q, view=view),
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
    ) -> HTMLResponse:
        task = create_task(workspace, title)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(request, task, projects=f_project, date_from=f_from, date_to=f_to, q=f_q, view=f_view),
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
            context(request, task, projects=f_project, date_from=f_from, date_to=f_to, q=f_q, view=f_view),
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
    ) -> HTMLResponse:
        parsed = json.loads(raw_json)
        task = Task.model_validate(parsed)
        save_task(workspace, task)
        register_project(workspace, task.project)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(request, task, projects=f_project, date_from=f_from, date_to=f_to, q=f_q, view=f_view),
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
            context(request, task, projects=f_project, date_from=f_from, date_to=f_to, q=f_q, view=f_view),
        )

    @app.post("/tasks/{task_id}/delete")
    async def delete_task_route(
        task_id: str,
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("board"),
    ) -> RedirectResponse:
        delete_task(workspace, task_id)
        params: list[tuple[str, str]] = [("project", p) for p in f_project if p]
        if f_from:
            params.append(("date_from", f_from))
        if f_to:
            params.append(("date_to", f_to))
        if f_q:
            params.append(("q", f_q))
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

    @app.post("/git/sync", response_class=HTMLResponse)
    def git_sync_route(
        request: Request,
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("board"),
        f_sort: str = Form("priority"),
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
                git_message=result["message"],
            ),
        )

    @app.get("/export/csv")
    def export_csv(
        project: list[str] = Query(default=[]), date_from: str = "", date_to: str = "", q: str = ""
    ) -> Response:
        tasks = filter_tasks(load_all_tasks(workspace), project, date_from, date_to, q)
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
        project: list[str] = Query(default=[]), date_from: str = "", date_to: str = "", q: str = ""
    ) -> Response:
        tasks = filter_tasks(load_all_tasks(workspace), project, date_from, date_to, q)
        data = [task.model_dump(mode="json") for task in tasks]
        return Response(
            json.dumps(data, indent=2, ensure_ascii=False),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=taskwright-export.json"},
        )

    @app.get("/healthz")
    def healthz() -> Response:
        return Response("ok", media_type="text/plain")

    return app
