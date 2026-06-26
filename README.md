# Taskwright

A local-first, file-backed productivity web app for managing program tasks. Everything lives in
plain files on your disk — no database, no account, no cloud. Point Taskwright at a folder and it
serves a browser UI for dashboards, a Gantt timeline, a Kanban board, a calendar, notes,
attachments, and an editable task side panel.

> Documentation: a full guide is in the [`docs/`](docs/) folder and is set up to build on
> [Read the Docs](https://readthedocs.org/) (Sphinx + Markdown).

## Why Taskwright

- **File-backed source of truth.** Each task is a single JSON file you can read, diff, and commit
  to git. The whole workspace is a folder you own.
- **Local-first.** Runs entirely on `127.0.0.1`. Your data never leaves your machine.
- **Git-aware.** The UI shows branch/ahead/behind/dirty status and has a one-click commit + pull +
  push sync button so a workspace can double as a git repo.

## Workspace layout

The source of truth is a workspace folder:

```text
workspace/
  program.json          # program spec + projects (name, description, color)
  tasks/
    TASK-0001.json      # one file per task
    A1B2-C3D4-E5F6-7890.json
  milestones/
    M-1A2B-3C4D.json    # one file per milestone (groups tasks across projects)
  assets/
    TASK-0001/          # attachments live alongside their task id
      screenshot.png
```

The app provides a browser UI for viewing dashboards, timelines, task boards, notes, attachments,
and editing tasks through a side panel.

## Install locally

From a clone of this repository:

```bash
pip install -e .
```

This installs the `taskwright` command. (A PyPI release will come later; for now install from
source.)

## Quick start

```bash
taskwright init ./my-workboard
taskwright serve --workspace ./my-workboard
```

Then open:

```text
http://127.0.0.1:8000
```

`init` scaffolds the folder with a `program.json`, a `tasks/` directory, and a sample task. Use
`--no-sample` to start empty.

## Serve an existing workspace

```bash
taskwright serve --workspace ./path/to/workspace --host 127.0.0.1 --port 8000
```

| Flag | Default | Description |
| --- | --- | --- |
| `--workspace` | `.` | Workspace folder to serve |
| `--host` | `127.0.0.1` | Host interface to bind |
| `--port` | `8000` | Port to listen on |
| `--reload` | off | Enable uvicorn auto-reload (development) |

## Task file example

```json
{
  "id": "TASK-0001",
  "title": "CAF refinement prototype",
  "status": "working",
  "priority": "high",
  "project": "Apollo",
  "summary": "Build a prototype CAF refinement workflow.",
  "description": "Compare baseline CAF sharpness against protection-aware refinement.",
  "tags": ["caf", "signal-processing", "prototype"],
  "start_date": "2026-06-24",
  "due_date": "2026-06-30",
  "completed_date": null,
  "percent_complete": 60,
  "depends_on": ["TASK-0002"],
  "checklist": [
    {"text": "Generate baseline CAF", "done": true},
    {"text": "Export comparison plots", "done": false}
  ],
  "notes": [
    {
      "created_at": "2026-06-26T10:30:00",
      "body": "Protection-aware version improved sharpness."
    }
  ],
  "attachments": []
}
```

`depends_on` stores task **ids**. In the UI you add dependencies with a search-as-you-type picker
that finds tasks by name and shows their status, project, due date, and id — then stores the id.
Dependencies are reflected on the Gantt timeline (an `↳ after <name>` label plus a marker where the
dependency's bar ends).

## Milestones

Milestones are a separate entity from projects. A milestone can span **multiple projects**, holds an
**ordered list of tasks** (which can come from any project, and a task can belong to any number of
milestones), and has its own description, notes, and attachments — just like a task.

- Open the **Milestones** view to see every milestone with a live rollup (task count, progress,
  target date).
- Click a milestone to **filter the whole board to just its tasks** and show a rollup banner; the
  side panel opens the milestone for editing.
- From inside a milestone you can **create a new task** (added to the milestone automatically) or
  **add an existing task** with a search-as-you-type picker, reorder tasks, and remove them.

Milestones are stored one JSON file per milestone under `milestones/`.

## Features

- JSON-per-task source model you can version with git
- Dashboard summary cards (total / done / working / blocked)
- Kanban-style board with per-project color strips
- Gantt timeline with dependency markers
- Calendar view
- **Milestones** that group tasks across projects, with an ordered task list, a
  click-to-filter rollup, and their own notes/attachments/description
- Click any task (row, card, timeline bar, calendar entry) to open an editable side panel
- Save edits back to the JSON file; raw JSON editor escape hatch
- Notes list and attachment/image uploads
- Searchable "depends on" picker that resolves names to task ids
- Project management with custom colors
- Filtering by project, milestone, date range, and free-text search; sortable views
- CSV / JSON export
- Built-in git status chip and one-click sync (commit + pull + push)

## Building the docs locally

```bash
pip install -e ".[docs]"
sphinx-build -b html docs docs/_build/html
```

Open `docs/_build/html/index.html`. On Read the Docs the build is driven by
[`.readthedocs.yaml`](.readthedocs.yaml).

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

## License

MIT


