"""Unit tests for GalaxyFileSystem using a fake bioblend client (no network)."""

from __future__ import annotations

import datetime as _dt

import pytest

from galaxy_fsspec.exceptions import GalaxyApiError, NotFoundError, ReadOnlyError
from galaxy_fsspec.fs import GalaxyFileSystem


class FakeHistories:
    def __init__(self, store):
        self.store = store

    def get_histories(self):
        # Realistic: bioblend returns only id + name here.
        return [{"id": h["id"], "name": h["name"]} for h in self.store["histories"]]

    #: Keys the real ``/api/histories/{id}/contents`` withholds from its default
    #: serialization. They live on HDADetailed, which only ``details`` asks for.
    #: Verified against usegalaxy.eu and a 26.2.dev0 server on 2026-09-22: the
    #: default listing carries create_time and update_time but never file_size.
    _DETAIL_ONLY = ("file_size",)

    def show_history(self, history_id, contents=True, details=None, keys=None):
        hist = next(h for h in self.store["histories"] if h["id"] == history_id)
        if not contents:
            return {
                "id": hist["id"],
                "name": hist["name"],
                "create_time": hist["create_time"],
                "update_time": hist["update_time"],
            }
        wants_detail = details in ("all", "true") or (
            isinstance(details, str) and details not in ("", "none")
        )
        items = []
        for item in hist["contents"]:
            if wants_detail:
                items.append(dict(item))
                continue
            # `keys` is deliberately ignored, because Galaxy ignores it too: asking
            # for file_size by name returns the default key set without it.
            items.append({k: v for k, v in item.items() if k not in self._DETAIL_ONLY})
        return items


class FakeDatasetCollections:
    def __init__(self, store):
        self.store = store

    def show_dataset_collection(self, collection_id):
        return {"elements": self.store["collections"][collection_id]}


class FakeDatasets:
    def __init__(self, store):
        self.store = store

    def show_dataset(self, dataset_id, hda_ldda="hda"):
        # Check library dataset sizes first, then history dataset_sizes.
        for lib in self.store.get("libraries", []):
            for item in lib.get("contents", []):
                if item.get("type") == "file" and item.get("id") == dataset_id:
                    return {
                        "id": item.get("ldda_id", dataset_id),
                        "file_size": item.get("file_size", 1024),
                        "state": "ok",
                    }
        sizes = self.store.get("dataset_sizes", {})
        return {
            "id": dataset_id,
            "file_size": sizes.get(dataset_id, 1024),
            "state": "ok",
        }


class FakeLibraries:
    def __init__(self, store):
        self.store = store

    def get_libraries(self):
        return [{"id": lib["id"], "name": lib["name"]} for lib in self.store["libraries"]]

    def show_library(self, library_id, contents=False):
        lib = next(lb for lb in self.store["libraries"] if lb["id"] == library_id)
        if contents:
            return lib["contents"]
        return {"id": lib["id"], "name": lib["name"]}


class FakeGalaxyInstance:
    def __init__(self, store):
        self.base_url = "https://galaxy.example"
        self.key = "test-key"
        self.histories = FakeHistories(store)
        self.dataset_collections = FakeDatasetCollections(store)
        self.datasets = FakeDatasets(store)
        self.libraries = FakeLibraries(store)


class FakeResponse:
    """Minimal stand-in for ``requests.Response`` that records closure."""

    def __init__(self, status_code, body=b"", headers=None):
        self.status_code = status_code
        self.headers = dict(headers or {})
        self._body = body
        self.closed = False

    def iter_content(self, chunk_size=8192):
        for offset in range(0, len(self._body), chunk_size):
            yield self._body[offset : offset + chunk_size]

    def close(self):
        self.closed = True


class RecordingTransport:
    """Stand-in for the ``requests`` module: replays queued responses in order."""

    def __init__(self, *responses):
        self._queue = list(responses)
        self.calls = []
        self.responses = []

    def get(self, url, headers=None, timeout=None, stream=False, allow_redirects=True):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers or {}),
                "allow_redirects": allow_redirects,
            }
        )
        response = self._queue.pop(0)
        self.responses.append(response)
        return response


