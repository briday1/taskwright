# Git integration

Because a Taskwright workspace is a folder of plain files, it works naturally with git. Taskwright
adds a lightweight status chip and a one-click sync so you rarely need to leave the app.

## Requirements

- `git` installed and on your `PATH`.
- The workspace folder is a git repository (`git init` inside it).
- A remote named `origin` is configured if you want pull/push to work.

```bash
cd ./my-workboard
git init
git add .
git commit -m "Initial workboard"
git remote add origin <your-remote-url>
git push -u origin main
```

## The status chip

The toolbar chip reflects the workspace repository:

- **Branch** name.
- **Ahead / behind** counts versus the upstream (`↑`/`↓`).
- **Dirty** indicator (`●`) with the number of uncommitted changes.
- **Synced** (`✓`) when clean and up to date.
- **No remote** when no upstream is configured.

All git operations are scoped to the workspace folder, so they only touch your task files even if
the workspace happens to live inside a larger repository.

## The Sync button

**Sync** performs, in order:

1. `git add -A` (scoped to the workspace)
2. `git commit` (if there are staged changes)
3. `git pull --no-edit`
4. `git push`

A toast reports the result. Dismiss it with the **×**, or it auto-dismisses after a few seconds.

## Tip: keep the workspace separate

If you keep your workboard inside another git repository, prefer giving it its own repo (or its own
folder you track separately) so commits don't accidentally sweep in unrelated source changes.
</content>
