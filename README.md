# Taskwright

A local-first, file-backed productivity web app for managing program tasks.

The source of truth is a workspace folder containing individual JSON task files:

```text
workspace/
  program.json
  tasks/
    TASK-0001.json
    TASK-0002.json
  assets/
    TASK-0001/
      screenshot.png
```

The app provides a browser UI for viewing dashboards, timelines, task boards, notes, attachments, and editing tasks through a side panel.

## Install locally

From this repository:

```bash
pip install -e .
```

## Initialize a new taskwright workspace

```bash
taskwright init ./my-workboard
cd ./my-workboard
taskwright serve
```

Then open:

```text
http://127.0.0.1:8000
```

## Serve an existing workspace

```bash
taskwright serve --workspace ./path/to/workspace --host 127.0.0.1 --port 8000
```

## Task file example

```json
{
  "id": "TASK-0001",
  "title": "CAF refinement prototype",
  "status": "working",
  "priority": "high",
  "owner": "Brian",
  "summary": "Build a prototype CAF refinement workflow.",
  "description": "Compare baseline CAF sharpness against protection-aware refinement.",
  "tags": ["caf", "signal-processing", "prototype"],
  "start_date": "2026-06-24",
  "due_date": "2026-06-30",
  "completed_date": null,
  "percent_complete": 60,
  "depends_on": [],
  "checklist": [
    {"text": "Generate baseline CAF", "done": true},
    {"text": "Export comparison plots", "done": false}
  ],
  "notes": [
    {
      "created_at": "2026-06-26T10:30:00",
      "body": "Initial version was worse. Protection-aware version improved sharpness."
    }
  ],
  "attachments": []
}
```

## Current MVP features

- JSON-per-task source model
- Dashboard summary
- Kanban-style board
- Simple timeline
- Click a task to open an editable side panel
- Save edits back to the JSON file
- Add notes
- Upload attachments/images
- Create new tasks
- Raw JSON editor escape hatch