class ByteRangeTransport:
    """Serves 206 responses slicing a fixed payload per the Range header."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.responses = []

    def get(self, url, headers=None, timeout=None, stream=False, allow_redirects=True):
        headers = dict(headers or {})
        self.calls.append(
            {"url": url, "headers": headers, "allow_redirects": allow_redirects}
        )
        start, end = headers["Range"].removeprefix("bytes=").split("-")
        response = FakeResponse(206, self.payload[int(start) : int(end) + 1])
        self.responses.append(response)
        return response


def _use_transport(monkeypatch, transport):
    import galaxy_fsspec.fs as fsmod

    monkeypatch.setattr(fsmod, "requests", transport)
    return transport


def _store():
    return {
        "histories": [
            {
                "id": "hid1",
                "name": "History A",
                "create_time": "2024-01-01T00:00:00",
                "update_time": "2024-01-02T00:00:00",
                "contents": [
                    {
                        "id": "ds1",
                        "hid": 1,
                        "name": "my-uploaded-dataset",
                        "history_content_type": "dataset",
                        "file_size": 11,
                    },
                    {
                        "id": "coll1",
                        "hid": 2,
                        "name": "my result",
                        "history_content_type": "dataset_collection",
                        "collection_type": "list:paired",
                    },
                ],
            }
        ],
        "collections": {
            "coll1": [
                {
                    "element_type": "dataset_collection",
                    "element_index": 0,
                    "element_identifier": "sample1",
                    "object": {
                        "id": "subcoll1",
                        "name": "sample1",
                        "collection_type": "paired",
                    },
                },
            ],
            "subcoll1": [
                {
                    "element_type": "hda",
                    "element_index": 0,
                    "element_identifier": "forward",
                    "object": {"id": "dsF", "name": "R1"},
                },
                {
                    "element_type": "hda",
                    "element_index": 1,
                    "element_identifier": "reverse",
                    "object": {"id": "dsR", "name": "R2"},
                },
            ],
        },
        "libraries": [
            {
                "id": "lib1",
                "name": "Shared Data",
                "contents": [
                    {"id": "f_root", "type": "folder", "name": "/"},
                    {"id": "f1", "type": "folder", "name": "/genomes"},
                    {"id": "dsL1", "type": "file", "name": "/genomes/hg38.fa",
                     "ldda_id": "ldda1", "file_size": 14},
                    {"id": "dsL2", "type": "file", "name": "/reads.fastq",
                     "ldda_id": "ldda2", "file_size": 10},
                    {"id": "dsL3", "type": "file", "name": "/genomes.txt",
                     "ldda_id": "ldda3", "file_size": 5},
                ],
            }
        ],
    }


def make_fs(store=None, **kwargs):
    kwargs.setdefault("url", "https://galaxy.example")
    kwargs.setdefault("api_key", "test-key")
    kwargs.setdefault("skip_instance_cache", True)
    filesystem = GalaxyFileSystem(**kwargs)
    filesystem.gi = FakeGalaxyInstance(_store() if store is None else store)
    return filesystem


def _store_with_namesakes():
    """A second history and a second library, each named like the first."""
    store = _store()
    store["histories"].append(
        {
            "id": "hid2",
            "name": "History A",
            "create_time": "2024-02-01T00:00:00",
            "update_time": "2024-02-02T00:00:00",
            "contents": [
                {
                    "id": "ds2",
                    "hid": 1,
                    "name": "second-dataset",
                    "history_content_type": "dataset",
                    "file_size": 7,
                }
            ],
        }
    )
    store["libraries"].append(
        {
            "id": "lib2",
            "name": "Shared Data",
            "contents": [
                {"id": "f_root2", "type": "folder", "name": "/"},
                {
                    "id": "dsL9",
                    "type": "file",
                    "name": "/other.txt",
                    "ldda_id": "ldda9",
                    "file_size": 3,
                },
            ],
        }
    )
    return store


@pytest.fixture
def fs():
    return make_fs()


class TestRoot:
    def test_root_lists_histories_dir(self, fs):
        assert "histories" in fs.ls("/", detail=False)

    def test_root_lists_libraries_dir(self, fs):
        assert "libraries" in fs.ls("/", detail=False)

    def test_root_lists_both(self, fs):
        assert set(fs.ls("/", detail=False)) == {"histories", "libraries"}

    def test_histories_dir_info(self, fs):
        info = fs.info("histories")
        assert info["type"] == "directory"


class TestHistories:
    def test_list_histories(self, fs):
        names = fs.ls("histories", detail=False)
        assert names == ["histories/History A"]

    def test_history_info_has_dates(self, fs):
        info = fs.info("histories/History A")
        assert info["type"] == "directory"
        assert info["created"] == "2024-01-01T00:00:00"
        assert info["mtime"] == "2024-01-02T00:00:00"
        assert info["history_id"] == "hid1"


class TestHistoryContents:
    def test_lists_dataset_and_collection(self, fs):
        names = fs.ls("histories/History A", detail=False)
        assert "histories/History A/my-uploaded-dataset" in names
        assert "histories/History A/my result" in names

    def test_dataset_info(self, fs):
        info = fs.info("histories/History A/my-uploaded-dataset")
        assert info["type"] == "file"
        assert info["size"] == 11
        assert info["dataset_id"] == "ds1"
        assert info["hid"] == 1

    def test_a_listing_reports_the_real_size(self, fs):
        """Every entry read 0 bytes before, on every real Galaxy.

        The listing endpoint answers with the summary serialization, which has no
        file_size, so sizes have to be asked for. A file source built on this shows
        the number from here in its browser, and 0 for every file is what a user saw.
        """
        entries = fs.ls("histories/History A", detail=True)
        dataset = next(e for e in entries if e["type"] == "file")
        assert dataset["size"] == 11

    def test_collection_info(self, fs):
        info = fs.info("histories/History A/my result")
        assert info["type"] == "directory"
        assert info["collection_id"] == "coll1"


class TestNestedCollections:
    def test_list_list_paired(self, fs):
        names = fs.ls("histories/History A/my result", detail=False)
        assert names == ["histories/History A/my result/sample1"]

    def test_descend_into_paired(self, fs):
        names = fs.ls("histories/History A/my result/sample1", detail=False)
        assert set(names) == {
            "histories/History A/my result/sample1/forward",
            "histories/History A/my result/sample1/reverse",
        }

    def test_leaf_dataset_info(self, fs):
        info = fs.info("histories/History A/my result/sample1/forward")
        assert info["type"] == "file"
        assert info["dataset_id"] == "dsF"


class TestNumberedNames:
    def fs_numbered(self):
        return make_fs(show_hid_in_names=True)

    def test_history_contents_numbered(self):
        fs = self.fs_numbered()
        names = fs.ls("histories/History A", detail=False)
        assert "histories/History A/1-my-uploaded-dataset" in names
        assert "histories/History A/2-my result" in names

    def test_collection_elements_numbered(self):
        fs = self.fs_numbered()
        names = fs.ls("histories/History A/2-my result", detail=False)
        assert "histories/History A/2-my result/1-sample1" in names

    def test_paired_elements_numbered(self):
        fs = self.fs_numbered()
        names = fs.ls("histories/History A/2-my result/1-sample1", detail=False)
        assert "histories/History A/2-my result/1-sample1/1-forward" in names
        assert "histories/History A/2-my result/1-sample1/2-reverse" in names

    def test_info_resolves_numbered(self):
        fs = self.fs_numbered()
        info = fs.info("histories/History A/1-my-uploaded-dataset")
        assert info["dataset_id"] == "ds1"


class TestReadOnly:
    def test_open_write_raises(self, fs):
        from galaxy_fsspec.exceptions import ReadOnlyError

        with pytest.raises(ReadOnlyError):
            fs.open("histories/History A/my-uploaded-dataset", "wb")

    def test_mkdir_raises(self, fs):
        from galaxy_fsspec.exceptions import ReadOnlyError

        with pytest.raises(ReadOnlyError):
            fs.mkdir("histories/History A/new")

    def test_rm_raises(self, fs):
        from galaxy_fsspec.exceptions import ReadOnlyError

        with pytest.raises(ReadOnlyError):
            fs.rm("histories/History A/my-uploaded-dataset")

    DATASET = "histories/History A/my-uploaded-dataset"

    @pytest.mark.parametrize(
        "operation",
        [
            lambda fs: fs.rmdir("histories/History A"),
            lambda fs: fs.cp_file(TestReadOnly.DATASET, TestReadOnly.DATASET + " copy"),
            lambda fs: fs.copy(TestReadOnly.DATASET, TestReadOnly.DATASET + " copy"),
            lambda fs: fs.mv(TestReadOnly.DATASET, TestReadOnly.DATASET + " moved"),
        ],
        ids=["rmdir", "cp_file", "copy", "mv"],
    )
    def test_every_mutating_call_raises(self, fs, operation):
        from galaxy_fsspec.exceptions import ReadOnlyError

        with pytest.raises(ReadOnlyError):
            operation(fs)


class TestNotFound:
    def test_missing_history(self, fs):
        from galaxy_fsspec.exceptions import NotFoundError

        with pytest.raises(NotFoundError):
            fs.info("histories/Nope")

    def test_missing_dataset(self, fs):
        from galaxy_fsspec.exceptions import NotFoundError

        with pytest.raises(NotFoundError):
            fs.info("histories/History A/missing")


class TestDownloadRange:
    def test_fetch_uses_requests_206(self, fs, monkeypatch):
        transport = _use_transport(monkeypatch, RecordingTransport(FakeResponse(206, b"HELLO")))
        assert fs._download_range("ds1", 0, 5) == b"HELLO"
        headers = transport.calls[0]["headers"]
        assert headers["Range"] == "bytes=0-4"
        assert headers["x-api-key"] == "test-key"

    def test_fetch_200_slices(self, fs, monkeypatch):
        _use_transport(monkeypatch, RecordingTransport(FakeResponse(200, b"HELLOWORLD")))
        assert fs._download_range("ds1", 2, 7) == b"LLOWO"


class TestFileRead:
    def test_open_and_read(self, fs, monkeypatch):
        _use_transport(monkeypatch, ByteRangeTransport(b"HELLOWORLD"))
        with fs.open("histories/History A/my-uploaded-dataset", "rb") as f:
            assert f.read() == b"HELLOWORLD"

    def test_open_and_read_inside_collection(self, fs, monkeypatch):
        """A dataset leaf inside a collection is listed with size 0; opening it
        must fetch the real size via datasets.show_dataset so read() returns bytes."""
        # Real size for the forward dataset (dsF).
        fs.gi.datasets.store["dataset_sizes"] = {"dsF": 10}
        transport = _use_transport(monkeypatch, ByteRangeTransport(b"R1CONTENT!"))
        with fs.open("histories/History A/my result/sample1/forward", "rb") as f:
            assert f.size == 10
            assert f.read() == b"R1CONTENT!"
        assert transport.calls[0]["url"] == "https://galaxy.example/api/datasets/dsF/display"


class TestLibrariesRoot:
    def test_libraries_dir_info(self, fs):
        info = fs.info("libraries")
        assert info["type"] == "directory"

    def test_list_libraries(self, fs):
        names = fs.ls("libraries", detail=False)
        assert "libraries/Shared Data" in names


class TestLibraryContents:
    def test_list_library_root(self, fs):
        names = fs.ls("libraries/Shared Data", detail=False)
        assert "libraries/Shared Data/genomes" in names
        assert "libraries/Shared Data/reads.fastq" in names

    def test_library_folder_info(self, fs):
        info = fs.info("libraries/Shared Data/genomes")
        assert info["type"] == "directory"
        assert info["library_folder_id"] == "f1"

    def test_library_dataset_info(self, fs):
        info = fs.info("libraries/Shared Data/reads.fastq")
        assert info["type"] == "file"
        assert info["library_dataset_id"] == "dsL2"
        assert info["library_id"] == "lib1"

    def test_list_nested_folder(self, fs):
        names = fs.ls("libraries/Shared Data/genomes", detail=False)
        assert names == ["libraries/Shared Data/genomes/hg38.fa"]

    def test_nested_dataset_info(self, fs):
        info = fs.info("libraries/Shared Data/genomes/hg38.fa")
        assert info["type"] == "file"
        assert info["library_dataset_id"] == "dsL1"


class TestLibraryFileRead:
    def test_open_and_read_library_dataset(self, fs, monkeypatch):
        """Library datasets are read through Galaxy's display endpoint."""
        transport = _use_transport(monkeypatch, ByteRangeTransport(b"GTACGTACGTACGT"))
        with fs.open("libraries/Shared Data/genomes/hg38.fa", "rb") as f:
            assert f.size == 14
            assert f.read() == b"GTACGTACGTACGT"
        assert "display" in transport.calls[0]["url"]

    def test_open_and_read_root_library_dataset(self, fs, monkeypatch):
        _use_transport(monkeypatch, ByteRangeTransport(b"ATGCATGCAT"))
        with fs.open("libraries/Shared Data/reads.fastq", "rb") as f:
            assert f.size == 10
            assert f.read() == b"ATGCATGCAT"


