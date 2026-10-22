"""Data files the package reads at runtime must ship with it.

Regression: ``openapi_examples.json`` was missing from ``package-data``. An
editable install (development, tests) found it on disk, so everything passed;
the Docker image installs the package normally and served ReDoc without a
single example.
"""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
PACKAGE = BACKEND / "src" / "hedgelab"


def test_every_runtime_data_file_is_declared_as_package_data() -> None:
    config = tomllib.loads((BACKEND / "pyproject.toml").read_text())
    patterns = config["tool"]["setuptools"]["package-data"]["hedgelab"]
    data_files = [
        p.relative_to(PACKAGE).as_posix()
        for p in PACKAGE.rglob("*")
        if p.is_file() and p.suffix not in {".py", ".pyc"} and "__pycache__" not in p.parts
    ]
    undeclared = [f for f in data_files if not any(fnmatch.fnmatch(f, pat) for pat in patterns)]
    assert data_files, "expected YAML specs and the OpenAPI examples"
    assert undeclared == []
