# Quick start

## 1. Create a workspace

```bash
taskunity init ./my-workboard
```

This scaffolds the folder with empty `programs/`, `projects/`, `tasks/`, `milestones/`, and
`assets/` directories plus a README stub. It does not create starter projects or tasks:

```bash
taskunity init ./my-workboard
```

## 2. Serve it

```bash
taskunity serve --workspace ./my-workboard
```

Then open:

```text
http://127.0.0.1:8000
```

If you run `taskunity serve` without `--workspace`, it serves the current directory.

## 3. Work with tasks

- Click any task — a row, a board card, a timeline bar, or a calendar entry — to open the editable
  side panel.
- Edit fields and press **Save Task** to write changes back to the task's JSON file.
- Add notes, upload images, and manage the checklist from the same panel.
- Every note, image upload, and progress change is logged to the task's **Activity Log**, which
  also powers the per-task **Burndown Chart**.
- Create new tasks with the **Create Task** box in the toolbar.

## 4. Version your work (optional)

Because every task is a plain JSON file, you can put the whole workspace under git:

```bash
cd ./my-workboard
git init
git add .
git commit -m "Initial workboard"
git remote add origin <your-remote-url>
```

Once a remote is configured, the in-app git chip shows branch and ahead/behind status, and the
**Sync** button commits, pulls, and pushes in one click. See {doc}`git`.

## 5. Theme

Use the ⚙ settings button in the top-right corner to switch between **Light**, **Dark**, and
**System** (follows your OS preference, the default).
</content>
