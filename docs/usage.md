# Using the app

Open `http://127.0.0.1:8000` after starting the server. The interface is a single page with a
toolbar, a main view area, and a task detail side panel.

## Summary cards

Across the top: **Total**, **Done**, **Working**, and **Blocked** counts for the currently filtered
set of tasks.

## Views

Switch views from the toolbar:

- **Task List** — a compact, sortable list of every task.
- **Task Board** — Kanban columns by status, with a colored strip per project.
- **Gantt** — a timeline of tasks by start/due date. Dependencies show an `↳ after <name>` label
  and a marker where the dependency's bar ends.
- **Calendar** — tasks placed on their due dates.
- **Milestones** — milestones that group tasks across projects, each with a rollup. See
  {doc}`milestones`.
- **Projects** — manage projects and their colors.

### Task List

![Task List view](_static/screenshots/task-list.png)

### Task Board

![Task Board view](_static/screenshots/task-board.png)

### Gantt Timeline

![Gantt timeline view](_static/screenshots/gantt.png)

### Calendar

![Calendar view](_static/screenshots/calendar.png)

### Milestones

![Milestones view](_static/screenshots/milestones.png)

### Projects

![Projects view](_static/screenshots/projects.png)

## The task panel

Click any task (a list row, board card, timeline bar, or calendar entry) to open the side panel.
From there you can:

- Edit core fields (title, status, priority, project, dates, percent complete, tags, summary,
  description).
- Add **dependencies** with the searchable "Depends on" picker — type a task name, see its status /
  project / due date / id, and click to add it. The picker stores the underlying task id.
- Manage the **checklist**.
- Add **notes** (newest first).
- Upload **attachments** / images.
- Use the **raw JSON editor** as an escape hatch for anything the form doesn't cover.

Press **Save Task** to write changes back to the task's JSON file. **Complete** / **Reopen** toggles
the done state.

![Task detail panel](_static/screenshots/task-panel.png)

## Filtering, search, and sorting

The toolbar's filter controls let you narrow tasks by:

- **Project** (checkboxes)
- **Date range** (from / to)
- **Free-text search** across id, title, project, summary, description, and tags

Use the **Sort** dropdown to order by priority, due date, title, status, progress, or project.
Active filters render as removable pills.

## Export

Export the current set with **Export CSV** or **Export JSON** from the toolbar.

## Git status & sync

When the workspace is a git repository, a chip in the toolbar shows the branch and ahead/behind/
dirty status. The **Sync** button commits, pulls, and pushes in one step. See {doc}`git`.

## Creating tasks

Use the **Create Task** box in the toolbar. New tasks created in the UI get a random
`XXXX-XXXX-XXXX-XXXX` id and a matching JSON file in `tasks/`.
</content>
