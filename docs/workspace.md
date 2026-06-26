# Workspace & data model

A Taskwright workspace is just a folder. Everything is a plain file you own, can diff, and can
commit to git.

```text
workspace/
  program.json          # program spec + projects (name, description, color)
  tasks/
    TASK-0001.json      # one file per task
    A1B2-C3D4-E5F6-7890.json
  milestones/
    M-1A2B-3C4D.json    # one file per milestone
  assets/
    TASK-0001/          # attachments live alongside their task id
      screenshot.png
```

## `program.json`

Holds the program-level spec and the list of projects. Each project has a `name`, optional
`description`, and a `color` used to tint board cards and timeline labels.

```json
{
  "name": "My Program",
  "description": "What this program is about",
  "projects": [
    {"name": "Apollo", "description": "First initiative", "color": "#d82cd3"},
    {"name": "Gemini", "description": "Second initiative", "color": "#2e6fd8"}
  ]
}
```

## Task files

Each task is one JSON file in `tasks/`. Task ids may be the readable `TASK-0001` form or a random
`XXXX-XXXX-XXXX-XXXX` hex id generated when you create tasks in the UI.

```json
{
  "id": "TASK-0001",
  "title": "CAF refinement prototype",
  "status": "working",
  "priority": "high",
  "project": "Apollo",
  "summary": "Build a prototype CAF refinement workflow.",
  "description": "Compare baseline sharpness against protection-aware refinement.",
  "tags": ["caf", "prototype"],
  "start_date": "2026-06-24",
  "due_date": "2026-06-30",
  "completed_date": null,
  "percent_complete": 60,
  "depends_on": ["TASK-0002"],
  "checklist": [
    {"text": "Generate baseline", "done": true},
    {"text": "Export plots", "done": false}
  ],
  "notes": [
    {"created_at": "2026-06-26T10:30:00", "body": "Progress note."}
  ],
  "attachments": []
}
```

### Field reference

| Field | Type | Notes |
| --- | --- | --- |
| `id` | string | Unique; matches the filename |
| `title` | string | Display name |
| `status` | string | e.g. `backlog`, `working`, `blocked`, `done` |
| `priority` | string | e.g. `low`, `medium`, `high` |
| `project` | string | Project name from `program.json` |
| `summary` | string | Short one-liner |
| `description` | string | Longer description |
| `tags` | string[] | Free-form labels |
| `start_date` / `due_date` | date | `YYYY-MM-DD`; used by the timeline and calendar |
| `completed_date` | date or null | Set when finished |
| `percent_complete` | int | 0–100, drives progress bars |
| `depends_on` | string[] | Other task **ids** this task depends on |
| `checklist` | object[] | `{text, done}` items |
| `notes` | object[] | `{created_at, body}` entries |
| `attachments` | object[] | `{filename, path, kind, description, uploaded_at}` |

### Dependencies

`depends_on` stores task **ids**. In the UI you add dependencies with a search-as-you-type picker
that finds tasks by name and shows their status, project, due date, and id — then stores the id.
Dependencies appear on the Gantt timeline as an `↳ after <name>` label plus a marker where the
dependency's bar ends.

## Attachments

Uploaded files are stored under `assets/<TASK-ID>/` and referenced from the task's `attachments`
array. Removing a task's attachment entries is as simple as editing the JSON (or using the panel).

## Milestone files

Each milestone is one JSON file in `milestones/`. Milestones group tasks across projects and have
their own notes and attachments. Milestone ids look like `M-XXXX-XXXX`.

```json
{
  "id": "M-1A2B-3C4D",
  "title": "Beta launch",
  "status": "active",
  "summary": "First public beta.",
  "description": "Cross-project work required to ship the beta.",
  "projects": ["Apollo", "Gemini"],
  "start_date": "2026-06-24",
  "target_date": "2026-07-15",
  "task_ids": ["TASK-0001", "TASK-0002"],
  "notes": [],
  "attachments": [],
  "extra": {}
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `id` | string | Unique; matches the filename |
| `title` | string | Display name |
| `status` | string | `planned`, `active`, or `done` |
| `summary` | string | Short one-liner |
| `description` | string | Longer description |
| `projects` | string[] | Project names this milestone spans |
| `start_date` / `target_date` | date | `YYYY-MM-DD` |
| `task_ids` | string[] | **Ordered** task ids belonging to the milestone |
| `notes` | object[] | `{created_at, body}` entries |
| `attachments` | object[] | `{filename, path, kind, description, uploaded_at}` |

A task may appear in any number of milestones, and milestones never duplicate task data — they only
reference task ids. See {doc}`milestones` for how to use them.
</content>