class TestLibraryNotFound:
    def test_missing_library(self, fs):
        from galaxy_fsspec.exceptions import NotFoundError

        with pytest.raises(NotFoundError):
            fs.info("libraries/Nope")

    def test_missing_library_dataset(self, fs):
        from galaxy_fsspec.exceptions import NotFoundError

        with pytest.raises(NotFoundError):
            fs.info("libraries/Shared Data/missing.txt")


LEAF = "histories/History A/my result/sample1/forward"


class TestDownloads:
    """Redirects are followed by hand, so the API key only ever goes to Galaxy."""

    @pytest.mark.parametrize(
        ("location", "gets_key"),
        [
            ("/api/datasets/ds1/inner", True),
            ("https://objects.example/signed", False),
            ("http://galaxy.example/plain", False),
        ],
        ids=["same-origin", "object-store", "downgrade"],
    )
    def test_the_key_only_follows_a_redirect_to_galaxy(self, fs, monkeypatch, location, gets_key):
        transport = _use_transport(
            monkeypatch,
            RecordingTransport(
                FakeResponse(302, headers={"Location": location}), FakeResponse(206, b"HELLO")
            ),
        )
        assert fs._download_range("ds1", 0, 5) == b"HELLO"
        assert ("x-api-key" in transport.calls[1]["headers"]) is gets_key
        assert transport.calls[1]["headers"]["Range"] == "bytes=0-4"
        assert all(call["allow_redirects"] is False for call in transport.calls)
        assert all(response.closed for response in transport.responses)

    @pytest.mark.parametrize(
        "location",
        [None, "file:///etc/passwd", "https://galaxy.example/loop"],
        ids=["no-location", "not-http", "loop"],
    )
    def test_a_bad_redirect_is_refused(self, fs, monkeypatch, location):
        headers = {} if location is None else {"Location": location}
        _use_transport(monkeypatch, RecordingTransport(*[FakeResponse(302, headers=headers)] * 10))
        with pytest.raises(GalaxyApiError):
            fs._download_range("ds1", 0, 5)

    @pytest.mark.parametrize(("status", "error"), [(403, ReadOnlyError), (500, NotFoundError)])
    def test_an_error_response_is_closed(self, fs, monkeypatch, status, error):
        transport = _use_transport(monkeypatch, RecordingTransport(FakeResponse(status)))
        with pytest.raises(error):
            fs._download_range("ds1", 0, 5)
        assert transport.responses[0].closed

    def test_a_trailing_slash_in_the_url_is_harmless(self, monkeypatch):
        fs = make_fs(url="https://galaxy.example/")
        transport = _use_transport(monkeypatch, RecordingTransport(FakeResponse(206, b"HELLO")))
        fs._download_range("ds1", 0, 5)
        assert transport.calls[0]["url"] == "https://galaxy.example/api/datasets/ds1/display"
        assert transport.calls[0]["headers"]["x-api-key"] == "test-key"


