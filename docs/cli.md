# Command-line reference

Taskwright exposes a single command, `taskwright`, with two subcommands.

```bash
taskwright --help
```

## `taskwright init`

Initialize a new workspace.

```bash
taskwright init [PATH] [--no-sample]
```

| Argument / flag | Default | Description |
| --- | --- | --- |
| `PATH` | `.` | Folder to initialize as a workspace |
| `--no-sample` | off | Skip creating the sample task |

Creates a `programs/` directory (including `default.json`), a `tasks/` directory, and (unless
`--no-sample`) a starter task.

## `taskwright serve`

Serve the local web UI for a workspace.

```bash
taskwright serve [--workspace PATH] [--host HOST] [--port PORT] [--reload]
```

| Flag | Default | Description |
| --- | --- | --- |
| `--workspace` | `.` | Workspace folder to serve |
| `--host` | `127.0.0.1` | Host interface to bind |
| `--port` | `8000` | Port to listen on |
| `--reload` | off | Enable uvicorn auto-reload (development only) |

Example:

```bash
taskwright serve --workspace ./my-workboard --host 127.0.0.1 --port 8000
```

Then open `http://127.0.0.1:8000`.
</content>
