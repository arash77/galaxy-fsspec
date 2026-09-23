"""Buffered, read-only file handle backed by a Galaxy dataset."""

from __future__ import annotations

from fsspec.spec import AbstractBufferedFile


class GalaxyFile(AbstractBufferedFile):
    """A read-only buffered file streaming bytes from a Galaxy dataset.

    The owning filesystem is responsible for providing a ``fetch_range`` callable
    that returns the bytes for ``[start, end)`` of the underlying dataset.
    """

    def __init__(self, fs, path, mode="rb", block_size=8 << 20, *, details, **kwargs):
        if mode not in ("rb", "r"):
            from galaxy_fsspec.exceptions import ReadOnlyError

            raise ReadOnlyError(f"galaxy-fsspec only supports read mode, got {mode!r}")
        super().__init__(
            fs,
            path,
            mode=mode,
            block_size=block_size,
            cache_type=kwargs.pop("cache_type", "bytes"),
            size=details["size"],
            **kwargs,
        )
        # What the filesystem resolved on opening, so reading never depends on its caches.
        self.details = details

    def _fetch_range(self, start: int, end: int) -> bytes:
        return self.fs._fetch_dataset_range(self.details, start, end)

    def _open(self, *args, **kwargs):  # pragma: no cover - exercised by AbstractBufferedFile
        # Required by AbstractBufferedFile; we do not open a second handle.
        return self
