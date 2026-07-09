"""Live integration tests against a Galaxy instance.

Skipped automatically when GALAXY_USER_API_KEY is not set (see conftest.py).
These tests create a throwaway history, upload two small FASTQ files, build a
paired collection, and assert the fsspec view matches. The history is deleted
in a finalizer.
"""

from __future__ import annotations

import contextlib
import os
import uuid

import pytest
from bioblend.galaxy import GalaxyInstance
from bioblend.galaxy.dataset_collections import CollectionDescription, SimpleElement

from galaxy_fsspec.fs import GalaxyFileSystem

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def fs():
    filesystem = GalaxyFileSystem()  # reads GALAXY_URL + GALAXY_USER_API_KEY
    filesystem._clear_cache()
    return filesystem


@pytest.fixture(scope="module")
def seeded_history(fs):
    gi = GalaxyInstance(
        url=os.environ["GALAXY_URL"], key=os.environ["GALAXY_USER_API_KEY"]
    )
    name = f"galaxy-fsspec-test-{uuid.uuid4().hex[:8]}"
    hist_id = gi.histories.create_history(name=name)["id"]
    try:
        gi.tools.paste_content("@r1\nACGT\n+\nIIII\n", history_id=hist_id)
        gi.tools.paste_content("@r2\nTTGG\n+\nIIII\n", history_id=hist_id)
        # Resolve the uploaded HDA ids and wait for them to be ready.
        contents = gi.histories.show_history(hist_id, contents=True)
        hda_ids = [c["id"] for c in contents if c["history_content_type"] == "dataset"]
        assert len(hda_ids) >= 2
        for hid in hda_ids:
            gi.datasets.wait_for_dataset(hid)
        description = CollectionDescription(
            name="my-paired",
            type="paired",
            elements=[
                SimpleElement({"name": "forward", "src": "hda", "id": hda_ids[0]}),
                SimpleElement({"name": "reverse", "src": "hda", "id": hda_ids[1]}),
            ],
        )
        gi.histories.create_dataset_collection(hist_id, description)
        yield hist_id
    finally:
        with contextlib.suppress(Exception):
            gi.histories.delete_history(hist_id, purge=True)


def test_histories_listed(fs, seeded_history):
    fs._clear_cache()
    names = fs.ls("histories")
    assert any(n.endswith(seeded_history) or "galaxy-fsspec-test" in n for n in names), names


def test_history_contents_and_collection(fs, seeded_history):
    fs._clear_cache()
    # Find our history folder by name.
    entries = fs.ls("histories", detail=True)
    ours = next(e for e in entries if e.get("history_id") == seeded_history)
    assert ours["type"] == "directory"

    # Timestamps are fetched lazily via info(), not during ls().
    details = fs.info(ours["name"])
    assert details["created"] is not None
    assert details["last_modified"] is not None

    children = fs.ls(ours["name"], detail=True)
    coll = next(c for c in children if c["type"] == "directory")
    assert coll["collection_type"] == "paired"

    pair = fs.ls(coll["name"], detail=True)
    assert {p["name"].rsplit("/", 1)[-1] for p in pair} == {"forward", "reverse"}
    assert all(p["type"] == "file" for p in pair)


def test_read_dataset_bytes(fs, seeded_history):
    fs._clear_cache()
    entries = fs.ls("histories", detail=True)
    ours = next(e for e in entries if e.get("history_id") == seeded_history)
    children = fs.ls(ours["name"], detail=True)
    coll = next(c for c in children if c["type"] == "directory")
    pair = fs.ls(coll["name"], detail=True)
    forward = next(p for p in pair if p["name"].endswith("/forward"))
    with fs.open(forward["name"], "rb") as fh:
        body = fh.read()
    assert b"@r1" in body


@pytest.fixture(scope="module")
def library_with_file():
    """Find the "Charts Example Data" library and its bacteriome.txt file.

    This is a small public library on usegalaxy.org with a flat file,
    ideal for testing library browsing and downloads without walking
    deep folder trees.
    """
    gi = GalaxyInstance(
        url=os.environ["GALAXY_URL"], key=os.environ["GALAXY_USER_API_KEY"]
    )
    libraries = gi.libraries.get_libraries(deleted=False)

    # Prefer "Charts Example Data" (small, flat, public on usegalaxy.org).
    preferred = [
        lib for lib in libraries if "charts" in lib.get("name", "").lower()
    ]
    candidates = preferred or libraries
    if not candidates:
        pytest.skip("No accessible data libraries on this Galaxy instance")

    for lib in candidates:
        entries = gi.libraries.show_library(lib["id"], contents=True)
        files = [e for e in entries if e.get("type") == "file"]
        if files:
            # Prefer bacteriome.txt; fall back to shallowest file.
            bacteriome = [f for f in files if "bacteriome" in f.get("name", "")]
            target = bacteriome[0] if bacteriome else min(
                files, key=lambda f: f.get("name", "").count("/")
            )
            return lib["id"], target["name"]
    pytest.skip("No accessible library contains readable files")


def test_libraries_listed(fs):
    fs._clear_cache()
    names = fs.ls("libraries")
    assert len(names) > 0


def test_library_browse_and_read(fs, library_with_file):
    lib_id, galaxy_path = library_with_file
    fs._clear_cache()

    # Find the library folder in the fsspec tree.
    entries = fs.ls("libraries", detail=True)
    ours = next(e for e in entries if e.get("library_id") == lib_id)
    assert ours["type"] == "directory"

    # Build the fsspec path from the Galaxy path (e.g. /a/b/c.vcf -> a/b/c.vcf).
    rel = galaxy_path.lstrip("/")
    file_path = f"{ours['name']}/{rel}"

    # Verify navigation: each parent folder must be listable via fs.ls.
    parts = rel.split("/")
    for i in range(len(parts) - 1):
        parent = f"{ours['name']}/" + "/".join(parts[: i + 1])
        children = fs.ls(parent, detail=True)
        child_name = f"{ours['name']}/" + "/".join(parts[: i + 2])
        assert any(c["name"] == child_name for c in children), (
            f"{child_name} not found in {parent}"
        )

    # Read the file.
    info = fs.info(file_path)
    assert info["type"] == "file"
    with fs.open(file_path, "rb") as fh:
        body = fh.read(1024)
    assert len(body) > 0
