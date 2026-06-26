# Development

## Setup

```bash
git clone https://github.com/briday1/taskwright.git
cd taskwright
pip install -e ".[dev]"
```

## Running locally

```bash
taskwright serve --workspace ./my-workboard --reload
```

`--reload` enables uvicorn auto-reload so Python changes are picked up without restarting. Template,
CSS, and JS changes are served on the next request (hard-refresh the browser to bust caches for
`app.css` and `htmx.min.js`).

## Tests & linting

```bash
pytest
ruff check .
```

## Project layout

```text
src/taskwright/
  app.py          # FastAPI app factory, routes, context builder
  cli.py          # `taskwright` CLI (init / serve)
  models.py       # Pydantic models (Task, etc.)
  render.py       # sorting, filtering, dashboard / timeline / calendar builders
  task_store.py   # workspace file I/O, task CRUD, git integration
  templates/      # Jinja2 templates and HTMX partials
  static/         # CSS and the lightweight client script
```

## Building the docs

```bash
pip install -e ".[docs]"
sphinx-build -b html docs docs/_build/html
```

Open `docs/_build/html/index.html`. On Read the Docs the build is driven by `.readthedocs.yaml`.
</content>
