from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from .app import create_app
from .task_store import copy_starter_files, ensure_workspace


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="taskwright", description="Local file-backed workspace/task tracker")
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init", help="Initialize a new workspace")
    init_p.add_argument("path", nargs="?", default=".", help="Workspace path")
    init_p.add_argument("--no-sample", action="store_true", help="Ignored; init no longer creates sample content")

    serve_p = sub.add_parser("serve", help="Serve the local web UI")
    serve_p.add_argument("--workspace", default=".", help="Workspace path")
    serve_p.add_argument("--host", default="127.0.0.1", help="Host interface")
    serve_p.add_argument("--port", default=8000, type=int, help="Port")
    serve_p.add_argument("--reload", action="store_true", help="Enable uvicorn reload")

    args = parser.parse_args(argv)

    if args.command == "init":
        target = Path(args.path).resolve()
        if args.no_sample:
            ensure_workspace(target)
        else:
            copy_starter_files(target)
        print(f"Initialized taskwright workspace: {target}")
        print("Run: taskwright serve --workspace", target)
        return

    if args.command == "serve":
        workspace = Path(args.workspace).resolve()
        ensure_workspace(workspace)
        app = create_app(workspace)
        print(f"Serving workspace: {workspace}")
        uvicorn.run(app, host=args.host, port=args.port, reload=args.reload)
