# Command-line reference

Taskunity exposes a single command, `taskunity`, with two subcommands.

```bash
taskunity --help
```

## `taskunity init`

Initialize a new workspace.

```bash
taskunity init [PATH] [--no-sample]
```

| Argument / flag | Default | Description |
| --- | --- | --- |
| `PATH` | `.` | Folder to initialize as a workspace |
| `--no-sample` | off | Skip creating the sample task |

Creates `programs/`, `projects/`, `tasks/`, `milestones/`, and `assets/` directories plus a README
stub. It does not create starter projects or tasks.

## `taskunity serve`

Serve the local web UI for a workspace.

```bash
taskunity serve [--workspace PATH] [--host HOST] [--port PORT] [--reload]
```

| Flag | Default | Description |
| --- | --- | --- |
| `--workspace` | `.` | Workspace folder to serve |
| `--host` | `127.0.0.1` | Host interface to bind |
| `--port` | `8000` | Port to listen on |
| `--reload` | off | Enable uvicorn auto-reload (development only) |

Example:

```bash
taskunity serve --workspace ./my-workboard --host 127.0.0.1 --port 8000
```

Then open `http://127.0.0.1:8000`.
</content>