class TestReading:
    """A read returns the whole dataset, or says why it cannot."""

    @pytest.mark.parametrize("prefix", ["", "/", "galaxy://"])
    def test_every_spelling_reads_the_whole_dataset(self, fs, monkeypatch, prefix):
        fs.gi.datasets.store["dataset_sizes"] = {"dsF": 10}
        _use_transport(monkeypatch, ByteRangeTransport(b"R1CONTENT!"))
        with fs.open(prefix + LEAF, "rb") as handle:
            assert handle.read() == b"R1CONTENT!"

    def test_a_failed_lookup_raises_instead_of_reading_empty(self, fs):
        cause = ConnectionError("Galaxy unavailable")

        def boom(*args, **kwargs):
            raise cause

        fs.gi.datasets.show_dataset = boom
        for path in (LEAF, "libraries/Shared Data/genomes/hg38.fa"):
            with pytest.raises(GalaxyApiError) as excinfo:
                fs.open(path, "rb")
            assert excinfo.value.__cause__ is cause

    def test_a_missing_size_is_an_error(self, fs):
        fs.gi.datasets.show_dataset = lambda dataset_id, hda_ldda="hda": {"id": dataset_id}
        with pytest.raises(GalaxyApiError):
            fs.open(LEAF, "rb")

    def test_a_zero_size_is_an_empty_dataset(self, fs):
        fs.gi.datasets.show_dataset = lambda dataset_id, hda_ldda="hda": {"file_size": 0}
        with fs.open(LEAF, "rb") as handle:
            assert handle.read() == b""

    @pytest.mark.parametrize("element", [{"name": "R1"}, {"name": "R1", "file_size": 5}])
    def test_an_entry_without_a_dataset_id_is_an_error(self, monkeypatch, element):
        store = _store()
        store["collections"]["subcoll1"] = [
            {
                "element_type": "hda",
                "element_index": 0,
                "element_identifier": "forward",
                "object": element,
            }
        ]
        _use_transport(monkeypatch, ByteRangeTransport(b"HELLO"))
        with pytest.raises(GalaxyApiError):
            make_fs(store).open(LEAF, "rb").read()

    def test_a_library_dataset_without_an_ldda_id_is_an_error(self, fs):
        fs.gi.datasets.show_dataset = lambda dataset_id, hda_ldda="hda": {
            "file_size": 0,
            "state": "ok",
        }
        with pytest.raises(GalaxyApiError):
            fs.open("libraries/Shared Data/genomes/hg38.fa", "rb")


