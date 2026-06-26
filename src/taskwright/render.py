from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from .models import Milestone, Task

STATUSES = ["backlog", "working", "blocked", "done"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MAX_CALENDAR_WEEKS = 26
PRIORITY_ORDER = {"critical": 0, "high": 1, "normal": 2, "low": 3}
STATUS_ORDER = {"working": 0, "blocked": 1, "backlog": 2, "done": 3}
SORTS = {
    "priority": "Priority",
    "due_date": "Due date",
    "title": "Title",
    "status": "Status",
    "percent_complete": "Progress",
    "project": "Project",
}


def sort_tasks(tasks: list[Task], sort: str = "priority") -> list[Task]:
    if sort == "due_date":
        return sorted(tasks, key=lambda t: (parse_date(t.due_date) or date.max, t.title.lower()))
    if sort == "title":
        return sorted(tasks, key=lambda t: t.title.lower())
    if sort == "status":
        return sorted(tasks, key=lambda t: (STATUS_ORDER.get(t.status, 9), t.title.lower()))
    if sort == "percent_complete":
        return sorted(tasks, key=lambda t: (-t.percent_complete, t.title.lower()))
    if sort == "project":
        return sorted(tasks, key=lambda t: ((t.project or "~").lower(), t.title.lower()))
    return sorted(tasks, key=lambda t: (PRIORITY_ORDER.get(t.priority, 9), t.title.lower()))


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None


def dashboard_model(tasks: list[Task]) -> dict:
    counts = Counter(t.status for t in tasks)
    by_status = {status: [] for status in STATUSES}
    for task in tasks:
        by_status.setdefault(task.status, []).append(task)

    done = counts.get("done", 0)
    total = len(tasks)
    progress = round((done / total) * 100) if total else 0

    upcoming = sorted(
        [t for t in tasks if t.status != "done" and parse_date(t.due_date)],
        key=lambda t: parse_date(t.due_date) or date.max,
    )[:8]

    blocked = [t for t in tasks if t.status == "blocked"]
    recent_notes = []
    for task in tasks:
        for note in task.notes[-3:]:
            recent_notes.append({"task": task, "note": note})
    recent_notes.sort(key=lambda x: x["note"].created_at, reverse=True)

    return {
        "tasks": tasks,
        "counts": counts,
        "by_status": by_status,
        "total": total,
        "progress": progress,
        "upcoming": upcoming,
        "blocked": blocked,
        "recent_notes": recent_notes[:8],
        "timeline": build_timeline(tasks),
    }


def build_timeline(tasks: list[Task]) -> dict:
    dated = []
    all_dates = []
    for task in tasks:
        start = parse_date(task.start_date) or parse_date(task.due_date)
        end = parse_date(task.completed_date) or parse_date(task.due_date) or start
        if start and end:
            if end < start:
                start, end = end, start
            dated.append((task, start, end))
            all_dates.extend([start, end])

    if not dated:
        return {"rows": [], "start": None, "end": None, "days": 0}

    start_min = min(all_dates)
    end_max = max(all_dates)
    total_days = max((end_max - start_min).days + 1, 1)
    items = []
    for task, start, end in dated:
        left = ((start - start_min).days / total_days) * 100
        width = max((((end - start).days + 1) / total_days) * 100, 2)
        items.append({"task": task, "left": left, "width": width, "start": start, "end": end})
    end_pos = {item["task"].id: item["left"] + item["width"] for item in items}
    for item in items:
        item["dep_marks"] = [
            {"id": dep, "pos": min(end_pos[dep], 100)}
            for dep in item["task"].depends_on
            if dep in end_pos
        ]
    return {"rows": items, "start": start_min, "end": end_max, "days": total_days}


def milestone_rollup(milestone: Milestone, tasks_by_id: dict[str, Task]) -> dict:
    ordered = [tasks_by_id[tid] for tid in milestone.task_ids if tid in tasks_by_id]
    counts = Counter(t.status for t in ordered)
    total = len(ordered)
    done = counts.get("done", 0)
    progress = round(sum(t.percent_complete for t in ordered) / total) if total else 0
    upcoming = sorted(
        [t for t in ordered if t.status != "done" and parse_date(t.due_date)],
        key=lambda t: parse_date(t.due_date) or date.max,
    )
    next_due = upcoming[0].due_date if upcoming else None
    missing = [tid for tid in milestone.task_ids if tid not in tasks_by_id]
    return {
        "milestone": milestone,
        "tasks": ordered,
        "missing": missing,
        "counts": counts,
        "total": total,
        "done": done,
        "working": counts.get("working", 0),
        "blocked": counts.get("blocked", 0),
        "backlog": counts.get("backlog", 0),
        "progress": progress,
        "next_due": next_due,
    }


def tags_summary(tasks: list[Task]) -> dict[str, int]:
    counts: defaultdict[str, int] = defaultdict(int)
    for task in tasks:
        for tag in task.tags:
            counts[tag] += 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def filter_tasks(
    tasks: list[Task],
    projects: list[str] | None = None,
    date_from: str = "",
    date_to: str = "",
    q: str = "",
) -> list[Task]:
    result = list(tasks)
    projects = [p for p in (projects or []) if p]
    if projects:
        result = [t for t in result if (t.project or "") in projects]

    needle = (q or "").strip().lower()
    if needle:
        def matches(task: Task) -> bool:
            haystack = " ".join(
                [
                    task.id,
                    task.title,
                    task.project,
                    task.summary,
                    task.description,
                    " ".join(task.tags),
                ]
            ).lower()
            return needle in haystack

        result = [t for t in result if matches(t)]

    start = parse_date(date_from)
    end = parse_date(date_to)
    if start or end:
        def in_range(task: Task) -> bool:
            dates = [
                d
                for d in (
                    parse_date(task.start_date),
                    parse_date(task.due_date),
                    parse_date(task.completed_date),
                )
                if d
            ]
            if not dates:
                return False
            lo, hi = min(dates), max(dates)
            if start and hi < start:
                return False
            if end and lo > end:
                return False
            return True

        result = [t for t in result if in_range(t)]
    return result


def build_calendar(tasks: list[Task], date_from: str = "", date_to: str = "") -> dict:
    events: list[tuple[date, Task]] = []
    for task in tasks:
        anchor = parse_date(task.due_date) or parse_date(task.start_date)
        if anchor:
            events.append((anchor, task))

    start = parse_date(date_from)
    end = parse_date(date_to)
    event_dates = [d for d, _ in events]
    if not start:
        start = min(event_dates) if event_dates else date.today().replace(day=1)
    if not end:
        end = max(event_dates) if event_dates else start
    if end < start:
        start, end = end, start

    grid_start = start - timedelta(days=start.weekday())
    grid_end = end + timedelta(days=(6 - end.weekday()))
    if (grid_end - grid_start).days > MAX_CALENDAR_WEEKS * 7:
        grid_end = grid_start + timedelta(days=MAX_CALENDAR_WEEKS * 7 - 1)

    by_day: defaultdict[date, list[Task]] = defaultdict(list)
    for d, task in events:
        by_day[d].append(task)

    today = date.today()
    weeks = []
    cursor = grid_start
    while cursor <= grid_end:
        week = []
        for _ in range(7):
            week.append(
                {
                    "date": cursor,
                    "in_range": start <= cursor <= end,
                    "today": cursor == today,
                    "tasks": by_day.get(cursor, []),
                }
            )
            cursor += timedelta(days=1)
        weeks.append(week)

    return {
        "weeks": weeks,
        "weekdays": WEEKDAYS,
        "start": start,
        "end": end,
        "has_events": bool(events),
    }
