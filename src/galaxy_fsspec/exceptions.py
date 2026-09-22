"""Exceptions for galaxy-fsspec."""

from __future__ import annotations


class GalaxyFsspecError(OSError):
    """Base error for galaxy-fsspec.

    Deriving from OSError is deliberate. fsspec's own helpers, ``walk`` and
    ``find`` among them, catch ``(FileNotFoundError, OSError)`` and skip the
    entry; anything else escapes and takes the whole traversal with it.
    """


class NotFoundError(GalaxyFsspecError, FileNotFoundError):
    """A path does not map to any Galaxy object."""


class ReadOnlyError(GalaxyFsspecError, PermissionError):
    """galaxy-fsspec is read-only; a write was attempted."""


class GalaxyApiError(GalaxyFsspecError):
    """A wrapped error from a Galaxy API call."""