class TestNames:
    """Every path ls() gives out opens the object it named, and only that one."""

    def test_namesakes_each_open_their_own(self):
        fs = make_fs(_store_with_namesakes())
        first, second = fs.ls("histories", detail=False)
        assert f"{first}/my-uploaded-dataset" in fs.ls(first, detail=False)
        assert f"{second}/second-dataset" in fs.ls(second, detail=False)
        one, two = fs.ls("libraries", detail=False)
        assert f"{one}/reads.fastq" in fs.ls(one, detail=False)
        assert f"{two}/other.txt" in fs.ls(two, detail=False)
        assert fs.ls("histories/hid2", detail=False) == ["histories/hid2/second-dataset"]
        assert fs.ls("libraries/lib2", detail=False) == ["libraries/lib2/other.txt"]


class TestCollectionsAndLibraries:
    def test_a_nested_collection_reports_its_own_type(self, fs):
        entries = fs.ls("histories/History A/my result", detail=True)
        assert [entry["collection_type"] for entry in entries] == ["paired"]


class TestTimestamps:
    @pytest.mark.parametrize(
        ("stamp", "microsecond"),
        [
            ("2024-01-02T03:04:05", 0),
            ("2024-01-02T03:04:05Z", 0),
            ("2024-01-02T03:04:05.123456Z", 123456),
        ],
    )
    def test_every_spelling_galaxy_sends_parses(self, stamp, microsecond):
        store = _store()
        store["histories"][0]["create_time"] = stamp
        created = make_fs(store).created("histories/History A")
        assert created == _dt.datetime(2024, 1, 2, 3, 4, 5, microsecond, tzinfo=_dt.timezone.utc)

    def test_a_missing_date_is_an_error(self, fs):
        with pytest.raises(GalaxyApiError):
            fs.modified("histories/History A/my result/sample1/forward")


class TestConstruction:
    def test_walk_skips_a_directory_it_cannot_list(self, fs):
        """Every error is an OSError, which fsspec's walk skips instead of aborting on."""
        original = fs._list

        def fail_inside_a_library(path):
            if path.startswith("libraries/"):
                raise GalaxyApiError("Galaxy unavailable")
            return original(path)

        fs._list = fail_inside_a_library
        assert [root for root, _dirs, _files in fs.walk("libraries")] == ["libraries"]
