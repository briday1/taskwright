from __future__ import annotations

import csv
import difflib
import html as html_lib
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import markdown as markdown_lib
from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .models import ChecklistItem, Milestone, Note, Project, Task, TaskActivityEvent
from .render import (
    SORTS,
    STATUSES,
    build_calendar,
    dashboard_model,
    filter_tasks,
    hide_stale_closed_tasks,
    milestone_rollup,
    sort_tasks,
    tasks_to_jsonantt,
)
from .task_store import (
    add_milestone_attachment,
    add_milestone_note,
    add_task_activity_image,
    add_task_activity_note,
    add_task_to_milestone,
    available_projects,
    create_milestone,
    create_task,
    delete_milestone,
    delete_project,
    delete_task,
    ensure_workspace,
    git_lfs_init,
    git_lfs_status,
    git_status,
    git_sync,
    load_all_milestones,
    load_all_projects,
    load_all_tasks,
    load_milestone,
    load_project,
    load_task,
    load_workspace_config,
    log_progress_change,
    normalize_task_project_refs,
    project_colors,
    register_project,
    remove_task_from_milestone,
    save_milestone,
    save_project,
    save_task,
    save_workspace_config,
    upsert_project,
)

PACKAGE_DIR = Path(__file__).parent


def markdown_filter(text: str) -> str:
    return markdown_lib.markdown(text or "", extensions=["extra", "sane_lists"])


def build_task_activity_entries(task: Task | None) -> list[dict[str, object]]:
    if task is None:
        return []

    entries: list[dict[str, object]] = []
    for note in task.notes:
        entries.append(
            {
                "kind": "note",
                "created_at": note.created_at,
                "body": note.body,
                "filename": None,
                "path": None,
                "is_image": False,
                "progress_before": None,
                "progress_after": None,
            }
        )
    for attachment in task.attachments:
        entries.append(
            {
                "kind": "image" if attachment.kind == "image" else "file",
                "created_at": attachment.uploaded_at,
                "body": attachment.description,
                "filename": attachment.filename,
                "path": attachment.path,
                "is_image": attachment.kind == "image",
                "progress_before": None,
                "progress_after": None,
            }
        )
    for event in task.activity:
        if event.event_type == "progress_update":
            entries.append(
                {
                    "kind": "progress_update",
                    "created_at": event.created_at,
                    "body": None,
                    "filename": None,
                    "path": None,
                    "is_image": False,
                    "progress_before": event.progress_before,
                    "progress_after": event.progress_after,
                }
            )
        elif event.event_type == "image":
            image_name = event.image_filename or event.image_path or ""
            is_image = Path(image_name).suffix.lower() in {
                ".png",
                ".jpg",
                ".jpeg",
                ".gif",
                ".webp",
                ".bmp",
                ".svg",
            }
            entries.append(
                {
                    "kind": "image" if is_image else "file",
                    "created_at": event.created_at,
                    "body": event.note_text,
                    "filename": event.image_filename,
                    "path": event.image_path,
                    "is_image": is_image,
                    "progress_before": None,
                    "progress_after": None,
                }
            )
        else:
            entries.append(
                {
                    "kind": "note",
                    "created_at": event.created_at,
                    "body": event.note_text,
                    "filename": None,
                    "path": None,
                    "is_image": False,
                    "progress_before": None,
                    "progress_after": None,
                }
            )

    return sorted(entries, key=lambda item: str(item.get("created_at") or ""), reverse=True)


