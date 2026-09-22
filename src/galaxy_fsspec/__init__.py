"""galaxy-fsspec: an fsspec filesystem for Galaxy histories, datasets, and collections."""

import importlib.metadata

from galaxy_fsspec.fs import GalaxyFileSystem

__all__ = ["GalaxyFileSystem"]

try:
    # pyproject.toml is the single authored source of the version; at runtime we
    # read whatever was actually installed so the two can never drift.
    __version__ = importlib.metadata.version("galaxy-fsspec")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover (uninstalled source tree)
    __version__ = "0.0.0+unknown"
