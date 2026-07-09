"""Exceptions for galaxy-fsspec."""

from __future__ import annotations


class GalaxyFsspecError(Exception):
    """Base error for galaxy-fsspec."""


class NotFoundError(GalaxyFsspecError, FileNotFoundError):
    """A path does not map to any Galaxy object."""


class ReadOnlyError(GalaxyFsspecError, PermissionError):
    """galaxy-fsspec is read-only; a write was attempted."""


class GalaxyApiError(GalaxyFsspecError):
    """A wrapped error from a Galaxy API call."""
