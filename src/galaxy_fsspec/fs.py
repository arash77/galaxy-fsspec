"""The Galaxy fsspec filesystem."""

from __future__ import annotations

import datetime as dt
import urllib.parse
from collections.abc import Iterable
from typing import Any, cast

import requests
from fsspec.spec import AbstractFileSystem
from requests import PreparedRequest

from galaxy_fsspec.client import (
    DEFAULT_TIMEOUT,
    build_galaxy_instance,
    show_hid_in_names_from_env,
)
from galaxy_fsspec.exceptions import GalaxyApiError, NotFoundError, ReadOnlyError
from galaxy_fsspec.file import GalaxyFile
from galaxy_fsspec.paths import (
    dedupe_names,
    sanitize_segment,
)

ROOT = ""
HISTORIES_DIR = "histories"
LIBRARIES_DIR = "libraries"
_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# Galaxy's own Dataset.no_data_states. A dataset in one of these can still list a nonzero
# size, e.g. partial bytes where jobs write straight into the object store.
_NO_DATA_STATES = frozenset(
    {"new", "upload", "queued", "running", "setting_metadata", "paused", "deferred", "discarded"}
)


class GalaxyFileSystem(AbstractFileSystem):
    """Read-only fsspec filesystem exposing a Galaxy account.

    Layout::

        galaxy://
        ├── histories/
        │   └── <history>/
        │       ├── <dataset>
        │       └── <collection>/   (list, paired, nested ...)
        │           └── ...
        └── libraries/
            └── <library>/
                ├── <dataset>
                └── <folder>/
                    └── ...

    History folders expose ``created`` and ``mtime`` timestamps via
    :meth:`info`. Set ``show_hid_in_names=True`` (or ``GALAXY_FSSPEC_SHOW_HID_IN_NAMES=true``)
    to prefix every entry with its Galaxy ``hid`` in the style ``1-my-dataset``.
    """

    protocol = "galaxy"
    root_marker = ""
    # fsspec tokenizes arguments before __init__ reads the key from the environment, so two
    # callers with different keys would otherwise share one instance.
    cachable = False

    def __init__(
        self,
        *,
        url: str | None = None,
        api_key: str | None = None,
        show_hid_in_names: bool | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.timeout: float = DEFAULT_TIMEOUT if timeout is None else float(timeout)
        self.gi = build_galaxy_instance(url=url, api_key=api_key, timeout=self.timeout)
        # Always the normalised URL, never the raw argument: bioblend strips a
        # trailing slash and supplies a missing scheme, and every request we
        # build by hand has to agree with the ones bioblend makes.
        self._url: str = str(self.gi.base_url)
        self._key: str = str(api_key or self.gi.key)
        # Only this origin may receive the Galaxy API key.
        self._origin: tuple[str, str, int | None] = _origin(self._url)
        self.show_hid_in_names: bool = (
            show_hid_in_names if show_hid_in_names is not None else show_hid_in_names_from_env()
        )
        # Listings live in self.dircache, which honours fsspec's cache options.
        # path -> info dict, carrying the size _open resolved
        self._info_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------ #
    # Public fsspec API
    # ------------------------------------------------------------------ #

    def ls(self, path: str, detail: bool = True, **kwargs: Any) -> list:
        """List a directory. Returns entry dicts, or names with ``detail=False``."""
        entries = self._ls(path)
        if detail:
            return entries
        return [e["name"] for e in entries]

    def _ls(self, path: str) -> list[dict]:
        path = self._strip_protocol(path)
        cached = self.dircache.get(path)
        if cached is not None:
            return cast(list[dict], cached)
        entries = self._list(path)
        # Keep what was just built rather than reading it back. With caching off, or an entry
        # already expired, the write keeps nothing and the read raises KeyError.
        self.dircache[path] = entries
        return entries

    def to_dict(self, *, include_password: bool = True) -> dict[str, Any]:
        """Serialise the filesystem; ``include_password=False`` also drops ``api_key``."""
        serialised = super().to_dict(include_password=include_password)
        if not include_password:
            serialised.pop("api_key", None)
        return serialised

    def info(self, path: str, **kwargs: Any) -> dict:
        return self._info(path, **kwargs)

    def created(self, path: str) -> dt.datetime:
        """Return when the Galaxy object at ``path`` was created."""
        return self._timestamp(path, "created")

    def modified(self, path: str) -> dt.datetime:
        """Return when the Galaxy object at ``path`` last changed."""
        return self._timestamp(path, "mtime")

    def _timestamp(self, path: str, key: str) -> dt.datetime:
        value = self._info(path).get(key)
        if value is None:
            raise GalaxyApiError(f"Galaxy reported no {key} for {path!r}")
        seconds = _as_epoch(value)
        if seconds is None:
            raise GalaxyApiError(f"Galaxy reported an unparsable {key} for {path!r}: {value!r}")
        return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)

    def _info(self, path: str, **kwargs: Any) -> dict:
        path = self._strip_protocol(path)
        if self._cache_ok() and path in self._info_cache:
            return self._info_cache[path]
        if path == ROOT:
            return {"name": ROOT, "size": 0, "type": "directory"}
        parent, _, leaf = path.rpartition("/")
        if leaf == HISTORIES_DIR and parent == ROOT:
            return {"name": HISTORIES_DIR, "size": 0, "type": "directory"}
        if leaf == LIBRARIES_DIR and parent == ROOT:
            return {"name": LIBRARIES_DIR, "size": 0, "type": "directory"}
        # Find this entry within its parent listing.
        try:
            parent_entries = self._ls(parent or ROOT)
        except NotFoundError as exc:
            raise NotFoundError(path) from exc
        for entry in parent_entries:
            if entry["name"] == path:
                # Enrich history folders with timestamps on demand.
                if parent == HISTORIES_DIR and entry.get("created") is None:
                    hist = self.gi.histories.show_history(entry["history_id"], contents=False)
                    entry["created"] = _as_epoch(hist.get("create_time"))
                    entry["mtime"] = _as_epoch(hist.get("update_time"))
                return entry
        raise NotFoundError(path)

    def _open(
        self,
        path: str,
        mode: str = "rb",
        block_size: int | None = None,
        autocommit: bool = True,
        cache_type: str = "bytes",
        **kwargs: Any,
    ) -> GalaxyFile:
        if mode not in ("rb", "r"):
            raise ReadOnlyError(f"galaxy-fsspec is read-only; cannot open {mode!r}")
        info = self._info(path)
        if info["type"] != "file":
            raise IsADirectoryError(path)
        if info.get("state") in _NO_DATA_STATES:
            raise GalaxyApiError(f"{path!r} has no data to read yet (state {info['state']!r})")
        # A listed size of 0 means empty or not known; the details say which.
        if info.get("size", 0) == 0:
            if "library_dataset_id" in info:
                ldda_id, size = self._library_dataset_details(
                    info["library_id"], info["library_dataset_id"]
                )
                info = {**info, "size": size, "ldda_id": ldda_id}
            else:
                info = {**info, "size": self._dataset_details(info.get("dataset_id"))}
            if self._cache_ok():
                self._info_cache[path] = info
        return GalaxyFile(
            self,
            path,
            mode="rb",
            block_size=block_size or (8 << 20),
            cache_type=cache_type,
            details=info,
            **kwargs,
        )

    def _show_dataset(self, dataset_id: str | None) -> dict:
        """Fetch dataset metadata; a failed lookup must not read as an empty file."""
        if not dataset_id:
            raise GalaxyApiError("Galaxy returned a dataset entry without an id")
        try:
            return dict(self.gi.datasets.show_dataset(dataset_id))
        except Exception as exc:
            raise GalaxyApiError(
                f"failed to fetch metadata for dataset {dataset_id}: {exc}"
            ) from exc

    @staticmethod
    def _require_file_size(details: dict, dataset_id: str) -> int:
        """Return the reported size. Galaxy reports 0 for a dataset whose size it does not know,
        so 0 is only believed when the state is ok."""
        size = _to_int(details.get("file_size"))
        if size is None:
            raise GalaxyApiError(
                f"Galaxy did not report a file_size for dataset {dataset_id} "
                f"(state={details.get('state')!r})"
            )
        state = details.get("state")
        if size == 0 and state is not None and state != "ok":
            raise GalaxyApiError(
                f"dataset {dataset_id} is in state {state!r}, so its size is not "
                f"known yet; reading it would return an empty file"
            )
        return size

    def _dataset_details(self, dataset_id: str | None) -> int:
        """Return a history dataset's size from the datasets API."""
        return self._require_file_size(self._show_dataset(dataset_id), str(dataset_id))

    def _library_dataset_details(self, library_id: str, dataset_id: str) -> tuple[str, int]:
        """Return ``(ldda_id, file_size)`` for a library dataset.

        Listings give LibraryDataset ids, but the bytes live under an LDDA id; decoding one as
        the other finds a different dataset rather than failing. Only this endpoint maps them.
        """
        try:
            details = self.gi.libraries.show_dataset(library_id, dataset_id)
        except Exception as exc:
            raise GalaxyApiError(
                f"failed to fetch metadata for library dataset {dataset_id}: {exc}"
            ) from exc
        size = self._require_file_size(details, str(dataset_id))
        ldda_id = details.get("ldda_id")
        if not ldda_id:
            raise GalaxyApiError(
                f"Galaxy did not report an ldda_id for library dataset {dataset_id}"
            )
        return str(ldda_id), size

    def _fetch_dataset_range(self, info: dict, start: int, end: int) -> bytes:
        if "ldda_id" in info:
            return self._download_range(info["ldda_id"], start, end, hda_ldda="ldda")
        dataset_id = info.get("dataset_id")
        if not dataset_id:
            raise GalaxyApiError(f"no Galaxy dataset id is known for {info.get('name')!r}")
        return self._download_range(dataset_id, start, end)

    # Read-only enforcement -------------------------------------------------
    def _rm(self, path):
        raise ReadOnlyError("galaxy-fsspec is read-only")

    def rm_file(self, path):
        raise ReadOnlyError("galaxy-fsspec is read-only")

    def rm(self, path, recursive=False, maxdepth=None):
        raise ReadOnlyError("galaxy-fsspec is read-only")

    def mkdir(self, path, create_parents=True, **kwargs):
        raise ReadOnlyError("galaxy-fsspec is read-only")

    def makedirs(self, path, exist_ok=False):
        raise ReadOnlyError("galaxy-fsspec is read-only")

    def pipe_file(self, path, value, **kwargs):
        raise ReadOnlyError("galaxy-fsspec is read-only")

    def touch(self, path, **kwargs):
        raise ReadOnlyError("galaxy-fsspec is read-only")

    def rmdir(self, path):
        raise ReadOnlyError("galaxy-fsspec is read-only")

    def cp_file(self, path1, path2, **kwargs):
        raise ReadOnlyError("galaxy-fsspec is read-only")

    def copy(self, path1, path2, recursive=False, maxdepth=None, **kwargs):
        raise ReadOnlyError("galaxy-fsspec is read-only")

    def mv(self, path1, path2, recursive=False, maxdepth=None, **kwargs):
        raise ReadOnlyError("galaxy-fsspec is read-only")

    # ------------------------------------------------------------------ #
    # Path resolution
    # ------------------------------------------------------------------ #

    @classmethod
    def _strip_protocol(cls, path: str) -> str:
        """Like fsspec's, but also drops a leading slash: paths are relative to the account."""
        stripped = super()._strip_protocol(path)
        return stripped.lstrip("/") or ROOT

    def invalidate_cache(self, path: str | None = None) -> None:
        """Empty the caches at or under ``path``, or all of them; the base class empties nothing."""
        super().invalidate_cache(path)
        if path is None:
            self.dircache.clear()
            self._info_cache.clear()
            return
        # "at or under given path", per the base class contract.
        prefix = self._strip_protocol(path)
        for cached in [p for p in list(self.dircache) if p == prefix or p.startswith(f"{prefix}/")]:
            del self.dircache[cached]
        for cached in [p for p in self._info_cache if p == prefix or p.startswith(f"{prefix}/")]:
            del self._info_cache[cached]

    def _cache_ok(self) -> bool:
        """Whether the caches that are not ``dircache`` may answer at all."""
        return bool(self.dircache.use_listings_cache)

    def _list(self, path: str) -> list[dict]:
        if path == ROOT:
            return [
                {
                    "name": HISTORIES_DIR,
                    "size": 0,
                    "type": "directory",
                },
                {
                    "name": LIBRARIES_DIR,
                    "size": 0,
                    "type": "directory",
                },
            ]
        segments = path.split("/")
        head = segments[0]
        if head == HISTORIES_DIR:
            rest = segments[1:]
            if not rest:
                return self._list_histories()
            history = self._resolve_history(rest[0])
            if len(rest) == 1:
                return self._list_history_contents(history, path)
            return self._list_collection_path(history, rest[1:], path)
        if head == LIBRARIES_DIR:
            rest = segments[1:]
            if not rest:
                return self._list_libraries()
            library = self._resolve_library(rest[0])
            if len(rest) == 1:
                return self._list_library_contents(library, path)
            return self._list_library_path(library, rest[1:], path)
        raise NotFoundError(path)

    # ------------------------------------------------------------------ #
    # Histories
    # ------------------------------------------------------------------ #

    def _list_histories(self) -> list[dict]:
        raw = self.gi.histories.get_histories()
        named = dedupe_names(raw, numbered=self.show_hid_in_names)
        entries: list[dict] = []
        for display, h in named:
            entries.append(
                {
                    "name": f"{HISTORIES_DIR}/{display}",
                    "size": 0,
                    "type": "directory",
                    "history_id": h["id"],
                    "hid": None,
                    **_timestamp_keys(h.get("create_time"), h.get("update_time")),
                }
            )
        return entries

    def _resolve_history(self, segment: str) -> dict:
        histories = self.gi.histories.get_histories()
        # Resolve through the same deduplicated display names _list_histories
        # emits, so every path returned by ls() can be browsed.
        named = dedupe_names(histories, numbered=self.show_hid_in_names)
        for display, h in named:
            if segment == display:
                return h
        # Then the raw name, which the display form may have numbered, and
        # finally the raw id.
        matched = [
            (display, h) for display, h in named if segment == sanitize_segment(h.get("name") or "")
        ]
        if len(matched) == 1:
            return matched[0][1]
        if matched:
            raise NotFoundError(_ambiguous(f"{HISTORIES_DIR}/{segment}", matched))
        for h in histories:
            if h["id"] == segment:
                return h
        raise NotFoundError(f"histories/{segment}")

    # ------------------------------------------------------------------ #
    # Libraries
    # ------------------------------------------------------------------ #

    def _list_libraries(self) -> list[dict]:
        raw = self.gi.libraries.get_libraries()
        named = dedupe_names(raw, numbered=False)
        entries: list[dict] = []
        for display, lib in named:
            entries.append(
                {
                    "name": f"{LIBRARIES_DIR}/{display}",
                    "size": 0,
                    "type": "directory",
                    "library_id": lib["id"],
                }
            )
        return entries

    def _resolve_library(self, segment: str) -> dict:
        libraries = self.gi.libraries.get_libraries()
        # Mirror _list_libraries: libraries have no hid, so never numbered.
        named = dedupe_names(libraries, numbered=False)
        for display, lib in named:
            if segment == display:
                return lib
        matched = [
            (display, lib)
            for display, lib in named
            if segment == sanitize_segment(lib.get("name") or "")
        ]
        if len(matched) == 1:
            return matched[0][1]
        if matched:
            raise NotFoundError(_ambiguous(f"{LIBRARIES_DIR}/{segment}", matched))
        for lib in libraries:
            if lib["id"] == segment:
                return lib
        raise NotFoundError(f"libraries/{segment}")

    def _list_library_contents(self, library: dict, path: str) -> list[dict]:
        """List the root folder of a library."""
        return self._list_library_path(library, [], path)

    def _folder_contents(self, folder_id: str) -> list[dict]:
        """Return one folder's contents, every page of it; ``show_folder`` stops at ten."""
        return list(self.gi.folders.contents_iter(folder_id))

    def _library_root_folder_id(self, library: dict) -> str:
        folder_id = library.get("root_folder_id")
        if not folder_id:
            raise GalaxyApiError(
                f"Galaxy did not say which folder library {library.get('id')!r} starts at"
            )
        return str(folder_id)

    def _list_library_path(self, library: dict, segments: list[str], path: str) -> list[dict]:
        """List one folder inside a library, walking the folders API from the library root."""
        folder_id = self._library_root_folder_id(library)
        walked: list[str] = []
        for segment in segments:
            children = self._folder_contents(folder_id)
            match = self._match_folder_child(children, segment, walked, path)
            folder_id = str(match["id"])
            walked.append(segment)
        return self._folder_entries(self._folder_contents(folder_id), library, path)

    def _match_folder_child(
        self, children: list[dict], segment: str, walked: list[str], path: str
    ) -> dict:
        """Find the sub-folder a path segment names."""
        folders = [child for child in children if child.get("type") == "folder"]
        named = dedupe_names(folders, numbered=False)
        for display, child in named:
            if display == segment:
                return child
        matched = [
            (display, child)
            for display, child in named
            if segment == sanitize_segment(child.get("name") or "")
        ]
        if len(matched) == 1:
            return matched[0][1]
        if matched:
            raise NotFoundError(_ambiguous(path, matched))
        raise NotFoundError(path)

    def _folder_entries(self, children: list[dict], library: dict, path: str) -> list[dict]:
        """Turn one folder's contents into fsspec entries, one unique path each.

        A file is deduplicated against every child, a folder only against other folders, which is
        the name _match_folder_child resolves.
        """
        folder_names = {
            id(child): display
            for display, child in dedupe_names(
                [c for c in children if c.get("type") == "folder"], numbered=False
            )
        }
        deduped = iter(
            display
            for display, _ in dedupe_names(
                [{"name": c.get("name") or "", "id": c.get("id")} for c in children],
                numbered=False,
            )
        )
        entries: list[dict] = []
        for child in children:
            is_folder = child.get("type") == "folder"
            candidate = next(deduped)
            display = folder_names[id(child)] if is_folder else candidate
            entry: dict = {
                "name": f"{path}/{display}",
                "type": "directory" if is_folder else "file",
                # raw_size, never file_size: that one is a human readable string from nice_size.
                "size": 0 if is_folder else _to_int(child.get("raw_size")) or 0,
            }
            created = _as_epoch(child.get("create_time"))
            changed = _as_epoch(child.get("update_time"))
            if created is not None:
                entry["created"] = created
            if changed is not None:
                entry["mtime"] = changed
            if is_folder:
                entry["library_folder_id"] = child["id"]
            else:
                entry["library_dataset_id"] = child["id"]
                entry["library_id"] = library["id"]
                if child.get("ldda_id"):
                    entry["ldda_id"] = child["ldda_id"]
                entry["state"] = child.get("state")
            entries.append(entry)
        return entries

    # ------------------------------------------------------------------ #
    # History contents
    # ------------------------------------------------------------------ #

    #: The summary listing has no ``file_size``. ``details`` brings it back; ``keys=[...]`` does
    #: not, because Galaxy ignores it.
    _CONTENTS_DETAILS = "all"

    def _history_contents(self, history_id: str) -> list[dict]:
        """Return what the history panel shows, with sizes, in one request.

        Without the filters Galaxy also returns deleted datasets, and the hidden copies it
        makes of every file put into a collection, which then appeared twice.
        """
        return list(
            self.gi.histories.show_history(
                history_id,
                contents=True,
                deleted=False,
                visible=True,
                details=self._CONTENTS_DETAILS,
            )
        )

    def _list_history_contents(self, history: dict, path: str) -> list[dict]:
        contents = self._history_contents(history["id"])
        return self._contents_to_entries(contents, path)

    def _contents_to_entries(self, contents: Iterable[dict], parent_path: str) -> list[dict]:
        named = dedupe_names(list(contents), numbered=self.show_hid_in_names)
        entries: list[dict] = []
        for display, item in named:
            is_collection = item.get("history_content_type") == "dataset_collection"
            entry: dict = {
                "name": f"{parent_path}/{display}",
                "type": "directory" if is_collection else "file",
                "hid": _to_int(item.get("hid")),
            }
            # The listing carries these already, no detail request needed for them. Galaxy's own
            # fsspec file source reads mtime and never last_modified, so an entry without it shows
            # no date at all in the file browser.
            created = _as_epoch(item.get("create_time"))
            changed = _as_epoch(item.get("update_time"))
            if created is not None:
                entry["created"] = created
            if changed is not None:
                entry["mtime"] = changed
            if is_collection:
                entry["size"] = 0
                entry["collection_id"] = item["id"]
                entry["collection_type"] = item.get("collection_type")
            else:
                entry["size"] = _to_int(item.get("file_size") or item.get("size")) or 0
                entry["dataset_id"] = item.get("id")
                entry["state"] = item.get("state")
            entries.append(entry)
        return entries

    # ------------------------------------------------------------------ #
    # Collection navigation
    # ------------------------------------------------------------------ #

    def _list_collection_path(self, history: dict, segments: list[str], path: str) -> list[dict]:
        """List the directory at ``path``, which lies inside at least one collection.

        ``segments`` is everything below the history folder; ``segments[0]`` is a
        top-level collection, any later segments descend into nested collections.
        """
        contents = self._history_contents(history["id"])
        current = self._resolve_in_contents(contents, segments[0])
        if not current.get("_is_collection"):
            # A top-level dataset has no children.
            raise NotFoundError(path)
        elements = self._require_elements(current, segments[0])
        # Walk intermediate segments through nested collections. The elements
        # are already in hand at every level, because the history listing asks
        # for details and Galaxy serialises the whole nested tree inline.
        for seg in segments[1:]:
            child = self._resolve_in_elements(elements, seg)
            if not child.get("_is_collection"):
                # Landed on a dataset leaf; no further descent is possible.
                raise NotFoundError(path)
            elements = self._require_elements(child, seg)
        return self._elements_to_entries(elements, path)

    @staticmethod
    def _require_elements(collection: dict, name: str) -> list[dict]:
        """Return a collection's elements, which the listing carries inline.

        Do not re-fetch them: a nested ``object.id`` is not an HDCA id, and the fetch would return a
        different collection instead of failing.
        """
        elements = collection.get("elements")
        if elements is None:
            raise GalaxyApiError(
                f"Galaxy did not include the elements of collection {name!r}, "
                f"so its contents cannot be listed"
            )
        return list(elements)

    def _resolve_in_contents(self, contents: list[dict], segment: str) -> dict:
        named = dedupe_names(contents, numbered=self.show_hid_in_names)
        for display, item in named:
            disp_name = display.rsplit("/", 1)[-1]
            if segment == disp_name:
                if item.get("history_content_type") == "dataset_collection":
                    return {
                        "id": item["id"],
                        "_is_collection": True,
                        "elements": item.get("elements"),
                    }
                return {"id": item["id"], "_is_collection": False}
        raise NotFoundError(segment)

    def _resolve_in_elements(self, elements: list[dict], segment: str) -> dict:
        for display, original, _hid in self._name_elements(elements):
            disp_name = display.rsplit("/", 1)[-1]
            if segment != disp_name:
                continue
            inner = _element_inner(original)
            if original.get("element_type") == "dataset_collection":
                return {
                    "id": inner.get("id"),
                    "_is_collection": True,
                    "elements": inner.get("elements"),
                }
            return {"id": inner.get("id"), "_is_collection": False}
        raise NotFoundError(segment)

    def _name_elements(self, elements: list[dict]) -> list[tuple[str, dict, int | None]]:
        """Return ``(display_name, original_element, hid)`` tuples.

        ``hid`` is the 1-based element index (when numbered naming is active),
        used purely so ``dedupe_names`` can build the ``<n>-`` prefix.
        """
        normalized: list[dict] = []
        for el in elements:
            inner = _element_inner(el)
            idx = el.get("element_index")
            normalized.append(
                {
                    "id": inner.get("id") or el.get("element_id"),
                    "name": el.get("element_identifier") or inner.get("name") or "",
                    "hid": (idx + 1) if isinstance(idx, int) else None,
                }
            )
        deduped = dedupe_names(normalized, numbered=self.show_hid_in_names)
        # Re-pair display names with the *original* elements (preserve order).
        return [
            (display, elements[i], normalized[i].get("hid"))
            for i, (display, _item) in enumerate(deduped)
        ]

    def _elements_to_entries(self, elements: list[dict], parent_path: str) -> list[dict]:
        entries: list[dict] = []
        for display, original, hid in self._name_elements(elements):
            inner = _element_inner(original)
            is_collection = original.get("element_type") == "dataset_collection"
            entry: dict = {
                "name": f"{parent_path}/{display}",
                "type": "directory" if is_collection else "file",
                "hid": hid,
            }
            if is_collection:
                entry["size"] = 0
                entry["collection_id"] = inner.get("id")
                entry["collection_type"] = inner.get("collection_type")
            else:
                entry["size"] = _to_int(inner.get("file_size") or inner.get("size")) or 0
                entry["dataset_id"] = inner.get("id")
                entry["state"] = inner.get("state")
            entries.append(entry)
        return entries

    # ------------------------------------------------------------------ #
    # Dataset download (range-aware, with full-download fallback)
    # ------------------------------------------------------------------ #

    def _download_range(
        self, dataset_id: str, start: int, end: int, hda_ldda: str = "hda"
    ) -> bytes:
        """Fetch bytes ``[start, end)`` from a dataset without buffering the
        full response in memory.

        Uses HTTP streaming: for partial-content responses (206) we read only
        the requested chunk; if the server ignores the Range header (200) we
        stream-discard the first ``start`` bytes then read the needed slice.

        ``raw`` asks for the stored file; rendering one fails for library datasets.
        """
        if end <= start:
            return b""
        query = {"raw": "true"}
        if hda_ldda != "hda":
            query["hda_ldda"] = hda_ldda
        url = (
            f"{self._url}/api/datasets/{urllib.parse.quote(dataset_id)}/display"
            f"?{urllib.parse.urlencode(query)}"
        )
        return self._download_from_url(url, start, end, dataset_id)

    def _is_galaxy_origin(self, url: str) -> bool:
        """Whether ``url`` may be sent the Galaxy API key: the Galaxy origin, or the same host
        upgraded from http to https."""
        scheme, host, port = _origin(url)
        galaxy_scheme, galaxy_host, galaxy_port = self._origin
        if not host or host != galaxy_host:
            return False
        if (scheme, port) == (galaxy_scheme, galaxy_port):
            return True
        return (
            galaxy_scheme == "http"
            and scheme == "https"
            and galaxy_port == _DEFAULT_PORTS["http"]
            and port == _DEFAULT_PORTS["https"]
        )

    def _request_range(self, url: str, start: int, end: int) -> requests.Response:
        """GET ``[start, end)`` of ``url``, following redirects by hand.

        requests keeps a custom header like ``x-api-key`` across a redirect to another host, so each
        hop decides again whether the key may go.
        """
        range_header = f"bytes={start}-{end - 1}"
        for _hop in range(_MAX_REDIRECTS + 1):
            # Normalise before deciding anything about this URL: a redirect target
            # is Galaxy's word, not ours.
            url = _as_requests_will_fetch(url)
            headers = {"Range": range_header}
            if self._is_galaxy_origin(url):
                headers["x-api-key"] = self._key
            resp = requests.get(
                url,
                headers=headers,
                timeout=self.timeout,
                stream=True,
                allow_redirects=False,
            )
            if resp.status_code not in _REDIRECT_STATUSES:
                return resp
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                raise GalaxyApiError(f"redirect from {url} carried no Location header")
            try:
                url = urllib.parse.urljoin(url, location)
                scheme = urllib.parse.urlsplit(url).scheme
            except ValueError as exc:
                raise GalaxyApiError(
                    f"refusing to follow an unparsable redirect to {location!r}: {exc}"
                ) from exc
            if scheme not in ("http", "https"):
                raise GalaxyApiError(f"refusing to follow redirect to {url!r}")
        raise GalaxyApiError(f"too many redirects (>{_MAX_REDIRECTS}) while downloading {url}")

    def _download_from_url(self, url: str, start: int, end: int, label: str) -> bytes:
        length = end - start
        resp = self._request_range(url, start, end)
        try:
            if resp.status_code == 206:
                return _read_stream(resp, length)
            if resp.status_code == 200:
                return _skip_then_read_stream(resp, start, length)
            if resp.status_code in (401, 403):
                raise ReadOnlyError(f"Galaxy refused dataset access: {resp.status_code}")
            if resp.status_code == 404:
                raise NotFoundError(f"dataset {label} (HTTP 404)")
            # Anything else is Galaxy failing, not the dataset missing.
            raise GalaxyApiError(
                f"Galaxy could not serve dataset {label} (HTTP {resp.status_code})"
            )
        finally:
            resp.close()


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _as_requests_will_fetch(url: str) -> str:
    """Return the URL ``requests`` will actually fetch, so the origin check judges that one.

    urllib and urllib3 disagree about a backslash in the authority: ``https://evil.com\\@galaxy.example/``
    is galaxy.example to one and evil.com to the other.
    """
    prepared = PreparedRequest()
    try:
        prepared.prepare_url(url, None)
    except Exception as exc:
        raise GalaxyApiError(f"refusing to fetch an unparsable URL {url!r}: {exc}") from exc
    if not prepared.url:
        raise GalaxyApiError(f"refusing to fetch an unparsable URL {url!r}")
    return str(prepared.url)


