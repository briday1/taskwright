# Taskwright

A local-first, file-backed productivity web app for managing program tasks. Everything lives in
plain files on your disk — no database, no account, no cloud. Point Taskwright at a folder and it
serves a browser UI for dashboards, a Gantt timeline, a Kanban board, a calendar, notes,
attachments, and an editable task side panel.

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
- **Git-aware.** Built-in status chip and one-click commit + pull + push sync.
- **Rich views.** Dashboard, Kanban board, Gantt timeline with dependency markers, and calendar.

## Where to start

- New here? Read {doc}`installation` then {doc}`quickstart`.
- Want to understand the data model? See {doc}`workspace`.
- Looking for what the UI can do? See {doc}`usage`.
</content>
