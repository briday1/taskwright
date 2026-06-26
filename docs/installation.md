# Installation

Taskwright requires **Python 3.10 or newer**.

## Install from source

A PyPI release will come later. For now, install from a clone of the repository:

```bash
git clone https://github.com/briday1/taskwright.git
cd taskwright
pip install -e .
```

This installs the `taskwright` command-line entry point along with its dependencies (FastAPI,
uvicorn, Jinja2, Pydantic, and python-multipart).

## Verify the install

```bash
taskwright --help
```

You should see the `init` and `serve` subcommands.

## Optional extras

Install development tooling (tests + linter):

```bash
pip install -e ".[dev]"
```

Install the documentation toolchain (Sphinx + MyST + Furo theme):

```bash
pip install -e ".[docs]"
```

## Git (optional but recommended)

Taskwright's sync features shell out to `git`. If you want the in-app git status chip and the
one-click sync button to work, make sure `git` is installed and on your `PATH`, and that your
workspace folder is a git repository with a configured remote.
</content>
