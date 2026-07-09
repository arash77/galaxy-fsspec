# galaxy-fsspec

An [`fsspec`](https://filesystem-spec.readthedocs.io/) filesystem that exposes a
Galaxy account as a virtual, read-only directory tree.

```
galaxy://
├── histories/
│   └── <history>/                 (created / last_modified exposed via info())
│       ├── <dataset>              (files)
│       └── <collection>/          (list, paired, list:paired, nested ...)
│           └── ...                (collections nest as folders of folders)
└── libraries/
    └── <library>/
        ├── <dataset>
        └── <folder>/
            └── ...
```

Datasets are streamed as files; collections (including nested collections)
appear as directories.

## Install

```bash
uv pip install galaxy-fsspec
```

Because the protocol is registered via the `fsspec.specs` entry point,
`fsspec.filesystem("galaxy")` and `fsspec.open("galaxy://...")` work without
any extra imports.

## Configure

The filesystem reads two environment variables:

| Variable                  | Default              | Purpose                          |
| ------------------------- | -------------------- | -------------------------------- |
| `GALAXY_URL`              | `https://usegalaxy.org` | Galaxy server URL             |
| `GALAXY_USER_API_KEY`     | *(required)*         | User API key                     |

Optional:

| Variable                       | Default | Purpose                                       |
| ------------------------------ | ------- | --------------------------------------------- |
| `GALAXY_FSSPEC_SHOW_HID_IN_NAMES` | unset   | `true` prefixes entries with their `hid` (`1-my-dataset`) |

You can also pass arguments explicitly:

```python
import fsspec
fs = fsspec.filesystem("galaxy", url="https://my.galaxy.org", api_key="...", show_hid_in_names=True)
```

## Usage

```python
import fsspec

fs = fsspec.filesystem("galaxy")

# List histories.
for h in fs.ls("histories", detail=True):
    print(h["name"])

# Timestamps (created / last_modified) are fetched lazily via info().
for h in fs.ls("histories", detail=True):
    info = fs.info(h["name"])
    print(info["name"], info["created"], info["last_modified"])

# Walk a history's contents — datasets are files, collections are folders.
print(fs.ls("histories/My History"))

# Read a dataset (range-aware streaming via Galaxy's display endpoint).
with fs.open("histories/My History/my-paired/forward/R1.fastq", "rb") as f:
    print(f.read(64))
```

`show_hid_in_names=True` renders history entries as Galaxy does in its UI:

```
histories/My History/1-my-uploaded-dataset
histories/My History/30-my result
histories/My History/5-mycollection/1-forward
```

## Scope

Read-only for now: histories, datasets, and (nested) collections. Writes,
uploads, and library browsing are not yet supported.
