# Milestones

Milestones are a separate concept from projects. Where a **project** is a single bucket a task
belongs to, a **milestone** is a goal that groups an **ordered list of tasks** which can come from
**any project** — and a task can belong to **any number of milestones**.

Each milestone has its own description, notes, and attachments, just like a task. Milestones are
stored one JSON file per milestone under `milestones/` (see {doc}`workspace`).

## The Milestones view

Open the **Milestones** view from the toolbar to see every milestone as a card with a live rollup:

- task count and how many are done,
- overall progress (the average of the tasks' percent-complete),
- assigned projects, and
- target date.

Each milestone also has its own **colour**, which tints its card and rollup visuals so milestones
are easy to distinguish at a glance.

Create a milestone with the **New milestone** form at the bottom of the view.

## Opening a milestone (filter + rollup)

Click a milestone card to **open** it. This:

1. **Filters the whole workspace to that milestone's tasks** — the board, list, Gantt, and calendar
   all show only the tasks in the milestone. A removable `Milestone: …` pill appears in the filter
   bar.
2. Shows a **rollup banner** above the view with totals and progress.
3. Opens the milestone in the **side panel** for editing.

Use **Clear milestone** (in the banner) or remove the pill to return to all tasks.

## Editing a milestone

The side panel lets you edit the milestone's title, status (`planned` / `active` / `done`),
**colour**, summary, description, start and target dates, and the **projects** it spans (a
milestone can belong to multiple projects). It also has its own **notes** and **attachments**.

## Managing tasks in a milestone

From the milestone panel, the **Tasks** section lists every task in the milestone in a scrollable
box and lets you:

- **Add a task** with a search-as-you-type picker that finds existing tasks by name and shows their
  status, project, due date, and id.
- **Quick-add a new task** with the **+ New task** button pinned at the bottom of the task box. The
  button stays visible while the task list scrolls and creates a task already added to the
  milestone.
- **Remove** a task from the milestone with the × (this does not delete the task).

Clicking a task opens that task's detail panel (the milestone filter stays active so you can move
between the milestone and its tasks). Task ordering and prioritisation live in the task views
themselves, so the milestone simply shows the set of tasks it groups.
</content>
