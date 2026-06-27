"""Sphinx configuration for Taskunity documentation."""

from __future__ import annotations

from importlib.metadata import version as _pkg_version

project = "Taskunity"
author = "Taskunity Contributors"
copyright = "2026, Taskunity Contributors"

try:
    release = _pkg_version("taskunity")
except Exception:  # pragma: no cover - docs may build without install
    release = "0.1.0"
version = release

extensions = [
    "myst_parser",
]

myst_enable_extensions = [
    "colon_fence",
    "deflist",
]

source_suffix = {
    ".md": "markdown",
    ".rst": "restructuredtext",
}

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

html_theme = "furo"
html_title = f"Taskunity {release}"
html_static_path = ["_static"]
