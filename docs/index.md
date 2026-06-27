# Taskunity

A local-first, file-backed productivity web app for managing program tasks. Everything lives in
plain files on your disk — no database, no account, no cloud. Point Taskunity at a folder and it
serves a browser UI for dashboards, a Gantt timeline, a Kanban board, a calendar, activity logs,
burndown charts, and an editable task side panel.

```{toctree}
:maxdepth: 2
:caption: Contents

installation
quickstart
workspace
usage
milestones
cli
git
development
```

## Highlights

- **File-backed source of truth.** Each task is a single JSON file you can read, diff, and commit
  to git.
- **Local-first.** Runs entirely on `127.0.0.1`; your data never leaves your machine.
- **Git-aware.** Built-in status chip, one-click commit + pull + push sync, and optional Git LFS
  support for large attachments.
- **Rich views.** Dashboard, Kanban board, Gantt timeline with dependency markers, and calendar.
- **Activity log.** Notes, image uploads, and progress changes are all recorded in a unified
  chronological feed per task.
- **Burndown charts.** Per-task and per-milestone burndown charts derived from activity events.
- **Themes.** Light, dark, and system (follows OS preference) themes switchable in one click.

## Where to start

- New here? Read {doc}`installation` then {doc}`quickstart`.
- Want to understand the data model? See {doc}`workspace`.
- Looking for what the UI can do? See {doc}`usage`.
</content>