def _timestamp_keys(created: object, changed: object) -> dict[str, float]:
    """The timestamp keys an entry should carry, leaving out the ones Galaxy did not send."""
    keys: dict[str, float] = {}
    born = _as_epoch(created)
    touched = _as_epoch(changed)
    if born is not None:
        keys["created"] = born
    if touched is not None:
        keys["mtime"] = touched
    return keys


def _as_epoch(value: object) -> float | None:
    """Turn a Galaxy timestamp into seconds since the epoch, or None.

    A number, as fsspec's LocalFileSystem uses: Galaxy's file source cannot format a string.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    # Python 3.10's fromisoformat rejects a trailing "Z", which Galaxy emits whenever its
    # timestamps are serialised as timezone aware.
    if text[-1:] in ("Z", "z"):
        text = f"{text[:-1]}+00:00"
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    # Galaxy records these in UTC and serialises them without an offset, so a naive value is UTC.
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.timestamp()


def _origin(url: str) -> tuple[str, str, int | None]:
    """Return the ``(scheme, host, port)`` origin of ``url``, lowercased.

    The port is resolved to its default for the scheme, so ``https://host`` and
    ``https://host:443`` compare equal.
    """
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    try:
        port = parsed.port
    except ValueError:  # malformed port
        port = None
    return scheme, (parsed.hostname or "").lower(), port or _DEFAULT_PORTS.get(scheme)


def _ambiguous(path: str, matched: list[tuple[str, dict]]) -> str:
    """Message for a plain name that more than one object answers to; picking one would depend
    on the order Galaxy lists them in."""
    offered = ", ".join(sorted(display for display, _item in matched))
    return f"{path} is ambiguous: {len(matched)} objects share that name. Use one of: {offered}"


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _element_inner(element: dict) -> dict:
    """Return the inner object describing a collection element.

    Galaxy's ``show_dataset_collection`` returns each element with the nested
    dataset/collection under the ``"object"`` key (see bioblend's own tests:
    ``element["object"]["id"]``). Some older payloads used ``"element"``;
    accept both for robustness.
    """
    inner = element.get("object")
    if inner is None:
        inner = element.get("element") or {}
    return inner if isinstance(inner, dict) else {}


def _read_stream(resp: requests.Response, length: int) -> bytes:
    """Read exactly ``length`` bytes from a streaming response in chunks."""
    chunks: list[bytes] = []
    remaining = length
    for chunk in resp.iter_content(chunk_size=64 << 10):
        if not chunk:
            continue
        if remaining <= 0:
            break
        if len(chunk) > remaining:
            chunk = chunk[:remaining]
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _skip_then_read_stream(resp: requests.Response, skip: int, length: int) -> bytes:
    """Stream-discard ``skip`` bytes, then read ``length`` bytes."""
    chunks: list[bytes] = []
    remaining_skip = skip
    remaining_read = length
    for chunk in resp.iter_content(chunk_size=64 << 10):
        if not chunk:
            continue
        offset = 0
        if remaining_skip > 0:
            if len(chunk) <= remaining_skip:
                remaining_skip -= len(chunk)
                continue
            offset = remaining_skip
            remaining_skip = 0
        if remaining_read <= 0:
            break
        take = min(len(chunk) - offset, remaining_read)
        chunks.append(chunk[offset : offset + take])
        remaining_read -= take
    return b"".join(chunks)