def _parse_event_datetime(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_event_points(points: list[dict[str, object]], fallback_iso: str) -> list[dict[str, object]]:
    ordered: list[tuple[datetime, int, dict[str, object]]] = []
    fallback_dt = _parse_event_datetime(fallback_iso) or datetime.now()
    for index, point in enumerate(points):
        dt = _parse_event_datetime(str(point.get("created_at") or "")) or fallback_dt
        ordered.append((dt, index, point))

    ordered.sort(key=lambda item: (item[0], item[1]))
    normalized: list[dict[str, object]] = []
    last_dt: datetime | None = None
    for dt, _, point in ordered:
        if last_dt is not None and dt <= last_dt:
            dt = last_dt + timedelta(seconds=1)
        last_dt = dt
        normalized.append(
            {
                "x": dt.isoformat(timespec="seconds"),
                "y": point.get("y", 100),
                "label": point.get("label", ""),
                "event_type": point.get("event_type", "update"),
                "preview_title": point.get("preview_title", ""),
                "preview_body": point.get("preview_body", ""),
                "preview_path": point.get("preview_path", ""),
                "is_image": bool(point.get("is_image")),
            }
        )
    return normalized


def _clip_progress(value: int | None, fallback: int = 0) -> int:
    try:
        raw = int(value if value is not None else fallback)
    except (TypeError, ValueError):
        raw = fallback
    return max(0, min(100, raw))


def _summarize_text(value: str | None, max_len: int = 46) -> str:
    text = " ".join((value or "").strip().split())
    if not text:
        return ""
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _preview_text(value: str | None, max_len: int = 180) -> str:
    return _summarize_text(value, max_len)


def create_app(workspace: str | Path = ".") -> FastAPI:
    workspace = Path(workspace).resolve()
    ensure_workspace(workspace)
    initial_config = load_workspace_config(workspace)
    app_name = initial_config["app_name"]

    app = FastAPI(title=app_name)
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    templates.env.filters["markdown"] = markdown_filter

    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
    app.mount("/assets", StaticFiles(directory=str(workspace / "assets")), name="assets")
    app.mount("/task-files", StaticFiles(directory=str(workspace / "tasks")), name="task-files")

    VIEWS = {"list", "board", "gantt", "calendar", "projects", "milestones"}
    STALE_CLOSED_DAYS = 30

    def parse_toggle(value: str | None) -> bool:
        return (value or "").strip().lower() in {"1", "true", "yes", "on"}

    def parse_stale_days(value: str | int | None) -> int:
        try:
            days = int(str(value).strip())
        except (TypeError, ValueError):
            days = STALE_CLOSED_DAYS
        return max(1, days)

    def parse_calendar_month(value: str | int | None) -> int | None:
        try:
            month = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return month if 1 <= month <= 12 else None

    def ui_config() -> dict[str, str]:
        config = load_workspace_config(workspace)
        return {
            "app_name": config["app_name"],
            "workspace_name": config["workspace_name"],
            "workspace_description": config["workspace_description"],
            "export_title": config["export_title"],
        }

    def ai_config() -> dict[str, str]:
        return {
            "ai_enabled": "1",
            "ai_base_url": "",
            "ai_api_key": "",
            "ai_model": "",
            "ai_chat_path": "",
            "ai_models_path": "",
            "ai_timeout_seconds": "30",
            "ai_max_tokens": "2048",
            "ai_temperature": "0.7",
        }

    def _ai_config_from_query(request: Request) -> dict[str, str]:
        """Build AI config using persisted values with query-string overrides."""
        cfg = ai_config()
        qp = request.query_params
        for key in {
            "ai_base_url",
            "ai_api_key",
            "ai_model",
            "ai_chat_path",
            "ai_models_path",
            "ai_timeout_seconds",
            "ai_max_tokens",
            "ai_temperature",
        }:
            if key in qp:
                cfg[key] = (qp.get(key) or "").strip()
        if "ai_enabled" in qp:
            cfg["ai_enabled"] = "1" if parse_toggle(qp.get("ai_enabled")) else "0"
        return cfg

    def _ai_endpoint_url(cfg: dict[str, str], override_key: str, default_suffix: str) -> str:
        """Resolve an endpoint URL with sane defaults for OpenAI-compatible APIs."""
        base_url = (cfg.get("ai_base_url") or "").strip().rstrip("/")
        if not base_url:
            return ""

        override = (cfg.get(override_key) or "").strip()
        if override:
            if override.startswith(("http://", "https://")):
                return override.rstrip("/")
            if not override.startswith("/"):
                override = "/" + override
            return base_url + override

        # If base already points to /v1, don't append /v1 again.
        base_path = (urllib.parse.urlparse(base_url).path or "").rstrip("/").lower()
        if base_path.endswith("/v1"):
            return base_url + default_suffix
        return base_url + "/v1" + default_suffix

    def _ai_error_summary(exc: Exception) -> str:
        if isinstance(exc, urllib.error.HTTPError):
            body = exc.read().decode("utf-8", errors="replace")
            body = " ".join(body.split())[:220]
            if body:
                return f"HTTP {exc.code} {exc.reason}: {body}"
            return f"HTTP {exc.code} {exc.reason}"
        if isinstance(exc, urllib.error.URLError):
            return f"Connection error: {exc.reason}"
        return "Unexpected error while contacting AI endpoint"

    def _ai_call(
        messages: list[dict[str, str]],
        cfg: dict[str, str],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict:
        """Call the configured OpenAI-compatible chat completions endpoint."""
        chat_url = _ai_endpoint_url(cfg, "ai_chat_path", "/chat/completions")
        api_key = cfg["ai_api_key"]
        model = cfg["ai_model"]
        if not chat_url:
            raise ValueError("No chat endpoint configured")
        timeout = max(5, min(120, int(cfg.get("ai_timeout_seconds") or "30")))
        if max_tokens is None:
            max_tokens = max(1, int(cfg.get("ai_max_tokens") or "2048"))
        if temperature is None:
            try:
                temperature = float(cfg.get("ai_temperature") or "0.7")
            except ValueError:
                temperature = 0.7

        payload = json.dumps({
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }).encode("utf-8")

        req = urllib.request.Request(
            chat_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _ai_fetch_models(cfg: dict[str, str]) -> list[str]:
        """Fetch available models from the configured endpoint."""
        models_url = _ai_endpoint_url(cfg, "ai_models_path", "/models")
        api_key = cfg["ai_api_key"]
        if not models_url:
            raise ValueError("No models endpoint configured")
        timeout = max(5, min(30, int(cfg.get("ai_timeout_seconds") or "30")))
        req = urllib.request.Request(
            models_url,
            headers={"Authorization": "Bearer " + api_key},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return sorted(m["id"] for m in data.get("data", []) if m.get("id"))

    def _parse_ai_suggestions(text: str) -> dict:
        """Try to parse structured suggestions from the AI response text.

        Looks for a JSON block fenced with ```json ... ``` or a bare JSON object.
        On parse failure, returns an empty suggestions dict so callers can degrade
        gracefully to plain text rendering.
        """
        suggestions: dict = {}
        # Try fenced code block first
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence_match:
            try:
                suggestions = json.loads(fence_match.group(1))
            except (json.JSONDecodeError, ValueError):
                pass
        # Fall back: try last bare JSON object in text
        if not suggestions:
            for match in re.finditer(r"\{[^{}]+\}", text, re.DOTALL):
                try:
                    parsed = json.loads(match.group())
                    if any(k in parsed for k in ("suggested_tasks", "suggested_checklist_items", "suggested_note", "suggested_file_edits")):
                        suggestions = parsed
                except (json.JSONDecodeError, ValueError):
                    continue
        return suggestions

    def _parse_json_object_from_text(text: str) -> dict:
        """Extract a JSON object from text (fenced block preferred)."""
        if not text:
            return {}
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence_match:
            try:
                parsed = json.loads(fence_match.group(1))
                return parsed if isinstance(parsed, dict) else {}
            except (json.JSONDecodeError, ValueError):
                pass
        for match in re.finditer(r"\{.*?\}", text, re.DOTALL):
            try:
                parsed = json.loads(match.group())
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                continue
        return {}

    def _apply_structured_file_edits(
        file_edits: list[dict],
        *,
        dry_run: bool = False,
    ) -> tuple[int, int, list[str], list[tuple[str, str]]]:
        """Apply structured file edits to workspace files.

        Returns (files_changed, edits_applied, detail_messages).
        """
        files_changed = 0
        edits_applied = 0
        details: list[str] = []
        diffs: list[tuple[str, str]] = []

        def _deep_merge(dst: dict, src: dict) -> dict:
            for k, v in src.items():
                if isinstance(v, dict) and isinstance(dst.get(k), dict):
                    _deep_merge(dst[k], v)
                else:
                    dst[k] = v
            return dst

        for spec in file_edits[:30]:
            if not isinstance(spec, dict):
                continue
            raw_path = str(spec.get("path", "")).strip().replace("\\", "/")
            if not raw_path:
                continue
            candidate = (workspace / raw_path).resolve()
            try:
                candidate.relative_to(workspace)
            except ValueError:
                details.append(f"Skipped {raw_path}: path outside workspace")
                continue

            write_content = spec.get("write_content")
            append_text = spec.get("append_text")
            json_merge = spec.get("json_merge")
            create_if_missing = bool(spec.get("create_if_missing"))

            if not candidate.exists() and isinstance(write_content, str):
                candidate.parent.mkdir(parents=True, exist_ok=True)
                if not dry_run:
                    candidate.write_text(write_content, encoding="utf-8")
                files_changed += 1
                edits_applied += 1
                details.append(f"Created {raw_path} (write_content)")
                diff_text = "\n".join(
                    difflib.unified_diff(
                        [],
                        write_content.splitlines(),
                        fromfile=f"{raw_path} (new)",
                        tofile=f"{raw_path} (new)",
                        lineterm="",
                    )
                )
                diffs.append((raw_path, diff_text[:12000]))
                continue
            if not candidate.exists() and create_if_missing:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_text("", encoding="utf-8")
            if not candidate.exists() or not candidate.is_file():
                details.append(f"Skipped {raw_path}: file not found")
                continue

            try:
                original = candidate.read_text(encoding="utf-8")
            except Exception:
                details.append(f"Skipped {raw_path}: not a UTF-8 text file")
                continue

            # High-confidence direct content write.
            if isinstance(write_content, str):
                if write_content != original:
                    if not dry_run:
                        candidate.write_text(write_content, encoding="utf-8")
                    files_changed += 1
                    edits_applied += 1
                    details.append(f"Updated {raw_path} (write_content)")
                    diff_text = "\n".join(
                        difflib.unified_diff(
                            original.splitlines(),
                            write_content.splitlines(),
                            fromfile=f"{raw_path} (before)",
                            tofile=f"{raw_path} (after)",
                            lineterm="",
                        )
                    )
                    diffs.append((raw_path, diff_text[:12000]))
                else:
                    details.append(f"{raw_path}: unchanged (write_content matched existing)")
                continue

            updated = original
            applied_here = 0

            # Optional append support.
            if isinstance(append_text, str) and append_text:
                updated += append_text
                applied_here += 1
                edits_applied += 1

            # Optional JSON merge support.
            if isinstance(json_merge, dict):
                try:
                    parsed = json.loads(updated or "{}")
                    if isinstance(parsed, dict):
                        merged = _deep_merge(parsed, json_merge)
                        updated = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
                        applied_here += 1
                        edits_applied += 1
                    else:
                        details.append(f"{raw_path}: skipped json_merge (file root is not object)")
                except (json.JSONDecodeError, ValueError):
                    details.append(f"{raw_path}: skipped json_merge (file is not valid JSON object)")

            edits = spec.get("edits")
            if isinstance(edits, dict):
                edits = [edits]
            if not isinstance(edits, list):
                # Backward-compatible shorthand: top-level find/replace
                if "find" in spec and "replace" in spec:
                    edits = [{"find": spec.get("find"), "replace": spec.get("replace")}]
                else:
                    edits = []

            for edit in edits:
                if not isinstance(edit, dict):
                    continue
                find = edit.get("find")
                replace = edit.get("replace")
                if not isinstance(find, str) or not isinstance(replace, str) or not find:
                    continue
                count = updated.count(find)
                if count == 1:
                    updated = updated.replace(find, replace, 1)
                    applied_here += 1
                    edits_applied += 1
                elif count == 0:
                    details.append(f"{raw_path}: skipped one edit (find text not found)")
                else:
                    details.append(f"{raw_path}: skipped one edit (find text matched {count} locations)")

            if updated != original:
                if not dry_run:
                    candidate.write_text(updated, encoding="utf-8")
                files_changed += 1
                details.append(f"Updated {raw_path} ({applied_here} edit{'s' if applied_here != 1 else ''})")
                diff_text = "\n".join(
                    difflib.unified_diff(
                        original.splitlines(),
                        updated.splitlines(),
                        fromfile=f"{raw_path} (before)",
                        tofile=f"{raw_path} (after)",
                        lineterm="",
                    )
                )
                diffs.append((raw_path, diff_text[:12000]))
            elif applied_here == 0:
                details.append(f"{raw_path}: no changes applied")

        return files_changed, edits_applied, details, diffs

    def _extract_checklist_items(text: str, entity_title: str = "") -> list[str]:
        """Fallback parser for checklist-like bullets when JSON is omitted."""
        items: list[str] = []
        title_norm = (entity_title or "").strip().lower()

        blocked_exact = {
            "updated task json",
            "key takeaway",
            "suggested_questions",
            "critical",
            "high",
            "normal",
            "low",
            "pending",
            "done",
            "in progress",
            "to do",
            "blocked",
            "working",
            "backlog",
        }
        blocked_substrings = (
            "would you like",
            "key takeaway",
            "your checklist now has",
            "currently at",
            "replace these placeholder",
            "completion",
            "what to provide",
            "please let me know how you'd like to proceed",
            "i need a few more details",
            "which items should be marked",
        )

        def _looks_like_checklist_item(candidate: str) -> bool:
            lowered = candidate.strip().lower()
            if not lowered:
                return False
            if title_norm and lowered == title_norm:
                return False
            if lowered in blocked_exact:
                return False
            if any(part in lowered for part in blocked_substrings):
                return False
            if re.search(r"\b\d+%", lowered):
                return False
            if len(candidate) > 90:
                return False
            words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'/-]*", candidate)
            if not words or len(words) > 8:
                return False
            if candidate.endswith(":"):
                return False
            return True

        def _clean_candidate(value: str) -> str:
            candidate = (value or "").strip()
            candidate = candidate.strip('"\'`“”‘’.,;:()[]{}')
            candidate = re.sub(r"\s+", " ", candidate)
            candidate = re.sub(r"`([^`]+)`", r"\1", candidate)
            candidate = re.sub(r"\*\*([^*]+)\*\*", r"\1", candidate)
            return candidate[:180].strip()

        def _push(value: str) -> None:
            candidate = _clean_candidate(value)
            if len(candidate) < 2:
                return
            if candidate.endswith("?"):
                return
            if candidate.lower().startswith("would you like"):
                return
            if not _looks_like_checklist_item(candidate):
                return
            if candidate not in items:
                items.append(candidate)

        def _extract_numbered_candidates(blob: str) -> None:
            # Handles compact formats like "**1. Item Name**Description..." and plain "1. Item".
            for match in re.finditer(r"\*\*\s*(?:item\s*)?(\d{1,2})[.)]\s*([^*]{2,140}?)\s*\*\*", blob or "", re.IGNORECASE):
                _push(match.group(2))
                if len(items) >= 12:
                    return
            if len(items) >= 12:
                return
            # Handles rows like "**1**Do the thing...**Focus Area**" from compact markdown tables.
            for match in re.finditer(
                r"\*\*\s*(\d{1,2})\s*\*\*\s*([^\n*][^\n]{3,180}?)(?=\*\*|\n|$)",
                blob or "",
                re.IGNORECASE,
            ):
                candidate = re.sub(r"\s+", " ", match.group(2)).strip(" -:;,.\t")
                _push(candidate)
                if len(items) >= 12:
                    return
            if len(items) >= 12:
                return
            for match in re.finditer(r"(?:^|\s)(?:item\s*)?(\d{1,2})[.)]\s+([A-Za-z][^\n|]{2,120})", blob or "", re.IGNORECASE):
                _push(match.group(2))
                if len(items) >= 12:
                    return

        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            bullet = re.match(r"^(?:[-*]|\d+[.)])\s+(.*)$", line)
            checkbox = re.match(r"^\[(?: |x|X)\]\s+(.*)$", line)
            candidate = ""
            if bullet:
                candidate = bullet.group(1).strip()
            elif checkbox:
                candidate = checkbox.group(1).strip()
            if candidate:
                _push(candidate)
            if len(items) >= 12:
                break

        if len(items) < 12:
            _extract_numbered_candidates(text or "")

        def _line_is_likely_checklist_addition(line: str) -> bool:
            lowered = line.lower()
            if "would you like" in lowered or line.endswith("?"):
                return False
            if "checklist" in lowered and any(word in lowered for word in ("added", "adding", "append", "insert", "include", "included")):
                return True
            if any(word in lowered for word in ("added", "adding", "append", "insert", "include", "included")) and ":" in line:
                return True
            return False

        def _extract_highlighted_tokens(line: str) -> None:
            for match in re.finditer(r"\*\*([^*]{1,120})\*\*", line):
                token = match.group(1)
                before = line[max(0, match.start() - 30):match.start()].lower()
                after = line[match.end():match.end() + 30].lower()
                if "task" in after or "for your" in before:
                    continue
                _push(token)
                if len(items) >= 12:
                    return
            for match in re.finditer(r"[\"“]([^\"”]{1,120})[\"”]", line):
                _push(match.group(1))
                if len(items) >= 12:
                    return

        # Parse markdown-style status/task tables used in checklist rewrites.
        status_markers = (
            ":white_check_mark:",
            ":hourglass_flowing_sand:",
            ":white_large_square:",
            "✅",
            "☑",
            "⬜",
            "⏳",
        )
        if len(items) < 12:
            for raw in (text or "").splitlines():
                line = raw.strip()
                if not line:
                    continue
                lowered = line.lower()

                # Pipe table rows, e.g. | status | task | description |
                if "|" in line:
                    parts = [p.strip() for p in line.split("|") if p.strip()]
                    if len(parts) >= 2:
                        joined = " ".join(parts[:3]).lower()
                        if "status" in joined and "task" in joined:
                            continue
                        status_cell = parts[0].lower()
                        if any(m in status_cell for m in status_markers) or status_cell in {"done", "pending", "in progress", "to do"}:
                            _push(parts[1])
                            if len(items) >= 12:
                                break

                # Flattened status row lines with bold task names.
                if any(m in lowered for m in status_markers):
                    _extract_highlighted_tokens(line)
                    if len(items) >= 12:
                        break

        # Parse narrative phrases like "added ...: item a, item b, and item c".
        if len(items) < 12:
            for line in (text or "").splitlines():
                if not _line_is_likely_checklist_addition(line):
                    continue
                token_count_before = len(items)
                _extract_highlighted_tokens(line)
                if len(items) >= 12:
                    break
                if len(items) > token_count_before:
                    continue
                if ":" not in line:
                    continue
                tail = line.split(":", 1)[1]
                tail = re.sub(r"\band\b", ",", tail, flags=re.IGNORECASE)
                for part in tail.split(","):
                    _push(part)
                    if len(items) >= 12:
                        break
                if len(items) >= 12:
                    break

        return items

    def _task_panel_refresh_url(
        task_id: str,
        *,
        f_view: str,
        f_milestone: str,
        f_show_closed: str,
        f_stale_days: str,
    ) -> str:
        url = (
            f"/tasks/{urllib.parse.quote(task_id)}/panel"
            f"?view={urllib.parse.quote(f_view)}"
        )
        if f_milestone:
            url += f"&milestone={urllib.parse.quote(f_milestone)}"
        if parse_toggle(f_show_closed):
            url += "&show_closed=1"
        url += f"&stale_days={parse_stale_days(f_stale_days)}"
        return url

    def _build_task_context(task: Task, all_tasks: list[Task]) -> str:
        task_by_id = {t.id: t for t in all_tasks}
        deps = [task_by_id[d].title for d in task.depends_on if d in task_by_id]
        checklist = [
            f"{'[x]' if item.done else '[ ]'} {item.text}"
            for item in task.checklist
        ]
        notes_preview = [n.body[:200] for n in task.notes[-3:]]
        data = {
            "id": task.id,
            "title": task.title,
            "status": task.status,
            "priority": task.priority,
            "project": task.project or task.project_id or "",
            "summary": task.summary,
            "description": task.description,
            "start_date": task.start_date or "",
            "due_date": task.due_date or "",
            "percent_complete": task.percent_complete,
            "tags": task.tags,
            "depends_on_titles": deps,
            "checklist": checklist,
            "recent_notes": notes_preview,
        }
        return json.dumps(data, indent=2)

    def _build_milestone_context(milestone: Milestone, all_tasks: list[Task]) -> str:
        tasks_by_id = {t.id: t for t in all_tasks}
        milestone_tasks = []
        for tid in milestone.task_ids:
            t = tasks_by_id.get(tid)
            if t:
                milestone_tasks.append({
                    "id": t.id,
                    "title": t.title,
                    "status": t.status,
                    "priority": t.priority,
                    "percent_complete": t.percent_complete,
                    "due_date": t.due_date or "",
                    "project": t.project or "",
                })
        notes_preview = [n.body[:200] for n in milestone.notes[-3:]]
        data = {
            "id": milestone.id,
            "title": milestone.title,
            "status": milestone.status,
            "summary": milestone.summary,
            "description": milestone.description,
            "start_date": milestone.start_date or "",
            "target_date": milestone.target_date or "",
            "task_count": len(milestone.task_ids),
            "tasks": milestone_tasks,
            "recent_notes": notes_preview,
        }
        return json.dumps(data, indent=2)

    def _build_project_context(project: Project, all_tasks: list[Task]) -> str:
        project_tasks = [
            t
            for t in all_tasks
            if (project.id and t.project_id == project.id) or ((not t.project_id) and t.project == project.name)
        ]
        data = {
            "id": project.id,
            "name": project.name,
            "description": project.description,
            "task_count": len(project_tasks),
            "status_counts": {
                "backlog": sum(1 for t in project_tasks if t.status == "backlog"),
                "working": sum(1 for t in project_tasks if t.status == "working"),
                "blocked": sum(1 for t in project_tasks if t.status == "blocked"),
                "done": sum(1 for t in project_tasks if t.status == "done"),
            },
            "tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "status": t.status,
                    "priority": t.priority,
                    "percent_complete": t.percent_complete,
                    "due_date": t.due_date or "",
                }
                for t in project_tasks[:60]
            ],
        }
        return json.dumps(data, indent=2)

    def _resolve_extra_context_blocks(
        extra_context_json: str,
        *,
        current_context_type: str,
        current_entity_id: str,
    ) -> list[tuple[str, str, str]]:
        """Resolve extra context references into (kind, label, json_context)."""
        try:
            refs = json.loads(extra_context_json or "[]")
            if not isinstance(refs, list):
                refs = []
        except (json.JSONDecodeError, ValueError):
            refs = []

        dedupe: set[tuple[str, str]] = set()
        blocks: list[tuple[str, str, str]] = []
        all_tasks = load_all_tasks(workspace)

        for raw in refs[:12]:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind", "")).strip().lower()
            entity_id = str(raw.get("id", "")).strip()
            if kind not in {"task", "milestone", "project"} or not entity_id:
                continue
            if kind == current_context_type and entity_id == current_entity_id:
                continue
            key = (kind, entity_id)
            if key in dedupe:
                continue
            dedupe.add(key)

            try:
                if kind == "task":
                    task = load_task(workspace, entity_id)
                    blocks.append(("task", task.title, _build_task_context(task, all_tasks)))
                elif kind == "milestone":
                    milestone = load_milestone(workspace, entity_id)
                    blocks.append(("milestone", milestone.title, _build_milestone_context(milestone, all_tasks)))
                elif kind == "project":
                    project = load_project(workspace, entity_id)
                    blocks.append(("project", project.name, _build_project_context(project, all_tasks)))
            except Exception:
                continue
        return blocks

    _MAX_TASK_SUMMARY_LENGTH = 500

    SYSTEM_PROMPT = """\
You are Taskunity AI, an assistant embedded in the Taskunity app.

Rules:
- Use only the provided context JSON for the current task or milestone.
- Do not invent task IDs, checklist items, dates, dependencies, or status values.
- Be concrete and app-aware: reference existing task fields and current checklist items.

Taskunity actions available from your response:
1) Create tasks from `suggested_tasks` (for milestone planning).
2) Append checklist items from `suggested_checklist_items` (for task context).
3) Save a note from `suggested_note` (task or milestone context).
4) Propose file edits via `suggested_file_edits` for this utility layer to apply.

When the user asks for actionable updates, ALWAYS include a JSON block with any relevant fields:

```json
{
    "suggested_tasks": [
        {"title": "...", "summary": "...", "priority": "low|normal|high|critical"}
    ],
    "suggested_checklist_items": ["item 1", "item 2"],
        "suggested_note": "...",
        "suggested_file_edits": [
            {
                "path": "relative/path/file.txt",
                "create_if_missing": false,
                "write_content": "optional full file content",
                "append_text": "optional text to append",
                "json_merge": {"optional": "json object to deep-merge"},
                "edits": [
                    {"find": "exact old text", "replace": "new text"}
                ]
            }
        ]
}
```

For `suggested_file_edits`:
- Use workspace-relative paths.
- Prefer `edits` with exact `find`/`replace` when confident.
- Use `write_content` when replacing whole file.
- Use `json_merge` for JSON config-like updates.
- Never propose paths outside the workspace.

Guidance:
- In task context, prioritize `suggested_checklist_items` and `suggested_note`.
- In milestone context, prioritize `suggested_tasks` and optionally `suggested_note`.
- Keep prose concise, then provide machine-usable JSON.
- If information is missing, state assumptions briefly and still provide best-effort structured output."""

    DEFAULT_TASKUNITY_ASSISTANT_SPEC = """\
Taskunity Runtime Spec (authoritative for assistant behavior)

App purpose:
- Manage projects, milestones, and tasks persisted as JSON files.
- Help users plan and execute work by proposing actionable updates.

Entity context rules:
- task context: prioritize checklist updates and task notes.
- milestone context: prioritize task decomposition into suggested tasks.
- project context: summarize cross-task themes and planning guidance only.

Checklist behavior contract:
- If user asks to create/rewrite/update checklist, produce specific, actionable checklist items.
- For deictic follow-ups ("use those", "put them in"), resolve references from prior assistant proposals in the same conversation.
- Avoid clarification loops when prior checklist proposals are explicit and user intent is apply/update.
- Exclude option labels, questions, and prompt boilerplate from checklist items.

Structured output contract:
- Always include JSON when user asks to apply/update/create actionable changes.
- Valid keys:
  - suggested_tasks: list[{title, summary, priority}]
  - suggested_checklist_items: list[str]
  - suggested_note: str
  - suggested_file_edits: list[file edit specs]

Checklist mode guidance:
- If user intent says rewrite/replace/new checklist, set checklist_mode to "replace".
- Otherwise default to "add".

Quality bar:
- Items must be implementation-relevant, not generic placeholders.
- Use concise verb-first wording where possible.
- Do not repeat task title as an item.
"""

    def _load_taskunity_assistant_spec() -> str:
        spec_path = workspace / "docs" / "ai-assistant-spec.md"
        try:
            if spec_path.is_file():
                text = spec_path.read_text(encoding="utf-8").strip()
                if text:
                    return text[:16000]
        except Exception:
            pass
        return DEFAULT_TASKUNITY_ASSISTANT_SPEC

    TASKUNITY_ASSISTANT_SPEC = _load_taskunity_assistant_spec()

    DEFAULT_TASKUNITY_INTENT_SPEC = """\
Taskunity Intent Contract

Return strict JSON only:
{
    "intent": {
        "kind": "update_checklist|create_tasks|save_note|file_edit|clarify|advice",
        "confidence": 0.0,
        "mode": "add|replace"
    },
    "resolved_checklist_items": ["..."],
    "resolved_tasks": [
        {"title": "...", "summary": "...", "priority": "low|normal|high|critical"}
    ],
    "resolved_note": "",
    "needs_clarification": false,
    "clarification_question": ""
}

Rules:
- Resolve deictic follow-ups (those/them/that/it/yes/do it) from recent conversation.
- Prefer prior complete proposals over clarification/option prompts.
- Exclude option labels and questions from checklist items.
- In task context, checklist updates should emit resolved_checklist_items.
"""

    def _load_taskunity_intent_spec() -> str:
        spec_path = workspace / "docs" / "ai-intent-spec.md"
        try:
            if spec_path.is_file():
                text = spec_path.read_text(encoding="utf-8").strip()
                if text:
                    return text[:16000]
        except Exception:
            pass
        return DEFAULT_TASKUNITY_INTENT_SPEC

    TASKUNITY_INTENT_SPEC = _load_taskunity_intent_spec()


    def parse_calendar_year(value: str | int | None) -> int | None:
        try:
            year = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return year if 1900 <= year <= 3000 else None

    def _ai_error_html(message: str) -> str:
        escaped = (
            message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        return f'<div class="ai-msg ai-msg-error"><strong>Error:</strong> {escaped}</div>'

    def build_query(
        projects: list[str], date_from: str, date_to: str, q: str, view: str = "", sort: str = "",
        milestone: str = "", show_closed: bool = False, stale_days: int = STALE_CLOSED_DAYS,
        calendar_month: int | None = None, calendar_year: int | None = None, hide_done: bool = False,
        hide_old: bool | None = None, sort_dir: str = "",
    ) -> str:
        params: list[tuple[str, str]] = [("project", p) for p in projects if p]
        if date_from:
            params.append(("date_from", date_from))
        if date_to:
            params.append(("date_to", date_to))
        if q:
            params.append(("q", q))
        if milestone:
            params.append(("milestone", milestone))
        if sort and sort != "priority":
            params.append(("sort", sort))
            if sort_dir in {"asc", "desc"}:
                params.append(("sort_dir", sort_dir))
        if hide_old is None:
            hide_old = not show_closed
        if hide_old:
            params.append(("hide_old", "1"))
        elif show_closed:
            params.append(("show_closed", "1"))
        if hide_done:
            params.append(("hide_done", "1"))
        if stale_days != STALE_CLOSED_DAYS:
            params.append(("stale_days", str(stale_days)))
        if calendar_month is not None:
            params.append(("calendar_month", str(calendar_month)))
        if calendar_year is not None:
            params.append(("calendar_year", str(calendar_year)))
        if view:
            params.append(("view", view))
        return urllib.parse.urlencode(params)

    def context(
        request: Request,
        selected_task: Task | None = None,
        *,
        projects: list[str] | None = None,
        date_from: str = "",
        date_to: str = "",
        q: str = "",
        sort: str = "priority",
        sort_dir: str = "",
        view: str = "list",
        milestone: str = "",
        show_closed: bool = False,
        hide_old: bool | None = None,
        hide_done: bool = False,
        stale_days: int = STALE_CLOSED_DAYS,
        calendar_month: int | None = None,
        calendar_year: int | None = None,
        git_message: str = "",
        git_message_level: str = "",
    ) -> dict:
        projects = [p for p in (projects or []) if p]
        q = (q or "").strip()
        sort = sort if sort in SORTS else "priority"
        query_params = request.query_params
        default_sort_dirs = {
            "priority": "asc",
            "due_date": "asc",
            "title": "asc",
            "status": "asc",
            "percent_complete": "desc",
            "project": "asc",
        }
        sort_dir = (sort_dir or query_params.get("sort_dir") or "").strip().lower()
        if sort_dir not in {"asc", "desc"}:
            sort_dir = default_sort_dirs.get(sort, "asc")
        view = view if view in VIEWS else "list"
        if hide_old is None:
            if query_params.get("hide_old") is not None:
                hide_old = parse_toggle(query_params.get("hide_old"))
            else:
                hide_old = not show_closed
        show_closed = not bool(hide_old)
        hide_done = hide_done or parse_toggle(query_params.get("hide_done"))
        today = date.today()
        focus_month = parse_calendar_month(calendar_month or query_params.get("calendar_month")) or today.month
        focus_year = parse_calendar_year(calendar_year or query_params.get("calendar_year")) or today.year
        prev_year = focus_year - 1 if focus_month == 1 else focus_year
        prev_month = 12 if focus_month == 1 else focus_month - 1
        next_year = focus_year + 1 if focus_month == 12 else focus_year
        next_month = 1 if focus_month == 12 else focus_month + 1
        year_prev_month = focus_month
        year_prev_year = focus_year - 1
        year_next_month = focus_month
        year_next_year = focus_year + 1
        config = ui_config()
        normalize_task_project_refs(workspace)
        all_projects = load_all_projects(workspace)
        project_name_by_id = {p.id: p.name for p in all_projects if p.id}
        project_by_name = {p.name: p for p in all_projects}
        resolved_projects: list[str] = []
        for p in projects:
            if p in project_name_by_id:
                resolved_projects.append(p)
                continue
            legacy = project_by_name.get(p)
            if legacy and legacy.id:
                resolved_projects.append(legacy.id)
                continue
            resolved_projects.append(p)
        projects = resolved_projects

        all_tasks = load_all_tasks(workspace)
        for task in all_tasks:
            if task.project_id and task.project_id in project_name_by_id:
                task.project = project_name_by_id[task.project_id]
        if selected_task is not None and selected_task.project_id and selected_task.project_id in project_name_by_id:
            selected_task.project = project_name_by_id[selected_task.project_id]
        milestones = load_all_milestones(workspace)
        tasks_by_id = {t.id: t for t in all_tasks}
        panel_task_id = (selected_task.id if selected_task else (request.query_params.get("panel_task") or "").strip())
        if selected_task is None and panel_task_id:
            selected_task = tasks_by_id.get(panel_task_id)
            if selected_task is None:
                panel_task_id = ""
        milestone_rollups = {m.id: milestone_rollup(m, tasks_by_id) for m in milestones}

        selected_milestone = None
        rollup = None
        candidate_tasks = all_tasks
        milestone = (milestone or "").strip()
        if milestone:
            selected_milestone = next((m for m in milestones if m.id == milestone), None)
            if selected_milestone is not None:
                rollup = milestone_rollup(selected_milestone, tasks_by_id)
                allowed = set(selected_milestone.task_ids)
                candidate_tasks = [t for t in all_tasks if t.id in allowed]
            else:
                milestone = ""

        filtered = sort_tasks(filter_tasks(candidate_tasks, projects, date_from, date_to, q), sort, sort_dir)
        if hide_done:
            filtered = [t for t in filtered if t.status != "done"]
        hidden_closed_count = 0
        if hide_old:
            filtered, hidden_closed_count = hide_stale_closed_tasks(filtered, stale_days)
        colors = project_colors(all_projects, all_tasks)
        project_rollups: dict[str, dict[str, int]] = {}
        for project in all_projects:
            rows = [
                t
                for t in all_tasks
                if (project.id and t.project_id == project.id) or ((not t.project_id) and t.project == project.name)
            ]
            total = len(rows)
            done = sum(1 for t in rows if t.status == "done")
            working = sum(1 for t in rows if t.status == "working")
            progress = round(sum(t.percent_complete for t in rows) / total) if total else 0
            project_rollups[project.id] = {
                "total": total,
                "done": done,
                "working": working,
                "progress": progress,
            }

        pills = []
        if selected_milestone is not None:
            pills.append(
                {
                    "label": f"Milestone: {selected_milestone.title}",
                    "color": "",
                    "remove": build_query(projects, date_from, date_to, q, view, sort, show_closed=show_closed, stale_days=stale_days, hide_done=hide_done, hide_old=hide_old, sort_dir=sort_dir),
                }
            )
        for p in projects:
            project_label = project_name_by_id.get(p, p)
            others = [x for x in projects if x != p]
            pills.append(
                {
                    "label": f"Project: {project_label}",
                    "color": colors.get(p, ""),
                    "remove": build_query(others, date_from, date_to, q, view, sort, milestone, show_closed, stale_days, hide_done=hide_done, hide_old=hide_old, sort_dir=sort_dir),
                }
            )
        if date_from:
            pills.append(
                {
                    "label": f"From {date_from}",
                    "color": "",
                    "remove": build_query(projects, "", date_to, q, view, sort, milestone, show_closed, stale_days, hide_done=hide_done, hide_old=hide_old, sort_dir=sort_dir),
                }
            )
        if date_to:
            pills.append(
                {
                    "label": f"To {date_to}",
                    "color": "",
                    "remove": build_query(projects, date_from, "", q, view, sort, milestone, show_closed, stale_days, hide_done=hide_done, hide_old=hide_old, sort_dir=sort_dir),
                }
            )
        if q:
            pills.append(
                {
                    "label": f'Search: "{q}"',
                    "color": "",
                    "remove": build_query(projects, date_from, date_to, "", view, sort, milestone, show_closed, stale_days, hide_done=hide_done, hide_old=hide_old, sort_dir=sort_dir),
                }
            )

        if hide_old:
            pills.append(
                {
                    "label": f"Hide old stuff ({stale_days}d+)",
                    "color": "",
                    "remove": build_query(projects, date_from, date_to, q, view, sort, milestone, show_closed=True, stale_days=stale_days, calendar_month=focus_month, calendar_year=focus_year, hide_done=hide_done, hide_old=False, sort_dir=sort_dir),
                }
            )

        if hide_done:
            pills.append(
                {
                    "label": "Hide done",
                    "color": "",
                    "remove": build_query(projects, date_from, date_to, q, view, sort, milestone, show_closed, stale_days, focus_month, focus_year, hide_done=False, hide_old=hide_old, sort_dir=sort_dir),
                }
            )

        return {
            "request": request,
            "app_name": config["app_name"],
            "workspace_name": config["workspace_name"],
            "model": dashboard_model(filtered),
            "statuses": STATUSES,
            "selected_task": selected_task,
            "milestones": milestones,
            "selected_milestone": selected_milestone,
            "rollup": rollup,
            "milestone_rollups": milestone_rollups,
            "workspace": workspace,
            "projects": all_projects,
            "project_rollups": project_rollups,
            "project_colors": colors,
            "project_name_by_id": project_name_by_id,
            "sorts": SORTS,
            "calendar": build_calendar(filtered, date_from, date_to, focus_month, focus_year),
            "git": git_status(workspace),
            "git_lfs": git_lfs_status(workspace),
            "git_message": git_message,
            "git_message_level": git_message_level,
            "task_activity_entries": build_task_activity_entries(selected_task),
            "task_index": [
                {
                    "id": t.id,
                    "title": t.title,
                    "status": t.status,
                    "project": t.project,
                    "due_date": t.due_date or "",
                }
                for t in sort_tasks(all_tasks, "title", "asc")
            ],
            "task_titles": {t.id: t.title for t in all_tasks},
            **ai_config(),
            "filters": {
                "projects": projects,
                "date_from": date_from,
                "date_to": date_to,
                "q": q,
                "sort": sort,
                "sort_dir": sort_dir,
                "view": view,
                "milestone": milestone,
                "stale_days": stale_days,
                "calendar_month": focus_month,
                "calendar_year": focus_year,
                "query": build_query(projects, date_from, date_to, q, "", sort, milestone, show_closed, stale_days, focus_month, focus_year, hide_done, hide_old=hide_old, sort_dir=sort_dir),
                "query_no_sort": build_query(projects, date_from, date_to, q, "", "", milestone, show_closed, stale_days, focus_month, focus_year, hide_done, hide_old=hide_old),
                "calendar_query": build_query(projects, date_from, date_to, q, "calendar", sort, milestone, show_closed, stale_days, focus_month, focus_year, hide_done, hide_old=hide_old, sort_dir=sort_dir),
                "calendar_prev_query": build_query(projects, date_from, date_to, q, "calendar", sort, milestone, show_closed, stale_days, prev_month, prev_year, hide_done, hide_old=hide_old, sort_dir=sort_dir),
                "calendar_next_query": build_query(projects, date_from, date_to, q, "calendar", sort, milestone, show_closed, stale_days, next_month, next_year, hide_done, hide_old=hide_old, sort_dir=sort_dir),
                "calendar_year_prev_query": build_query(projects, date_from, date_to, q, "calendar", sort, milestone, show_closed, stale_days, year_prev_month, year_prev_year, hide_done, hide_old=hide_old, sort_dir=sort_dir),
                "calendar_year_next_query": build_query(projects, date_from, date_to, q, "calendar", sort, milestone, show_closed, stale_days, year_next_month, year_next_year, hide_done, hide_old=hide_old, sort_dir=sort_dir),
                "panel_task": panel_task_id,
                "show_closed": show_closed,
                "hide_old": hide_old,
                "hide_done": hide_done,
                "hidden_closed_count": hidden_closed_count,
                "toggle_closed_query": build_query(projects, date_from, date_to, q, view, sort, milestone, not show_closed, stale_days, focus_month, focus_year, hide_done, hide_old=not hide_old, sort_dir=sort_dir),
                "sort_default_dirs": default_sort_dirs,
                "pills": pills,
            },
        }

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        project: list[str] = Query(default=[]),
        date_from: str = "",
        date_to: str = "",
        q: str = "",
        sort: str = "priority",
        sort_dir: str = "",
        view: str = "list",
        milestone: str = "",
        show_closed: str = "",
        hide_old: str = "",
        hide_done: str = "",
        stale_days: str = "",
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "index.html",
            context(
                request,
                projects=project,
                date_from=date_from,
                date_to=date_to,
                q=q,
                sort=sort,
                sort_dir=sort_dir,
                view=view,
                milestone=milestone,
                show_closed=parse_toggle(show_closed),
                hide_old=parse_toggle(hide_old),
                hide_done=parse_toggle(hide_done),
                stale_days=parse_stale_days(stale_days),
            ),
        )

    @app.get("/partials/main", response_class=HTMLResponse)
    def main_partial(
        request: Request,
        project: list[str] = Query(default=[]),
        date_from: str = "",
        date_to: str = "",
        q: str = "",
        sort: str = "priority",
        sort_dir: str = "",
        view: str = "list",
        milestone: str = "",
        show_closed: str = "",
        hide_old: str = "",
        hide_done: str = "",
        stale_days: str = "",
        panel_task: str = "",
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                projects=project,
                date_from=date_from,
                date_to=date_to,
                q=q,
                sort=sort,
                sort_dir=sort_dir,
                view=view,
                milestone=milestone,
                show_closed=parse_toggle(show_closed),
                hide_old=parse_toggle(hide_old),
                hide_done=parse_toggle(hide_done),
                stale_days=parse_stale_days(stale_days),
            ),
        )

    @app.get("/tasks/{task_id}/panel", response_class=HTMLResponse)
    def task_panel(
        request: Request,
        task_id: str,
        project: list[str] = Query(default=[]),
        date_from: str = "",
        date_to: str = "",
        q: str = "",
        view: str = "list",
        milestone: str = "",
        show_closed: str = "",
        hide_old: str = "",
        hide_done: str = "",
        stale_days: str = "",
    ) -> HTMLResponse:
        task = load_task(workspace, task_id)
        return templates.TemplateResponse(
            request,
            "partials/task_panel.html",
            context(
                request,
                task,
                projects=project,
                date_from=date_from,
                date_to=date_to,
                q=q,
                view=view,
                milestone=milestone,
                show_closed=parse_toggle(show_closed),
                hide_old=parse_toggle(hide_old),
                hide_done=parse_toggle(hide_done),
                stale_days=parse_stale_days(stale_days),
            ),
        )

    @app.post("/tasks/create", response_class=HTMLResponse)
    def create_task_route(
        request: Request,
        title: str = Form("New task"),
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_hide_done: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        task = create_task(workspace, title)
        if f_milestone:
            add_task_to_milestone(workspace, f_milestone, task.id)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                task,
                projects=f_project,
                date_from=f_from,
                date_to=f_to,
                q=f_q,
                view=f_view,
                milestone=f_milestone,
                show_closed=parse_toggle(f_show_closed),
                hide_done=parse_toggle(f_hide_done),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/tasks/{task_id}/save", response_class=HTMLResponse)
    async def save_task_route(
        request: Request,
        task_id: str,
        title: str = Form(...),
        status: str = Form(""),
        priority: str = Form(""),
        project: str = Form(""),
        summary: str = Form(""),
        description: str = Form(""),
        tags: str = Form(""),
        start_date: str = Form(""),
        due_date: str = Form(""),
        completed_date: str = Form(""),
        percent_complete: str = Form(""),
        depends_on: str = Form(""),
        checklist_text: str = Form(""),
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_hide_done: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        task = load_task(workspace, task_id)
        task.title = title
        status_value = (status or "").strip().lower()
        if status_value in set(STATUSES):
            task.status = status_value
        priority_value = (priority or "").strip().lower()
        if priority_value in {"low", "normal", "high", "critical"}:
            task.priority = priority_value
        project_value = (project or "").strip()
        projects_all = load_all_projects(workspace)
        by_id = {p.id: p for p in projects_all if p.id}
        by_name = {p.name: p for p in projects_all}
        selected_project = by_id.get(project_value) or by_name.get(project_value)
        if selected_project is None and project_value:
            selected_project = register_project(workspace, project_value)
        if selected_project is not None:
            task.project_id = selected_project.id
            task.project = selected_project.name
        else:
            task.project_id = ""
            task.project = ""
        task.summary = summary
        task.description = description
        task.tags = [x.strip() for x in tags.split(",") if x.strip()]
        task.start_date = start_date or None
        task.due_date = due_date or None
        task.completed_date = completed_date or None
        percent_raw = str(percent_complete or "").strip()
        if percent_raw:
            try:
                new_progress = max(0, min(int(percent_raw), 100))
            except ValueError:
                new_progress = task.percent_complete
            old_progress = task.percent_complete
            task.percent_complete = new_progress
            log_progress_change(workspace, task, old_progress, task.percent_complete)
        task.depends_on = [x.strip() for x in depends_on.split(",") if x.strip()]
        checklist = []
        for line in checklist_text.splitlines():
            line = line.strip()
            if not line:
                continue
            done = line.startswith("[x]") or line.startswith("[X]")
            text = line[3:].strip() if line[:3].lower() in {"[x]", "[ ]"} else line
            checklist.append(ChecklistItem(text=text, done=done))
        task.checklist = checklist
        save_task(workspace, task)
        if task.project:
            register_project(workspace, task.project)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                task,
                projects=f_project,
                date_from=f_from,
                date_to=f_to,
                q=f_q,
                view=f_view,
                milestone=f_milestone,
                show_closed=parse_toggle(f_show_closed),
                hide_done=parse_toggle(f_hide_done),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/tasks/{task_id}/update", response_class=HTMLResponse)
    async def update_task_activity_route(
        request: Request,
        task_id: str,
        progress_after: str = Form(""),
        status: str = Form(""),
        priority: str = Form(""),
        body: str = Form(""),
        attachment: UploadFile | None = File(None),
        description: str = Form(""),
        save_title: str | None = Form(None),
        save_project: str | None = Form(None),
        save_summary: str | None = Form(None),
        save_task_description: str | None = Form(None),
        save_tags: str | None = Form(None),
        save_start_date: str | None = Form(None),
        save_due_date: str | None = Form(None),
        save_completed_date: str | None = Form(None),
        save_depends_on: str | None = Form(None),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_hide_done: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        task = load_task(workspace, task_id)

        if save_title is not None:
            task.title = save_title
        if save_project is not None:
            project_value = save_project.strip()
            projects_all = load_all_projects(workspace)
            by_id = {p.id: p for p in projects_all if p.id}
            by_name = {p.name: p for p in projects_all}
            selected_project = by_id.get(project_value) or by_name.get(project_value)
            if selected_project is None and project_value:
                selected_project = register_project(workspace, project_value)
            if selected_project is not None:
                task.project_id = selected_project.id
                task.project = selected_project.name
            else:
                task.project_id = ""
                task.project = ""
        if save_summary is not None:
            task.summary = save_summary
        if save_task_description is not None:
            task.description = save_task_description
        if save_tags is not None:
            task.tags = [x.strip() for x in save_tags.split(",") if x.strip()]
        if save_start_date is not None:
            task.start_date = save_start_date or None
        if save_due_date is not None:
            task.due_date = save_due_date or None
        if save_completed_date is not None:
            task.completed_date = save_completed_date or None
        if save_depends_on is not None:
            task.depends_on = [x.strip() for x in save_depends_on.split(",") if x.strip()]

        progress_raw = str(progress_after or "").strip()
        if progress_raw:
            try:
                new_progress = max(0, min(int(progress_raw), 100))
            except ValueError:
                new_progress = task.percent_complete
            old_progress = task.percent_complete
            task.percent_complete = new_progress
            log_progress_change(workspace, task, old_progress, task.percent_complete)

        status_before = task.status
        priority_before = task.priority
        status_value = (status or "").strip().lower()
        if status_value in set(STATUSES):
            task.status = status_value
        priority_value = (priority or "").strip().lower()
        if priority_value in {"low", "normal", "high", "critical"}:
            task.priority = priority_value

        if task.status == "done" and not task.completed_date:
            task.completed_date = date.today().isoformat()
        elif status_before == "done" and task.status != "done":
            task.completed_date = None

        context_parts: list[str] = []
        if status_before != task.status:
            context_parts.append(f"Status {status_before} → {task.status}")
        if priority_before != task.priority:
            context_parts.append(f"Priority {priority_before} → {task.priority}")
        if context_parts:
            task.activity.append(
                TaskActivityEvent(
                    event_type="note",
                    note_text=" · ".join(context_parts),
                )
            )

        note_text = (body or "").strip()
        if note_text:
            task.activity.append(TaskActivityEvent(event_type="note", note_text=note_text))

        save_task(workspace, task)
        register_project(workspace, task.project)

        if attachment and (attachment.filename or "").strip():
            task = add_task_activity_image(
                workspace,
                task_id,
                attachment.filename or "attachment.bin",
                await attachment.read(),
                attachment.content_type,
                description,
            )

        return templates.TemplateResponse(
            request,
            "partials/task_panel.html",
            context(
                request,
                task,
                view=f_view,
                milestone=f_milestone,
                show_closed=parse_toggle(f_show_closed),
                hide_done=parse_toggle(f_hide_done),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/tasks/{task_id}/checklist/add", response_class=HTMLResponse)
    async def checklist_add_route(
        request: Request,
        task_id: str,
        item_text: str = Form(""),
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        task = load_task(workspace, task_id)
        text = item_text.strip()
        if text:
            task.checklist.append(ChecklistItem(text=text, done=False))
            save_task(workspace, task)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                task,
                projects=f_project,
                date_from=f_from,
                date_to=f_to,
                q=f_q,
                view=f_view,
                milestone=f_milestone,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/tasks/{task_id}/checklist/{item_index}/toggle", response_class=HTMLResponse)
    async def checklist_toggle_route(
        request: Request,
        task_id: str,
        item_index: int,
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        task = load_task(workspace, task_id)
        if 0 <= item_index < len(task.checklist):
            task.checklist[item_index].done = not task.checklist[item_index].done
            save_task(workspace, task)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                task,
                projects=f_project,
                date_from=f_from,
                date_to=f_to,
                q=f_q,
                view=f_view,
                milestone=f_milestone,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/tasks/{task_id}/checklist/{item_index}/delete", response_class=HTMLResponse)
    async def checklist_delete_route(
        request: Request,
        task_id: str,
        item_index: int,
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        task = load_task(workspace, task_id)
        if 0 <= item_index < len(task.checklist):
            task.checklist.pop(item_index)
            save_task(workspace, task)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                task,
                projects=f_project,
                date_from=f_from,
                date_to=f_to,
                q=f_q,
                view=f_view,
                milestone=f_milestone,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/tasks/{task_id}/raw", response_class=HTMLResponse)
    async def save_raw_json(
        request: Request,
        task_id: str,
        raw_json: str = Form(...),
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        parsed = json.loads(raw_json)
        task = Task.model_validate(parsed)
        save_task(workspace, task)
        register_project(workspace, task.project)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                task,
                projects=f_project,
                date_from=f_from,
                date_to=f_to,
                q=f_q,
                view=f_view,
                milestone=f_milestone,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/tasks/{task_id}/note", response_class=HTMLResponse)
    async def add_note_route(request: Request, task_id: str, body: str = Form("")) -> HTMLResponse:
        task = add_task_activity_note(workspace, task_id, body)
        return templates.TemplateResponse(request, "partials/task_panel.html", context(request, task))

    @app.post("/tasks/{task_id}/attachment", response_class=HTMLResponse)
    async def upload_attachment_route(
        request: Request,
        task_id: str,
        attachment: UploadFile = File(...),
        description: str = Form(""),
    ) -> HTMLResponse:
        task = add_task_activity_image(
            workspace,
            task_id,
            attachment.filename or "attachment.bin",
            await attachment.read(),
            attachment.content_type,
            description,
        )
        return templates.TemplateResponse(request, "partials/task_panel.html", context(request, task))

    @app.get("/tasks/{task_id}/burndown.json")
    def task_burndown_json(task_id: str) -> Response:
        task = load_task(workspace, task_id)
        fallback_iso = (
            task.extra.get("created_at")
            or task.start_date
            or task.due_date
            or task.completed_date
            or datetime.now().isoformat(timespec="seconds")
        )

        progress_updates = [event for event in task.activity if event.event_type == "progress_update"]
        progress_updates.sort(key=lambda item: item.created_at)
        first_before = progress_updates[0].progress_before if progress_updates else None
        current_progress = _clip_progress(first_before, task.percent_complete)

        raw_events: list[dict[str, object]] = []
        for note in task.notes:
            summary = _summarize_text(note.body)
            label = "Note added"
            if summary:
                label = f"Note: {summary}"
            raw_events.append(
                {
                    "created_at": note.created_at,
                    "event_type": "note",
                    "label": label,
                    "preview_title": "Note",
                    "preview_body": _preview_text(note.body),
                    "preview_path": "",
                    "is_image": False,
                    "progress_before": None,
                    "progress_after": None,
                }
            )
        for attachment in task.attachments:
            filename = (attachment.filename or "Attachment").strip() or "Attachment"
            raw_events.append(
                {
                    "created_at": attachment.uploaded_at,
                    "event_type": "attachment",
                    "label": f"Attachment: {filename}",
                    "preview_title": filename,
                    "preview_body": _preview_text(attachment.description),
                    "preview_path": attachment.path,
                    "is_image": attachment.kind == "image",
                    "progress_before": None,
                    "progress_after": None,
                }
            )
        for event in task.activity:
            if event.event_type == "progress_update":
                before = _clip_progress(event.progress_before, current_progress)
                after = _clip_progress(event.progress_after, before)
                raw_events.append(
                    {
                        "created_at": event.created_at,
                        "event_type": "progress_update",
                        "label": f"Progress {before}% → {after}%",
                        "preview_title": "Progress update",
                        "preview_body": "",
                        "preview_path": "",
                        "is_image": False,
                        "progress_before": before,
                        "progress_after": after,
                    }
                )
            elif event.event_type == "image":
                filename = (event.image_filename or "Attachment").strip() or "Attachment"
                image_name = event.image_filename or event.image_path or ""
                is_image = Path(image_name).suffix.lower() in {
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".gif",
                    ".webp",
                    ".bmp",
                    ".svg",
                }
                raw_events.append(
                    {
                        "created_at": event.created_at,
                        "event_type": "attachment",
                        "label": f"Attachment: {filename}",
                        "preview_title": filename,
                        "preview_body": _preview_text(event.note_text),
                        "preview_path": event.image_path or "",
                        "is_image": is_image,
                        "progress_before": None,
                        "progress_after": None,
                    }
                )
            else:
                summary = _summarize_text(event.note_text)
                label = "Note added"
                if summary:
                    label = f"Note: {summary}"
                raw_events.append(
                    {
                        "created_at": event.created_at,
                        "event_type": "note",
                        "label": label,
                        "preview_title": "Note",
                        "preview_body": _preview_text(event.note_text),
                        "preview_path": "",
                        "is_image": False,
                        "progress_before": None,
                        "progress_after": None,
                    }
                )

        raw_events.sort(
            key=lambda item: (
                _parse_event_datetime(str(item.get("created_at") or "")) or datetime.max,
                str(item.get("event_type") or ""),
            )
        )

        points: list[dict[str, object]] = []
        for event in raw_events:
            if event.get("event_type") == "progress_update":
                current_progress = _clip_progress(
                    event.get("progress_after") if isinstance(event.get("progress_after"), int) else None,
                    current_progress,
                )
            points.append(
                {
                    "created_at": str(event.get("created_at") or fallback_iso),
                    "y": 100 - current_progress,
                    "label": str(event.get("label") or "Update"),
                    "event_type": str(event.get("event_type") or "update"),
                    "preview_title": str(event.get("preview_title") or ""),
                    "preview_body": str(event.get("preview_body") or ""),
                    "preview_path": str(event.get("preview_path") or ""),
                    "is_image": bool(event.get("is_image")),
                }
            )

        if not points:
            points.append(
                {
                    "created_at": fallback_iso,
                    "y": 100 - _clip_progress(task.percent_complete),
                    "label": f"Current progress: {_clip_progress(task.percent_complete)}%",
                    "event_type": "snapshot",
                    "preview_title": "Current snapshot",
                    "preview_body": "",
                    "preview_path": "",
                    "is_image": False,
                }
            )

        normalized_points = _normalize_event_points(points, fallback_iso)
        return Response(
            json.dumps({"task_id": task_id, "title": task.title, "points": normalized_points}),
            media_type="application/json",
        )

    @app.post("/tasks/{task_id}/complete", response_class=HTMLResponse)
    async def complete_task_route(
        request: Request,
        task_id: str,
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        task = load_task(workspace, task_id)
        if task.status == "done":
            task.status = "working"
            task.completed_date = None
        else:
            task.status = "done"
            old_progress = task.percent_complete
            task.percent_complete = 100
            log_progress_change(workspace, task, old_progress, task.percent_complete)
            if not task.completed_date:
                task.completed_date = date.today().isoformat()
        save_task(workspace, task)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                task,
                projects=f_project,
                date_from=f_from,
                date_to=f_to,
                q=f_q,
                view=f_view,
                milestone=f_milestone,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/tasks/{task_id}/delete")
    async def delete_task_route(
        task_id: str,
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> RedirectResponse:
        delete_task(workspace, task_id)
        params: list[tuple[str, str]] = [("project", p) for p in f_project if p]
        if f_from:
            params.append(("date_from", f_from))
        if f_to:
            params.append(("date_to", f_to))
        if f_q:
            params.append(("q", f_q))
        if f_milestone:
            params.append(("milestone", f_milestone))
        if parse_toggle(f_show_closed):
            params.append(("show_closed", "1"))
        if parse_stale_days(f_stale_days) != STALE_CLOSED_DAYS:
            params.append(("stale_days", str(parse_stale_days(f_stale_days))))
        params.append(("view", f_view))
        return RedirectResponse("/?" + urllib.parse.urlencode(params), status_code=303)

    @app.post("/projects", response_class=HTMLResponse)
    def add_project_route(
        request: Request,
        name: str = Form(...),
        description: str = Form(""),
        color: str = Form("#2e6fd8"),
        project_id: str = Form(""),
    ) -> HTMLResponse:
        upsert_project(workspace, name, description.strip(), color, project_id=project_id)
        return templates.TemplateResponse(request, "partials/main.html", context(request, view="projects"))

    # --- Milestones ---------------------------------------------------------

    @app.post("/milestones/create", response_class=HTMLResponse)
    def create_milestone_route(
        request: Request,
        title: str = Form("New milestone"),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        milestone = create_milestone(workspace, title)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                view="list",
                milestone=milestone.id,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.get("/milestones/{milestone_id}/panel", response_class=HTMLResponse)
    def milestone_panel_route(
        request: Request,
        milestone_id: str,
        project: list[str] = Query(default=[]),
        date_from: str = "",
        date_to: str = "",
        q: str = "",
        view: str = "list",
        show_closed: str = "",
        stale_days: str = "",
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "partials/milestone_panel.html",
            context(
                request,
                projects=project,
                date_from=date_from,
                date_to=date_to,
                q=q,
                view=view,
                milestone=milestone_id,
                show_closed=parse_toggle(show_closed),
                stale_days=parse_stale_days(stale_days),
            ),
        )

    @app.get("/milestones/{milestone_id}/burndown.json")
    def milestone_burndown_json(milestone_id: str) -> Response:
        milestone = load_milestone(workspace, milestone_id)
        all_tasks = load_all_tasks(workspace)
        tasks_by_id = {task.id: task for task in all_tasks}
        milestone_tasks = [tasks_by_id[task_id] for task_id in milestone.task_ids if task_id in tasks_by_id]

        fallback_iso = (
            milestone.start_date
            or milestone.target_date
            or milestone.extra.get("created_at")
            or datetime.now().isoformat(timespec="seconds")
        )

        task_progress: dict[str, int] = {}
        for task in milestone_tasks:
            updates = [event for event in task.activity if event.event_type == "progress_update"]
            updates.sort(key=lambda item: item.created_at)
            baseline = updates[0].progress_before if updates else task.percent_complete
            task_progress[task.id] = _clip_progress(baseline, task.percent_complete)

        raw_events: list[dict[str, object]] = []

        for note in milestone.notes:
            summary = _summarize_text(note.body)
            label = "Milestone note"
            if summary:
                label = f"Milestone note: {summary}"
            raw_events.append(
                {
                    "created_at": note.created_at,
                    "event_type": "note",
                    "task_id": None,
                    "label": label,
                    "preview_title": "Milestone note",
                    "preview_body": _preview_text(note.body),
                    "preview_path": "",
                    "is_image": False,
                    "progress_after": None,
                }
            )
        for attachment in milestone.attachments:
            filename = (attachment.filename or "Attachment").strip() or "Attachment"
            raw_events.append(
                {
                    "created_at": attachment.uploaded_at,
                    "event_type": "attachment",
                    "task_id": None,
                    "label": f"Milestone attachment: {filename}",
                    "preview_title": filename,
                    "preview_body": _preview_text(attachment.description),
                    "preview_path": attachment.path,
                    "is_image": attachment.kind == "image",
                    "progress_after": None,
                }
            )

        for task in milestone_tasks:
            prefix = task.title.strip() or task.id
            for note in task.notes:
                summary = _summarize_text(note.body)
                label = f"{prefix}: note"
                if summary:
                    label = f"{prefix}: {summary}"
                raw_events.append(
                    {
                        "created_at": note.created_at,
                        "event_type": "note",
                        "task_id": task.id,
                        "label": label,
                        "preview_title": f"{prefix} note",
                        "preview_body": _preview_text(note.body),
                        "preview_path": "",
                        "is_image": False,
                        "progress_after": None,
                    }
                )
            for attachment in task.attachments:
                filename = (attachment.filename or "Attachment").strip() or "Attachment"
                raw_events.append(
                    {
                        "created_at": attachment.uploaded_at,
                        "event_type": "attachment",
                        "task_id": task.id,
                        "label": f"{prefix}: attachment {filename}",
                        "preview_title": filename,
                        "preview_body": _preview_text(attachment.description),
                        "preview_path": attachment.path,
                        "is_image": attachment.kind == "image",
                        "progress_after": None,
                    }
                )
            for event in task.activity:
                if event.event_type == "progress_update":
                    before = _clip_progress(event.progress_before, task_progress.get(task.id, task.percent_complete))
                    after = _clip_progress(event.progress_after, before)
                    raw_events.append(
                        {
                            "created_at": event.created_at,
                            "event_type": "progress_update",
                            "task_id": task.id,
                            "label": f"{prefix}: {before}% → {after}%",
                            "preview_title": f"{prefix} progress",
                            "preview_body": "",
                            "preview_path": "",
                            "is_image": False,
                            "progress_after": after,
                        }
                    )
                elif event.event_type == "image":
                    filename = (event.image_filename or "Attachment").strip() or "Attachment"
                    image_name = event.image_filename or event.image_path or ""
                    is_image = Path(image_name).suffix.lower() in {
                        ".png",
                        ".jpg",
                        ".jpeg",
                        ".gif",
                        ".webp",
                        ".bmp",
                        ".svg",
                    }
                    raw_events.append(
                        {
                            "created_at": event.created_at,
                            "event_type": "attachment",
                            "task_id": task.id,
                            "label": f"{prefix}: attachment {filename}",
                            "preview_title": filename,
                            "preview_body": _preview_text(event.note_text),
                            "preview_path": event.image_path or "",
                            "is_image": is_image,
                            "progress_after": None,
                        }
                    )
                else:
                    summary = _summarize_text(event.note_text)
                    label = f"{prefix}: note"
                    if summary:
                        label = f"{prefix}: {summary}"
                    raw_events.append(
                        {
                            "created_at": event.created_at,
                            "event_type": "note",
                            "task_id": task.id,
                            "label": label,
                            "preview_title": f"{prefix} note",
                            "preview_body": _preview_text(event.note_text),
                            "preview_path": "",
                            "is_image": False,
                            "progress_after": None,
                        }
                    )

        raw_events.sort(
            key=lambda item: (
                _parse_event_datetime(str(item.get("created_at") or "")) or datetime.max,
                str(item.get("event_type") or ""),
                str(item.get("task_id") or ""),
            )
        )

        def avg_remaining() -> int:
            if not task_progress:
                return 100
            return round(sum(100 - progress for progress in task_progress.values()) / len(task_progress))

        points: list[dict[str, object]] = []
        for event in raw_events:
            if event.get("event_type") == "progress_update":
                task_id = str(event.get("task_id") or "")
                if task_id in task_progress:
                    task_progress[task_id] = _clip_progress(
                        event.get("progress_after") if isinstance(event.get("progress_after"), int) else None,
                        task_progress[task_id],
                    )
            points.append(
                {
                    "created_at": str(event.get("created_at") or fallback_iso),
                    "y": avg_remaining(),
                    "label": str(event.get("label") or "Update"),
                    "event_type": str(event.get("event_type") or "update"),
                    "preview_title": str(event.get("preview_title") or ""),
                    "preview_body": str(event.get("preview_body") or ""),
                    "preview_path": str(event.get("preview_path") or ""),
                    "is_image": bool(event.get("is_image")),
                }
            )

        if not points and milestone_tasks:
            remaining = avg_remaining()
            points.append(
                {
                    "created_at": fallback_iso,
                    "y": remaining,
                    "label": f"Current average remaining: {remaining}%",
                    "event_type": "snapshot",
                    "preview_title": "Current snapshot",
                    "preview_body": "",
                    "preview_path": "",
                    "is_image": False,
                }
            )

        normalized_points = _normalize_event_points(points, fallback_iso)

        return Response(
            json.dumps({"milestone_id": milestone_id, "title": milestone.title, "points": normalized_points}),
            media_type="application/json",
        )

    @app.post("/milestones/{milestone_id}/save", response_class=HTMLResponse)
    def save_milestone_route(
        request: Request,
        milestone_id: str,
        title: str = Form(...),
        status: str = Form("active"),
        color: str = Form("#3567e0"),
        summary: str = Form(""),
        description: str = Form(""),
        start_date: str = Form(""),
        target_date: str = Form(""),
        f_view: str = Form("list"),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        milestone = load_milestone(workspace, milestone_id)
        milestone.title = title
        milestone.status = status if status in {"planned", "active", "done"} else "active"
        milestone.color = (color or "").strip() or "#3567e0"
        milestone.summary = summary
        milestone.description = description
        milestone.start_date = start_date or None
        milestone.target_date = target_date or None
        save_milestone(workspace, milestone)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                view=f_view,
                milestone=milestone.id,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/milestones/{milestone_id}/note", response_class=HTMLResponse)
    def milestone_note_route(
        request: Request,
        milestone_id: str,
        body: str = Form(""),
        save_title: str | None = Form(None),
        save_status: str | None = Form(None),
        save_color: str | None = Form(None),
        save_summary: str | None = Form(None),
        save_description: str | None = Form(None),
        save_projects: list[str] = Form(default=[]),
        save_projects_present: str = Form(""),
        save_start_date: str | None = Form(None),
        save_target_date: str | None = Form(None),
        f_view: str = Form("list"),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        milestone = load_milestone(workspace, milestone_id)
        if save_title is not None:
            milestone.title = save_title
        if save_status is not None:
            milestone.status = save_status if save_status in {"planned", "active", "done"} else milestone.status
        if save_color is not None:
            milestone.color = (save_color or "").strip() or "#3567e0"
        if save_summary is not None:
            milestone.summary = save_summary
        if save_description is not None:
            milestone.description = save_description
        if save_start_date is not None:
            milestone.start_date = save_start_date or None
        if save_target_date is not None:
            milestone.target_date = save_target_date or None
        if parse_toggle(save_projects_present):
            milestone.projects = [p.strip() for p in save_projects if p.strip()]
        save_milestone(workspace, milestone)
        add_milestone_note(workspace, milestone_id, body)
        return templates.TemplateResponse(
            request,
            "partials/milestone_panel.html",
            context(
                request,
                view=f_view,
                milestone=milestone_id,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/milestones/{milestone_id}/attachment", response_class=HTMLResponse)
    async def milestone_attachment_route(
        request: Request,
        milestone_id: str,
        attachment: UploadFile = File(...),
        description: str = Form(""),
        save_title: str | None = Form(None),
        save_status: str | None = Form(None),
        save_color: str | None = Form(None),
        save_summary: str | None = Form(None),
        save_description: str | None = Form(None),
        save_projects: list[str] = Form(default=[]),
        save_projects_present: str = Form(""),
        save_start_date: str | None = Form(None),
        save_target_date: str | None = Form(None),
        f_view: str = Form("list"),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        milestone = load_milestone(workspace, milestone_id)
        if save_title is not None:
            milestone.title = save_title
        if save_status is not None:
            milestone.status = save_status if save_status in {"planned", "active", "done"} else milestone.status
        if save_color is not None:
            milestone.color = (save_color or "").strip() or "#3567e0"
        if save_summary is not None:
            milestone.summary = save_summary
        if save_description is not None:
            milestone.description = save_description
        if save_start_date is not None:
            milestone.start_date = save_start_date or None
        if save_target_date is not None:
            milestone.target_date = save_target_date or None
        if parse_toggle(save_projects_present):
            milestone.projects = [p.strip() for p in save_projects if p.strip()]
        save_milestone(workspace, milestone)
        add_milestone_attachment(
            workspace,
            milestone_id,
            attachment.filename or "attachment.bin",
            await attachment.read(),
            attachment.content_type,
            description,
        )
        return templates.TemplateResponse(
            request,
            "partials/milestone_panel.html",
            context(
                request,
                view=f_view,
                milestone=milestone_id,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/milestones/{milestone_id}/tasks/new", response_class=HTMLResponse)
    def milestone_new_task_route(
        request: Request,
        milestone_id: str,
        title: str = Form("New task"),
        f_view: str = Form("list"),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        task = create_task(workspace, title)
        add_task_to_milestone(workspace, milestone_id, task.id)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                task,
                view=f_view,
                milestone=milestone_id,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/milestones/{milestone_id}/tasks/{task_id}/add", response_class=HTMLResponse)
    def milestone_add_task_route(
        request: Request,
        milestone_id: str,
        task_id: str,
        f_view: str = "list",
        f_show_closed: str = "",
        f_stale_days: str = "",
    ) -> HTMLResponse:
        add_task_to_milestone(workspace, milestone_id, task_id)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                view=f_view,
                milestone=milestone_id,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/milestones/{milestone_id}/tasks/{task_id}/remove", response_class=HTMLResponse)
    def milestone_remove_task_route(
        request: Request,
        milestone_id: str,
        task_id: str,
        f_view: str = "list",
        f_show_closed: str = "",
        f_stale_days: str = "",
    ) -> HTMLResponse:
        remove_task_from_milestone(workspace, milestone_id, task_id)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                view=f_view,
                milestone=milestone_id,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
            ),
        )

    @app.post("/milestones/{milestone_id}/delete")
    async def delete_milestone_route(milestone_id: str) -> RedirectResponse:
        delete_milestone(workspace, milestone_id)
        return RedirectResponse("/?view=milestones", status_code=303)

    @app.post("/git/sync", response_class=HTMLResponse)
    def git_sync_route(
        request: Request,
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_panel_task: str = Form(""),
        f_view: str = Form("list"),
        f_sort: str = Form("priority"),
        f_sort_dir: str = Form(""),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_hide_done: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
        f_calendar_month: str = Form(""),
        f_calendar_year: str = Form(""),
    ) -> HTMLResponse:
        result = git_sync(workspace)
        selected_task = None
        if f_panel_task:
            try:
                selected_task = load_task(workspace, f_panel_task)
            except Exception:
                selected_task = None
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                selected_task=selected_task,
                projects=f_project,
                date_from=f_from,
                date_to=f_to,
                q=f_q,
                sort=f_sort,
                sort_dir=f_sort_dir,
                view=f_view,
                milestone=f_milestone,
                show_closed=parse_toggle(f_show_closed),
                hide_done=parse_toggle(f_hide_done),
                stale_days=parse_stale_days(f_stale_days),
                calendar_month=parse_calendar_month(f_calendar_month),
                calendar_year=parse_calendar_year(f_calendar_year),
                git_message=result["message"],
                git_message_level="success" if result["ok"] else "error",
            ),
        )

    @app.post("/git/lfs/init", response_class=HTMLResponse)
    def git_lfs_init_route(
        request: Request,
        f_project: list[str] = Form(default=[]),
        f_from: str = Form(""),
        f_to: str = Form(""),
        f_q: str = Form(""),
        f_view: str = Form("list"),
        f_sort: str = Form("priority"),
        f_sort_dir: str = Form(""),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
        f_calendar_month: str = Form(""),
        f_calendar_year: str = Form(""),
    ) -> HTMLResponse:
        result = git_lfs_init(workspace)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                projects=f_project,
                date_from=f_from,
                date_to=f_to,
                q=f_q,
                sort=f_sort,
                sort_dir=f_sort_dir,
                view=f_view,
                milestone=f_milestone,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
                calendar_month=parse_calendar_month(f_calendar_month),
                calendar_year=parse_calendar_year(f_calendar_year),
                git_message=result["message"],
            ),
        )

    @app.get("/export/csv")
    def export_csv(
        project: list[str] = Query(default=[]),
        date_from: str = "",
        date_to: str = "",
        q: str = "",
        show_closed: str = "",
        stale_days: str = "",
    ) -> Response:
        normalize_task_project_refs(workspace)
        projects_all = load_all_projects(workspace)
        project_name_by_id = {p.id: p.name for p in projects_all if p.id}
        tasks = filter_tasks(load_all_tasks(workspace), project, date_from, date_to, q)
        for task in tasks:
            if task.project_id and task.project_id in project_name_by_id:
                task.project = project_name_by_id[task.project_id]
        if not parse_toggle(show_closed):
            tasks, _ = hide_stale_closed_tasks(tasks, parse_stale_days(stale_days))
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "id",
                "title",
                "project",
                "status",
                "priority",
                "percent_complete",
                "start_date",
                "due_date",
                "completed_date",
                "tags",
                "summary",
            ]
        )
        for task in tasks:
            writer.writerow(
                [
                    task.id,
                    task.title,
                    task.project,
                    task.status,
                    task.priority,
                    task.percent_complete,
                    task.start_date or "",
                    task.due_date or "",
                    task.completed_date or "",
                    ", ".join(task.tags),
                    task.summary,
                ]
            )
        return Response(
            buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=taskunity-export.csv"},
        )

    @app.get("/export/json")
    def export_json(
        project: list[str] = Query(default=[]),
        date_from: str = "",
        date_to: str = "",
        q: str = "",
        show_closed: str = "",
        stale_days: str = "",
    ) -> Response:
        normalize_task_project_refs(workspace)
        tasks = filter_tasks(load_all_tasks(workspace), project, date_from, date_to, q)
        if not parse_toggle(show_closed):
            tasks, _ = hide_stale_closed_tasks(tasks, parse_stale_days(stale_days))
        projects = load_all_projects(workspace)
        project_name_by_id = {p.id: p.name for p in projects if p.id}
        for task in tasks:
            if task.project_id and task.project_id in project_name_by_id:
                task.project = project_name_by_id[task.project_id]
        ordered_project_names = [p.name for p in available_projects(projects, tasks)]
        config = ui_config()
        data = tasks_to_jsonantt(
            tasks,
            title=config["export_title"],
            project_colors=project_colors(projects, tasks),
            project_order=ordered_project_names,
        )
        return Response(
            json.dumps(data, indent=2, ensure_ascii=False),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=taskunity-export.json"},
        )

    @app.get("/healthz")
    def healthz() -> Response:
        return Response("ok", media_type="text/plain")

    # --- Settings -----------------------------------------------------------

    @app.post("/settings/save", response_class=HTMLResponse)
    def settings_save_route(
        request: Request,
    ) -> HTMLResponse:
        return HTMLResponse(
            '<div id="ai-settings-status" class="ai-save-ok">✓ AI settings saved in browser.</div>'
        )

    @app.get("/ai/models", response_class=HTMLResponse)
    def ai_models_route(request: Request) -> HTMLResponse:
        cfg = _ai_config_from_query(request)
        if not cfg["ai_base_url"]:
            return HTMLResponse('<option value="">No endpoint configured</option>')
        if not cfg["ai_api_key"]:
            return HTMLResponse('<option value="">No API key configured</option>')
        try:
            models = _ai_fetch_models(cfg)
        except Exception as exc:
            return HTMLResponse(
                '<option value="">Could not load models: '
                + html_lib.escape(_ai_error_summary(exc))
                + "</option>"
            )
        current = cfg["ai_model"]
        opts = "\n".join(
            f'<option value="{html_lib.escape(m)}"{"selected" if m == current else ""}>{html_lib.escape(m)}</option>'
            for m in models
        )
        return HTMLResponse(opts or '<option value="">No models found</option>')

    @app.get("/ai/test", response_class=HTMLResponse)
    def ai_test_route(request: Request) -> HTMLResponse:
        cfg = _ai_config_from_query(request)
        if not cfg["ai_base_url"]:
            return HTMLResponse('<div id="ai-connection-status" class="ai-save-err">Set Base URL first.</div>')
        if not cfg["ai_api_key"]:
            return HTMLResponse('<div id="ai-connection-status" class="ai-save-err">Set API key first.</div>')
        try:
            models = _ai_fetch_models(cfg)
        except Exception as exc:
            return HTMLResponse(
                '<div id="ai-connection-status" class="ai-save-err">'
                + html_lib.escape(_ai_error_summary(exc))
                + "</div>"
            )

        model_count = len(models)
        suffix = "s" if model_count != 1 else ""
        message = f"Connected. Found {model_count} model{suffix}."
        return HTMLResponse('<div id="ai-connection-status" class="ai-save-ok">✓ ' + message + "</div>")

    # --- Project panel ------------------------------------------------------

    @app.get("/projects/{project_id}/panel", response_class=HTMLResponse)
    def project_panel_route(
        request: Request,
        project_id: str,
        view: str = "projects",
        stale_days: str = "",
        show_closed: str = "",
    ) -> HTMLResponse:
        try:
            project = load_project(workspace, project_id)
        except Exception:
            return HTMLResponse('<div class="empty-panel"><h2>Project not found</h2></div>')
        all_tasks = load_all_tasks(workspace)
        project_name_by_id = {p.id: p.name for p in load_all_projects(workspace) if p.id}
        for task in all_tasks:
            if task.project_id and task.project_id in project_name_by_id:
                task.project = project_name_by_id[task.project_id]
        project_tasks = [
            t for t in all_tasks
            if (project.id and t.project_id == project.id) or ((not t.project_id) and t.project == project.name)
        ]
        return templates.TemplateResponse(
            request,
            "partials/project_panel.html",
            {
                "request": request,
                "selected_project": project,
                "project_tasks": project_tasks,
                "filters": {
                    "view": view,
                    "stale_days": parse_stale_days(stale_days),
                    "show_closed": parse_toggle(show_closed),
                },
            },
        )

    @app.post("/projects/{project_id}/save", response_class=HTMLResponse)
    def project_save_route(
        request: Request,
        project_id: str,
        name: str = Form(...),
        description: str = Form(""),
        color: str = Form("#2e6fd8"),
        f_view: str = Form("projects"),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
        f_show_closed: str = Form(""),
    ) -> HTMLResponse:
        try:
            project = load_project(workspace, project_id)
        except Exception:
            project = Project(id=project_id, name=name)
        project.name = name.strip() or project.name
        project.description = description.strip()
        project.color = color.strip() or "#2e6fd8"
        save_project(workspace, project)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(request, view=f_view, stale_days=parse_stale_days(f_stale_days), show_closed=parse_toggle(f_show_closed)),
        )

    @app.post("/projects/{project_id}/delete", response_class=HTMLResponse)
    def project_delete_route(
        request: Request,
        project_id: str,
        f_view: str = Form("projects"),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
        f_show_closed: str = Form(""),
    ) -> HTMLResponse:
        delete_project(workspace, project_id)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(request, view=f_view, stale_days=parse_stale_days(f_stale_days), show_closed=parse_toggle(f_show_closed)),
        )

    # --- AI Assistant -------------------------------------------------------

    @app.get("/ai/panel", response_class=HTMLResponse)
    def ai_general_panel_route(
        request: Request,
        view: str = "list",
        milestone: str = "",
        show_closed: str = "",
        stale_days: str = "",
    ) -> HTMLResponse:
        cfg = ai_config()
        all_tasks = load_all_tasks(workspace)
        all_projects = load_all_projects(workspace)
        all_milestones = load_all_milestones(workspace)
        return templates.TemplateResponse(
            request,
            "partials/assistant_panel.html",
            {
                "request": request,
                "ai_cfg": cfg,
                "context_type": "",
                "entity_id": "",
                "entity_title": "",
                "context_json": "",
                "context_options": {
                    "tasks": [{"id": t.id, "label": t.title} for t in all_tasks],
                    "projects": [{"id": p.id, "label": p.name} for p in all_projects if p.id],
                    "milestones": [{"id": m.id, "label": m.title} for m in all_milestones],
                },
                "filters": {
                    "view": view,
                    "milestone": milestone,
                    "show_closed": parse_toggle(show_closed),
                    "stale_days": parse_stale_days(stale_days),
                },
            },
        )

    @app.get("/ai/panel/task/{task_id}", response_class=HTMLResponse)
    def ai_task_panel_route(
        request: Request,
        task_id: str,
        view: str = "list",
        milestone: str = "",
        show_closed: str = "",
        stale_days: str = "",
    ) -> HTMLResponse:
        cfg = ai_config()
        try:
            task = load_task(workspace, task_id)
        except Exception:
            return HTMLResponse('<div class="empty-panel"><h2>Task not found</h2></div>')
        all_tasks = load_all_tasks(workspace)
        all_projects = load_all_projects(workspace)
        all_milestones = load_all_milestones(workspace)
        ctx_json = _build_task_context(task, all_tasks)
        return templates.TemplateResponse(
            request,
            "partials/assistant_panel.html",
            {
                "request": request,
                "ai_cfg": cfg,
                "context_type": "task",
                "entity_id": task_id,
                "entity_title": task.title,
                "context_json": ctx_json,
                "context_options": {
                    "tasks": [{"id": t.id, "label": t.title} for t in all_tasks],
                    "projects": [{"id": p.id, "label": p.name} for p in all_projects if p.id],
                    "milestones": [{"id": m.id, "label": m.title} for m in all_milestones],
                },
                "filters": {
                    "view": view,
                    "milestone": milestone,
                    "show_closed": parse_toggle(show_closed),
                    "stale_days": parse_stale_days(stale_days),
                },
            },
        )

    @app.get("/ai/panel/milestone/{milestone_id}", response_class=HTMLResponse)
    def ai_milestone_panel_route(
        request: Request,
        milestone_id: str,
        view: str = "list",
        show_closed: str = "",
        stale_days: str = "",
    ) -> HTMLResponse:
        cfg = ai_config()
        try:
            milestone = load_milestone(workspace, milestone_id)
        except Exception:
            return HTMLResponse('<div class="empty-panel"><h2>Milestone not found</h2></div>')
        all_tasks = load_all_tasks(workspace)
        all_projects = load_all_projects(workspace)
        all_milestones = load_all_milestones(workspace)
        ctx_json = _build_milestone_context(milestone, all_tasks)
        return templates.TemplateResponse(
            request,
            "partials/assistant_panel.html",
            {
                "request": request,
                "ai_cfg": cfg,
                "context_type": "milestone",
                "entity_id": milestone_id,
                "entity_title": milestone.title,
                "context_json": ctx_json,
                "context_options": {
                    "tasks": [{"id": t.id, "label": t.title} for t in all_tasks],
                    "projects": [{"id": p.id, "label": p.name} for p in all_projects if p.id],
                    "milestones": [{"id": m.id, "label": m.title} for m in all_milestones],
                },
                "filters": {
                    "view": view,
                    "milestone": milestone_id,
                    "show_closed": parse_toggle(show_closed),
                    "stale_days": parse_stale_days(stale_days),
                },
            },
        )

    @app.post("/ai/chat", response_class=HTMLResponse)
    async def ai_chat_route(
        request: Request,
        ai_enabled: str = Form("0"),
        ai_base_url: str = Form(""),
        ai_api_key: str = Form(""),
        ai_model: str = Form(""),
        ai_chat_path: str = Form(""),
        ai_models_path: str = Form(""),
        ai_timeout_seconds: str = Form("30"),
        ai_max_tokens: str = Form("2048"),
        ai_temperature: str = Form("0.7"),
        context_type: str = Form(...),
        entity_id: str = Form(...),
        user_message: str = Form(...),
        context_json: str = Form(""),
        extra_context_json: str = Form("[]"),
        history: str = Form("[]"),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        cfg = {
            "ai_enabled": "1" if ai_enabled in {"1", "on", "true", "yes"} else "0",
            "ai_base_url": ai_base_url.strip(),
            "ai_api_key": ai_api_key.strip(),
            "ai_model": ai_model.strip(),
            "ai_chat_path": ai_chat_path.strip(),
            "ai_models_path": ai_models_path.strip(),
            "ai_timeout_seconds": ai_timeout_seconds.strip() or "30",
            "ai_max_tokens": ai_max_tokens.strip() or "2048",
            "ai_temperature": ai_temperature.strip() or "0.7",
        }

        if cfg["ai_enabled"] != "1":
            return HTMLResponse(_ai_error_html("AI is not enabled. Configure it in ⚙ Settings."))
        if not cfg["ai_base_url"]:
            return HTMLResponse(_ai_error_html("No AI endpoint configured. Set Base URL in ⚙ Settings."))
        if not cfg["ai_model"]:
            return HTMLResponse(_ai_error_html("No AI model configured. Set Model in ⚙ Settings."))

        # Parse conversation history
        try:
            history_msgs: list[dict[str, str]] = json.loads(history or "[]")
            if not isinstance(history_msgs, list):
                history_msgs = []
        except (json.JSONDecodeError, ValueError):
            history_msgs = []

        # Build messages
        extra_blocks = _resolve_extra_context_blocks(
            extra_context_json,
            current_context_type=context_type,
            current_entity_id=entity_id,
        )
        extra_context_text = ""
        if extra_blocks:
            extra_parts: list[str] = []
            for kind, label, block_json in extra_blocks:
                extra_parts.append(
                    f"Additional Context ({kind}: {label}):\n```json\n{block_json}\n```"
                )
            extra_context_text = "\n\n" + "\n\n".join(extra_parts)
        user_content = (
            f"Context ({context_type}):\n```json\n{context_json}\n```"
            f"{extra_context_text}\n\nUser: {user_message}"
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": TASKUNITY_ASSISTANT_SPEC},
        ]
        # Include prior turns (skip system messages already in history)
        for msg in history_msgs:
            if msg.get("role") in {"user", "assistant"}:
                messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": user_content})

        def _best_history_checklist_items(task_title_value: str) -> list[str]:
            scored: list[tuple[int, int, list[str]]] = []
            for idx, msg in enumerate(history_msgs[-24:]):
                if msg.get("role") != "assistant":
                    continue
                chunk = str(msg.get("content") or "").strip()
                if not chunk:
                    continue
                extracted = _extract_checklist_items(chunk, task_title_value)
                if not extracted:
                    continue
                lowered = chunk.lower()
                score = len(extracted) * 3
                if "checklist" in lowered and any(k in lowered for k in ("proposed", "revised", "updated", "phase")):
                    score += 6
                if "[ ]" in chunk or "`[ ]`" in chunk:
                    score += 3
                if any(k in lowered for k in ("what to provide", "please let me know how you'd like to proceed", "i need a few more details")):
                    score -= 9
                scored.append((score, idx, extracted))

            if not scored:
                return []
            scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
            best_score, _, best_items = scored[0]
            return best_items if best_score > 0 else []

        def _normalize_items(raw_items: list[str] | tuple[str, ...] | None) -> list[str]:
            if not isinstance(raw_items, (list, tuple)):
                return []
            dedupe: list[str] = []
            seen: set[str] = set()
            for item in raw_items:
                text = str(item).strip()
                key = text.lower()
                if not text or key in seen:
                    continue
                seen.add(key)
                dedupe.append(text)
            return dedupe

        def _current_user_refers_to_prior_list() -> bool:
            return bool(
                re.search(r"\b(those|that|them|the above|prior|previous)\b", user_message or "", re.IGNORECASE)
                and re.search(r"\b(checklist|list|items|tasks)\b", user_message or "", re.IGNORECASE)
            )

        def _wants_checklist_update_intent() -> bool:
            if context_type != "task":
                return False
            msg = user_message or ""
            has_action = bool(re.search(r"\b(update|apply|set|rewrite|replace|revise|make|use|put|do|yes|yep|yeah|ok)\b", msg, re.IGNORECASE))
            if not has_action:
                return False

            def _recent_assistant_has_checklist_proposal() -> bool:
                for hist in reversed(history_msgs[-14:]):
                    if hist.get("role") != "assistant":
                        continue
                    chunk = str(hist.get("content") or "")
                    if not chunk:
                        continue
                    if "checklist" in chunk.lower() and _extract_checklist_items(chunk):
                        return True
                return False

            # Direct intent: user mentions checklist/tasks explicitly.
            if re.search(r"\b(checklist|tasks?)\b", msg, re.IGNORECASE):
                return True

            # Plain confirmations ("yes", "ok", "yeah") should apply if a recent checklist proposal exists.
            if re.search(r"^\s*(yes|yep|yeah|ok|okay|do it|go ahead|sounds good)\s*[.!]*\s*$", msg, re.IGNORECASE):
                return _recent_assistant_has_checklist_proposal()

            # Deictic intent: "use/update with those/them/that" and recent assistant checklist proposal exists.
            refers_deictic = bool(re.search(r"\b(those|them|that|these|it)\b", msg, re.IGNORECASE))
            if not refers_deictic:
                return False
            return _recent_assistant_has_checklist_proposal()

        def _looks_like_clarification_response(text: str) -> bool:
            lowered = (text or "").strip().lower()
            if not lowered:
                return False
            return any(
                phrase in lowered
                for phrase in (
                    "clarification needed",
                    "could you please clarify",
                    "what to provide",
                    "most likely options",
                    "option a",
                    "option b",
                    "option c",
                    "let me know which",
                    "i need a few more details",
                )
            )

        intent_contract_cache: dict | None = None

        def _resolve_intent_contract(task_title_value: str) -> dict:
            nonlocal intent_contract_cache
            if intent_contract_cache is not None:
                return intent_contract_cache

            recent_turns: list[str] = []
            for msg in history_msgs[-14:]:
                role = msg.get("role")
                if role not in {"user", "assistant"}:
                    continue
                content = str(msg.get("content") or "").strip()
                if not content:
                    continue
                recent_turns.append(f"{role}: {content[:1400]}")

            payload = (
                f"Context type: {context_type}\n"
                f"Entity id: {entity_id}\n"
                f"Task title: {task_title_value}\n"
                f"Latest user message: {user_message}\n"
                "Recent conversation:\n"
                + "\n".join(recent_turns)
            )
            resolver_prompt = (
                "Resolve user intent and referenced actionable content using the intent contract. "
                "Return strict JSON object only."
            )
            try:
                resolved_resp = _ai_call(
                    [
                        {"role": "system", "content": TASKUNITY_INTENT_SPEC},
                        {"role": "system", "content": TASKUNITY_ASSISTANT_SPEC},
                        {"role": "system", "content": resolver_prompt},
                        {"role": "user", "content": payload},
                    ],
                    cfg,
                    max_tokens=420,
                    temperature=0.0,
                )
                resolved_text = str(resolved_resp["choices"][0]["message"]["content"])
                parsed = _parse_json_object_from_text(resolved_text)
                intent_contract_cache = parsed if isinstance(parsed, dict) else {}
                return intent_contract_cache
            except Exception:
                intent_contract_cache = {}
                return intent_contract_cache

        def _local_checklist_fallback_response() -> HTMLResponse | None:
            if context_type != "task":
                return None

            task_title = ""
            try:
                task_title = load_task(workspace, entity_id).title
            except Exception:
                task_title = ""

            contract = _resolve_intent_contract(task_title)
            intent_obj = contract.get("intent") if isinstance(contract, dict) else None
            fallback_items: list[str] = []
            checklist_mode = "add"
            if isinstance(intent_obj, dict):
                kind = str(intent_obj.get("kind", "")).strip().lower()
                try:
                    confidence = float(intent_obj.get("confidence", 0.0) or 0.0)
                except (TypeError, ValueError):
                    confidence = 0.0
                mode = str(intent_obj.get("mode", "")).strip().lower()
                if mode in {"add", "replace"}:
                    checklist_mode = mode
                resolved_items = _normalize_items(contract.get("resolved_checklist_items"))
                if kind == "update_checklist" and confidence >= 0.45 and resolved_items:
                    fallback_items = resolved_items

            if not fallback_items and not _wants_checklist_update_intent():
                return None

            if not fallback_items:
                fallback_items = _best_history_checklist_items(task_title)
            if not fallback_items:
                return None

            if checklist_mode not in {"add", "replace"}:
                checklist_mode = "add"
            if checklist_mode == "add" and re.search(r"\b(rewrite|replace|revise|overhaul|restructure|new checklist)\b", user_message or "", re.IGNORECASE):
                checklist_mode = "replace"

            return _render_checklist_suggestion_from_items(
                fallback_items,
                checklist_mode,
                "AI endpoint is currently unavailable. Using the latest checklist proposal from this chat so you can still preview and apply changes.",
            )

        def _render_checklist_suggestion_from_items(
            items: list[str],
            checklist_mode: str,
            display_text: str,
        ) -> HTMLResponse:
            synthetic_reply = display_text + "\n\n" + "\n".join(f"- {item}" for item in items)
            new_history = list(history_msgs)
            new_history.append({"role": "user", "content": user_message})
            new_history.append({"role": "assistant", "content": synthetic_reply})
            history_json = json.dumps(new_history)
            rendered_md = markdown_lib.markdown(display_text, extensions=["extra", "sane_lists"])
            return HTMLResponse(
                templates.get_template("partials/ai_message.html").render({
                    "reply_html": rendered_md,
                    "has_tasks": False,
                    "has_checklist": True,
                    "has_note": False,
                    "has_file_edits": False,
                    "tasks_json": "[]",
                    "checklist_json": json.dumps(items),
                    "checklist_mode": checklist_mode,
                    "note_text": "",
                    "file_edits_json": "[]",
                    "entity_id": entity_id,
                    "context_type": context_type,
                    "history_json": history_json,
                    "f_view": f_view,
                    "f_milestone": f_milestone,
                    "f_show_closed": f_show_closed,
                    "f_stale_days": f_stale_days,
                })
            )

        def _second_pass_resolve_checklist_items(task_title_value: str) -> list[str]:
            if not _wants_checklist_update_intent():
                return []

            contract = _resolve_intent_contract(task_title_value)
            intent = contract.get("intent") if isinstance(contract, dict) else None
            if isinstance(intent, dict):
                kind = str(intent.get("kind", "")).strip().lower()
                try:
                    confidence = float(intent.get("confidence", 0.0) or 0.0)
                except (TypeError, ValueError):
                    confidence = 0.0
                mode = str(intent.get("mode", "")).strip().lower()
                if mode in {"add", "replace"}:
                    suggestions["checklist_mode_hint"] = mode
                resolved_items = _normalize_items(contract.get("resolved_checklist_items"))
                if kind == "update_checklist" and confidence >= 0.5 and resolved_items:
                    return resolved_items

            recent_turns: list[str] = []
            for msg in history_msgs[-12:]:
                role = msg.get("role")
                if role not in {"user", "assistant"}:
                    continue
                content = str(msg.get("content") or "").strip()
                if not content:
                    continue
                recent_turns.append(f"{role}: {content[:1400]}")
            if not recent_turns:
                return []

            resolver_prompt = (
                "You are extracting checklist items for a task update action. "
                "Given recent chat turns and the latest user request, return STRICT JSON only: "
                "{\"suggested_checklist_items\":[...],\"checklist_mode\":\"add|replace\"}. "
                "Rules: "
                "1) Prefer the most complete prior proposed checklist if the latest assistant message is clarification/options. "
                "2) Exclude questions, option labels, instructions, and metadata. "
                "3) Keep concise actionable items only. "
                "4) If user asks rewrite/replace/new checklist, set checklist_mode to replace, else add. "
                "5) Do not include task title alone as an item."
            )
            resolver_user = (
                f"Task title: {task_title_value}\n"
                f"Latest user request: {user_message}\n"
                "Recent conversation:\n"
                + "\n".join(recent_turns)
            )

            try:
                resolver_resp = _ai_call(
                    [
                        {"role": "system", "content": resolver_prompt},
                        {"role": "user", "content": resolver_user},
                    ],
                    cfg,
                    max_tokens=420,
                    temperature=0.0,
                )
                resolver_text = str(resolver_resp["choices"][0]["message"]["content"])
                resolved = _parse_ai_suggestions(resolver_text)
                mode = str(resolved.get("checklist_mode", "")).strip().lower()
                if mode in {"add", "replace"}:
                    suggestions["checklist_mode_hint"] = mode
                items = _normalize_items(resolved.get("suggested_checklist_items"))
                return items
            except Exception:
                return []

        # Deterministic fast-path: if user asks to apply/update prior checklist proposal,
        # bypass another LLM generation step and surface preview/apply immediately.
        if context_type == "task" and _wants_checklist_update_intent():
            task_title = ""
            try:
                task_title = load_task(workspace, entity_id).title
            except Exception:
                task_title = ""

            contract = _resolve_intent_contract(task_title)
            intent_obj = contract.get("intent") if isinstance(contract, dict) else None
            if isinstance(intent_obj, dict):
                kind = str(intent_obj.get("kind", "")).strip().lower()
                try:
                    confidence = float(intent_obj.get("confidence", 0.0) or 0.0)
                except (TypeError, ValueError):
                    confidence = 0.0
                mode = str(intent_obj.get("mode", "")).strip().lower()
                resolved_items = _normalize_items(contract.get("resolved_checklist_items"))
                if kind == "update_checklist" and confidence >= 0.6 and resolved_items:
                    checklist_mode = mode if mode in {"add", "replace"} else "add"
                    return _render_checklist_suggestion_from_items(
                        resolved_items,
                        checklist_mode,
                        "Using intent resolution from the conversation contract. Review the preview and apply when ready.",
                    )

            history_items = _normalize_items(_best_history_checklist_items(task_title))
            msg_lower = (user_message or "").strip().lower()
            deictic_apply = _current_user_refers_to_prior_list() or (
                bool(re.search(r"\b(update|apply|put|use|set|do|yes|yep|yeah|ok)\b", msg_lower, re.IGNORECASE))
                and not bool(re.search(r"\b(new|make|create|draft|suggest|propose|generate)\b", msg_lower, re.IGNORECASE))
            )
            if history_items and deictic_apply:
                checklist_mode = "replace" if re.search(r"\b(rewrite|replace|revise|new checklist)\b", msg_lower, re.IGNORECASE) else "add"
                return _render_checklist_suggestion_from_items(
                    history_items,
                    checklist_mode,
                    "Using the previously proposed checklist from this conversation. Review the preview and apply when ready.",
                )

        try:
            response = _ai_call(messages, cfg)
            reply_text = response["choices"][0]["message"]["content"]
        except Exception as exc:
            fallback = _local_checklist_fallback_response()
            if fallback is not None:
                return fallback
            if isinstance(exc, urllib.error.HTTPError):
                body = exc.read().decode("utf-8", errors="replace")[:300]
                return HTMLResponse(_ai_error_html(f"HTTP {exc.code}: {exc.reason} — {body}"))
            if isinstance(exc, urllib.error.URLError):
                return HTMLResponse(_ai_error_html(f"Connection error: {exc.reason}"))
            return HTMLResponse(_ai_error_html("An unexpected error occurred. Please check your endpoint settings."))

        suggestions = _parse_ai_suggestions(reply_text)
        # Strip the JSON block from the display text
        display_text = re.sub(r"```(?:json)?\s*\{.*?\}\s*```", "", reply_text, flags=re.DOTALL).strip()

        checklist_intent_allowed = _wants_checklist_update_intent()
        intent_mode_from_contract = ""
        if context_type == "task":
            task_title = ""
            try:
                task_title = load_task(workspace, entity_id).title
            except Exception:
                task_title = ""
            contract = _resolve_intent_contract(task_title)
            intent_obj = contract.get("intent") if isinstance(contract, dict) else None
            if isinstance(intent_obj, dict):
                kind = str(intent_obj.get("kind", "")).strip().lower()
                try:
                    confidence = float(intent_obj.get("confidence", 0.0) or 0.0)
                except (TypeError, ValueError):
                    confidence = 0.0
                mode = str(intent_obj.get("mode", "")).strip().lower()
                if mode in {"add", "replace"}:
                    intent_mode_from_contract = mode
                if kind == "update_checklist" and confidence >= 0.45:
                    checklist_intent_allowed = True
                elif kind in {"advice", "clarify"} and confidence >= 0.45:
                    checklist_intent_allowed = False

        if context_type == "task" and checklist_intent_allowed:
            task_title = ""
            try:
                task_title = load_task(workspace, entity_id).title
            except Exception:
                task_title = ""
            resolved_items = _second_pass_resolve_checklist_items(task_title)
            if resolved_items:
                suggestions["suggested_checklist_items"] = resolved_items

        if context_type == "task" and checklist_intent_allowed and not suggestions.get("suggested_checklist_items"):
            task_title = ""
            try:
                task_title = load_task(workspace, entity_id).title
            except Exception:
                task_title = ""
            current_items = _normalize_items(_extract_checklist_items(display_text or reply_text, task_title))
            history_items = _normalize_items(_best_history_checklist_items(task_title))
            clarifying_reply = _looks_like_clarification_response(display_text or reply_text)

            choose_current = False
            if current_items:
                # Deictic asks like "use those" should prefer the richer prior proposal.
                if _current_user_refers_to_prior_list() and history_items:
                    choose_current = False
                elif clarifying_reply and history_items:
                    choose_current = False
                # Prefer direct current extraction for explicit add-item asks.
                elif re.search(r"\badd\b", user_message or "", re.IGNORECASE) and not re.search(r"\b(rewrite|replace|revise|new checklist|update checklist)\b", user_message or "", re.IGNORECASE):
                    choose_current = True
                elif len(current_items) >= 4 and (not history_items or len(current_items) >= len(history_items)):
                    choose_current = True

            fallback_items = current_items if choose_current or not history_items else history_items
            if fallback_items:
                suggestions["suggested_checklist_items"] = fallback_items

        if context_type == "task" and checklist_intent_allowed:
            task_title = ""
            try:
                task_title = load_task(workspace, entity_id).title
            except Exception:
                task_title = ""

            current_structured = _normalize_items(suggestions.get("suggested_checklist_items"))
            history_items = _normalize_items(_best_history_checklist_items(task_title))
            if current_structured and history_items and (_current_user_refers_to_prior_list() or _looks_like_clarification_response(display_text or reply_text)):
                # For deictic/clarification flows, favor the prior proposed checklist.
                if len(history_items) >= len(current_structured):
                    suggestions["suggested_checklist_items"] = history_items

        new_history = list(history_msgs)
        new_history.append({"role": "user", "content": user_message})
        new_history.append({"role": "assistant", "content": reply_text})
        history_json = json.dumps(new_history)

        if context_type == "task" and not checklist_intent_allowed:
            suggestions.pop("suggested_checklist_items", None)

        has_tasks = bool(suggestions.get("suggested_tasks"))
        has_checklist = bool(suggestions.get("suggested_checklist_items")) and context_type == "task" and checklist_intent_allowed
        has_note = bool(suggestions.get("suggested_note"))
        has_file_edits = bool(suggestions.get("suggested_file_edits"))
        checklist_mode = "add"
        mode_hint = str(suggestions.get("checklist_mode_hint", "")).strip().lower()
        if has_checklist and intent_mode_from_contract in {"add", "replace"}:
            checklist_mode = intent_mode_from_contract
        elif has_checklist and mode_hint in {"add", "replace"}:
            checklist_mode = mode_hint
        elif has_checklist and re.search(r"\b(rewrite|replace|revise|overhaul|restructure)\b", user_message or "", re.IGNORECASE):
            checklist_mode = "replace"
        tasks_json = json.dumps(suggestions.get("suggested_tasks", []))
        checklist_json = json.dumps(suggestions.get("suggested_checklist_items", []))
        note_text = str(suggestions.get("suggested_note", ""))
        file_edits_json = json.dumps(suggestions.get("suggested_file_edits", []))

        rendered_md = markdown_lib.markdown(display_text, extensions=["extra", "sane_lists"])
        return HTMLResponse(
            templates.get_template("partials/ai_message.html").render({
                "reply_html": rendered_md,
                "has_tasks": has_tasks,
                "has_checklist": has_checklist,
                "has_note": has_note,
                "has_file_edits": has_file_edits,
                "tasks_json": tasks_json,
                "checklist_json": checklist_json,
                "checklist_mode": checklist_mode,
                "note_text": note_text,
                "file_edits_json": file_edits_json,
                "entity_id": entity_id,
                "context_type": context_type,
                "history_json": history_json,
                "f_view": f_view,
                "f_milestone": f_milestone,
                "f_show_closed": f_show_closed,
                "f_stale_days": f_stale_days,
            })
        )

    @app.post("/ai/apply/tasks/{milestone_id}", response_class=HTMLResponse)
    def ai_apply_tasks_route(
        request: Request,
        milestone_id: str,
        tasks_json: str = Form("[]"),
        f_view: str = Form("list"),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        try:
            suggested: list[dict] = json.loads(tasks_json)
        except (json.JSONDecodeError, ValueError):
            suggested = []
        created = []
        for item in suggested:
            if not isinstance(item, dict) or not item.get("title"):
                continue
            task = create_task(workspace, item["title"])
            task.summary = str(item.get("summary", ""))[:_MAX_TASK_SUMMARY_LENGTH]
            if item.get("priority") in {"low", "normal", "high", "critical"}:
                task.priority = item["priority"]
            save_task(workspace, task)
            # add to milestone if valid
            try:
                add_task_to_milestone(workspace, milestone_id, task.id)
            except Exception:
                pass
            created.append(task.title)
        n = len(created)
        return templates.TemplateResponse(
            request,
            "partials/main.html",
            context(
                request,
                view=f_view,
                milestone=milestone_id,
                show_closed=parse_toggle(f_show_closed),
                stale_days=parse_stale_days(f_stale_days),
                git_message=f"Created {n} task{'s' if n != 1 else ''} from AI suggestions." if n else "No tasks created.",
            ),
        )

    @app.post("/ai/apply/checklist/{task_id}", response_class=HTMLResponse)
    def ai_apply_checklist_route(
        request: Request,
        task_id: str,
        checklist_json: str = Form("[]"),
        checklist_mode: str = Form("add"),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        try:
            items: list[str] = json.loads(checklist_json)
        except (json.JSONDecodeError, ValueError):
            items = []
        try:
            task = load_task(workspace, task_id)
        except Exception:
            return HTMLResponse('<div class="empty-panel"><h2>Task not found</h2></div>')

        mode = "replace" if (checklist_mode or "").strip().lower() == "replace" else "add"

        if mode == "replace":
            done_by_text = {item.text.strip().lower(): item.done for item in task.checklist if item.text.strip()}
            replacement: list[str] = []
            seen: set[str] = set()
            for item in items:
                text = str(item).strip()
                key = text.lower()
                if not text or key in seen:
                    continue
                seen.add(key)
                replacement.append(text)

            task.checklist = [
                ChecklistItem(text=text, done=bool(done_by_text.get(text.lower(), False)))
                for text in replacement
            ]
            save_task(workspace, task)
            message = f"Rewrote checklist for task '{task.title}'. Set {len(replacement)} item{'s' if len(replacement) != 1 else ''}."
            return HTMLResponse(
                templates.get_template("partials/ai_action_result.html").render({
                    "message": message,
                    "details": replacement,
                    "refresh_task": task.id,
                    "refresh_task_panel_url": _task_panel_refresh_url(
                        task.id,
                        f_view=f_view,
                        f_milestone=f_milestone,
                        f_show_closed=f_show_closed,
                        f_stale_days=f_stale_days,
                    ),
                    "refresh_context_url": f"/ai/panel/task/{task.id}?view={urllib.parse.quote(f_view)}"
                    + (f"&milestone={urllib.parse.quote(f_milestone)}" if f_milestone else "")
                    + ("&show_closed=1" if parse_toggle(f_show_closed) else "")
                    + f"&stale_days={parse_stale_days(f_stale_days)}",
                })
            )

        added = 0
        added_items: list[str] = []
        skipped_existing = 0
        existing = {item.text.strip().lower() for item in task.checklist if item.text.strip()}
        for item in items:
            text = str(item).strip()
            if text and text.lower() not in existing:
                task.checklist.append(ChecklistItem(text=text))
                added += 1
                added_items.append(text)
                existing.add(text.lower())
            elif text:
                skipped_existing += 1
        if added:
            save_task(workspace, task)
        message = f"Updated checklist for task '{task.title}'. Added {added} item{'s' if added != 1 else ''}."
        if skipped_existing:
            message += f" Skipped {skipped_existing} duplicate item{'s' if skipped_existing != 1 else ''}."
        return HTMLResponse(
            templates.get_template("partials/ai_action_result.html").render({
                "message": message,
                "details": added_items,
                "refresh_task": task.id,
                "refresh_task_panel_url": _task_panel_refresh_url(
                    task.id,
                    f_view=f_view,
                    f_milestone=f_milestone,
                    f_show_closed=f_show_closed,
                    f_stale_days=f_stale_days,
                ),
                "refresh_context_url": f"/ai/panel/task/{task.id}?view={urllib.parse.quote(f_view)}"
                + (f"&milestone={urllib.parse.quote(f_milestone)}" if f_milestone else "")
                + ("&show_closed=1" if parse_toggle(f_show_closed) else "")
                + f"&stale_days={parse_stale_days(f_stale_days)}",
            })
        )

    @app.post("/ai/preview/checklist/{task_id}", response_class=HTMLResponse)
    def ai_preview_checklist_route(
        request: Request,
        task_id: str,
        checklist_json: str = Form("[]"),
        checklist_mode: str = Form("add"),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        try:
            items: list[str] = json.loads(checklist_json)
        except (json.JSONDecodeError, ValueError):
            items = []
        try:
            task = load_task(workspace, task_id)
        except Exception:
            return HTMLResponse('<div class="ai-msg ai-msg-error"><strong>Error:</strong> Task not found.</div>')

        mode = "replace" if (checklist_mode or "").strip().lower() == "replace" else "add"
        before_lines = [f"- [{'x' if i.done else ' '}] {i.text}" for i in task.checklist]

        if mode == "replace":
            done_by_text = {item.text.strip().lower(): item.done for item in task.checklist if item.text.strip()}
            replacement: list[str] = []
            seen: set[str] = set()
            for item in items:
                text = str(item).strip()
                key = text.lower()
                if not text or key in seen:
                    continue
                seen.add(key)
                replacement.append(text)

            after_lines = [
                f"- [{'x' if done_by_text.get(t.lower(), False) else ' '}] {t}"
                for t in replacement
            ]
            diff_text = "\n".join(
                difflib.unified_diff(
                    before_lines,
                    after_lines,
                    fromfile="checklist (before)",
                    tofile="checklist (after)",
                    lineterm="",
                )
            )
            message = f"Preview for task '{task.title}': replace checklist with {len(replacement)} item{'s' if len(replacement) != 1 else ''}."
            return HTMLResponse(
                templates.get_template("partials/ai_action_result.html").render({
                    "message": message,
                    "details": replacement[:12],
                    "preview_diffs": [{"path": "checklist", "diff": diff_text[:16000]}],
                    "dispatch_apply": False,
                    "apply_url": f"/ai/apply/checklist/{task_id}",
                    "apply_button_label": "Apply checklist rewrite",
                    "apply_fields": {
                        "checklist_json": checklist_json,
                        "checklist_mode": mode,
                        "f_view": f_view,
                        "f_milestone": f_milestone,
                        "f_show_closed": f_show_closed,
                        "f_stale_days": f_stale_days,
                    },
                    "refresh_task": "",
                    "refresh_context_url": "",
                })
            )

        existing = {item.text.strip().lower() for item in task.checklist if item.text.strip()}
        add_items: list[str] = []
        skipped = 0
        for item in items:
            text = str(item).strip()
            if not text:
                continue
            if text.lower() in existing:
                skipped += 1
                continue
            existing.add(text.lower())
            add_items.append(text)

        after_lines = list(before_lines)
        after_lines.extend([f"- [ ] {t}" for t in add_items])
        diff_text = "\n".join(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile="checklist (before)",
                tofile="checklist (after)",
                lineterm="",
            )
        )

        message = f"Preview for task '{task.title}': add {len(add_items)} checklist item{'s' if len(add_items) != 1 else ''}."
        if skipped:
            message += f" Skip {skipped} duplicate item{'s' if skipped != 1 else ''}."

        return HTMLResponse(
            templates.get_template("partials/ai_action_result.html").render({
                "message": message,
                "details": add_items[:12],
                "preview_diffs": [{"path": "checklist", "diff": diff_text[:16000]}],
                "dispatch_apply": False,
                "apply_url": f"/ai/apply/checklist/{task_id}",
                "apply_button_label": "Apply checklist changes",
                "apply_fields": {
                    "checklist_json": checklist_json,
                    "checklist_mode": mode,
                    "f_view": f_view,
                    "f_milestone": f_milestone,
                    "f_show_closed": f_show_closed,
                    "f_stale_days": f_stale_days,
                },
                "refresh_task": "",
                "refresh_context_url": "",
            })
        )

    @app.post("/ai/apply/note/{task_id}", response_class=HTMLResponse)
    def ai_apply_note_route(
        request: Request,
        task_id: str,
        note_text: str = Form(""),
        f_view: str = Form("list"),
        f_milestone: str = Form(""),
        f_show_closed: str = Form(""),
        f_stale_days: str = Form(str(STALE_CLOSED_DAYS)),
    ) -> HTMLResponse:
        body = note_text.strip()
        task = None
        if body:
            try:
                task = load_task(workspace, task_id)
                task.notes.append(Note(body=body))
                save_task(workspace, task)
            except Exception:
                task = None
        if not task:
            return HTMLResponse(
                templates.get_template("partials/ai_action_result.html").render({
                    "message": "Could not save note from AI output.",
                    "details": [],
                    "refresh_task": "",
                    "refresh_context_url": "",
                })
            )
        return HTMLResponse(
            templates.get_template("partials/ai_action_result.html").render({
                "message": f"Saved AI note to task '{task.title}'.",
                "details": [body[:200]] if body else [],
                "refresh_task": task.id,
                "refresh_task_panel_url": _task_panel_refresh_url(
                    task.id,
                    f_view=f_view,
                    f_milestone=f_milestone,
                    f_show_closed=f_show_closed,
                    f_stale_days=f_stale_days,
                ),
                "refresh_context_url": f"/ai/panel/task/{task.id}?view={urllib.parse.quote(f_view)}"
                + (f"&milestone={urllib.parse.quote(f_milestone)}" if f_milestone else "")
                + ("&show_closed=1" if parse_toggle(f_show_closed) else "")
                + f"&stale_days={parse_stale_days(f_stale_days)}",
            })
        )

    @app.post("/ai/decline", response_class=HTMLResponse)
    def ai_decline_route(
        request: Request,
        decline_label: str = Form("suggested changes"),
        history: str = Form("[]"),
    ) -> HTMLResponse:
        try:
            history_msgs: list[dict[str, str]] = json.loads(history or "[]")
            if not isinstance(history_msgs, list):
                history_msgs = []
        except (json.JSONDecodeError, ValueError):
            history_msgs = []

        label = (decline_label or "suggested changes").strip()
        user_turn = f"Declined: {label}."
        assistant_turn = (
            f"Understood. I did not apply {label.lower()}. "
            "I can revise the proposal if you want a different version."
        )

        new_history = list(history_msgs)
        new_history.append({"role": "user", "content": user_turn})
        new_history.append({"role": "assistant", "content": assistant_turn})
        history_json = json.dumps(new_history)

        rendered_md = markdown_lib.markdown(assistant_turn, extensions=["extra", "sane_lists"])
        return HTMLResponse(
            templates.get_template("partials/ai_message.html").render({
                "reply_html": rendered_md,
                "has_tasks": False,
                "has_checklist": False,
                "has_note": False,
                "has_file_edits": False,
                "tasks_json": "[]",
                "checklist_json": "[]",
                "checklist_mode": "add",
                "note_text": "",
                "file_edits_json": "[]",
                "entity_id": "",
                "context_type": "task",
                "history_json": history_json,
                "f_view": "list",
                "f_milestone": "",
                "f_show_closed": "",
                "f_stale_days": str(STALE_CLOSED_DAYS),
            })
        )

    @app.post("/ai/apply/file-edits", response_class=HTMLResponse)
    def ai_apply_file_edits_route(
        request: Request,
        file_edits_json: str = Form("[]"),
    ) -> HTMLResponse:
        try:
            file_edits = json.loads(file_edits_json)
            if not isinstance(file_edits, list):
                file_edits = []
        except (json.JSONDecodeError, ValueError):
            file_edits = []

        files_changed, edits_applied, details, _diffs = _apply_structured_file_edits(file_edits, dry_run=False)
        message = (
            f"Applied {edits_applied} edit{'s' if edits_applied != 1 else ''} "
            f"across {files_changed} file{'s' if files_changed != 1 else ''}."
        )
        if edits_applied == 0:
            message = "No file edits were applied."

        return HTMLResponse(
            templates.get_template("partials/ai_action_result.html").render({
                "message": message,
                "details": details[:20],
                "refresh_task": "",
                "refresh_context_url": "",
            })
        )

    @app.post("/ai/preview/file-edits", response_class=HTMLResponse)
    def ai_preview_file_edits_route(
        request: Request,
        file_edits_json: str = Form("[]"),
    ) -> HTMLResponse:
        try:
            file_edits = json.loads(file_edits_json)
            if not isinstance(file_edits, list):
                file_edits = []
        except (json.JSONDecodeError, ValueError):
            file_edits = []

        files_changed, edits_applied, details, diffs = _apply_structured_file_edits(file_edits, dry_run=True)
        message = (
            f"Preview: {edits_applied} edit{'s' if edits_applied != 1 else ''} "
            f"across {files_changed} file{'s' if files_changed != 1 else ''}."
        )
        if edits_applied == 0:
            message = "Preview found no file edits to apply."

        return HTMLResponse(
            templates.get_template("partials/ai_action_result.html").render({
                "message": message,
                "details": details[:20],
                "preview_diffs": [{"path": p, "diff": d} for p, d in diffs[:8]],
                "dispatch_apply": False,
                "apply_url": "/ai/apply/file-edits",
                "apply_button_label": "Apply file edits",
                "apply_fields": {
                    "file_edits_json": file_edits_json,
                },
                "refresh_task": "",
                "refresh_context_url": "",
            })
        )

    return app
