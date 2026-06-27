# Git integration

Because a Taskunity workspace is a folder of plain files, it works naturally with git. Taskunity
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

Git integration only activates when the workspace folder itself is the repository root. If the
workspace lives inside some other repository, the status chip stays disabled and Sync will refuse to
run so Taskunity never pulls from or pushes to the parent project.

## The Sync button

**Sync** performs, in order:

1. `git add -A` (scoped to the workspace repo root)
2. `git commit` (if there are staged changes)
3. `git pull --no-edit`
4. `git push`

A toast reports the result. Dismiss it with the **×**, or it auto-dismisses after a few seconds.

## Git LFS (large file storage)

If your workspace stores many images or large binary attachments, you can use
[Git LFS](https://git-lfs.com) to track the `assets/` directory efficiently.

### Requirements

- `git-lfs` installed and on your `PATH` (see <https://git-lfs.com>).
- The workspace must be a tracked git repository (see above).

### Enabling

When `git-lfs` is available, a **Enable LFS** button appears next to the git chip. Clicking it:

1. Runs `git lfs install --local` in the workspace.
2. Runs `git lfs track "assets/**"` to mark all asset files for LFS.
3. Commits the resulting `.gitattributes`.

After enabling, all future `assets/` uploads are stored in LFS automatically.

## Tip: keep the workspace separate

Give each workboard its own repository root. If you want the workspace under another project folder,
initialize git inside the workspace itself rather than relying on the parent repository.
</content>
