"""The authored version lives only in pyproject.toml; everything else derives."""

from __future__ import annotations

import importlib
import importlib.metadata
from pathlib import Path

import pytest

import galaxy_fsspec

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10, where tomli is a dev dependency
    import tomli as tomllib

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _declared_version() -> str:
    if not PYPROJECT.is_file():
        pytest.skip("pyproject.toml is not available (installed distribution)")
    with PYPROJECT.open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def test_the_version_is_written_once_and_read_everywhere():
    installed = importlib.metadata.version("galaxy-fsspec")
    assert galaxy_fsspec.__version__ == installed == _declared_version()
