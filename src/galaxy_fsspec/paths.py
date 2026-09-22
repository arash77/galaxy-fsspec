"""Path handling: sanitization, numbered-name formatting/parsing, id resolution.

Galaxy dataset and collection names can:
  - contain slashes (we replace them with `_`),
  - collide within a single listing (we suffix a short id on collision),
  - contain arbitrary unicode.

When ``show_hid_in_names`` is enabled, each entry is prefixed with its ``hid``
(history item number) in the Galaxy style: ``1-my-uploaded-dataset``.
"""

from __future__ import annotations

# Characters that must not appear in a single path segment.
_SLASH_REPLACEMENT = "_"


def sanitize_segment(name: str) -> str:
    """Make a Galaxy name safe to use as a single fsspec path segment."""
    # Galaxy names may contain "/" which would break path parsing.
    return name.replace("/", _SLASH_REPLACEMENT).strip()


def name_with_prefix(hid: int | None, name: str, numbered: bool) -> str:
    """Return the display name, optionally prefixed with ``<hid>-``.

    Matches Galaxy's own history rendering: integer, hyphen, original name.
    No zero-padding; spaces preserved.
    """
    sanitized = sanitize_segment(name)
    if numbered and hid is not None:
        return f"{int(hid)}-{sanitized}"
    return sanitized


def dedupe_names(items: list[dict], numbered: bool) -> list[tuple[str, dict]]:
    """Assign unique display names to a list of Galaxy content dicts.

    Returns a list of ``(display_name, original_dict)`` preserving input order.

    A name that occurs once is used as-is. When a name occurs more than once,
    *every* member of that group is suffixed with ``__<short_id>``, not just the
    later ones. Galaxy orders ``/api/histories`` by update time, so the same two
    histories arrive in either order from one call to the next; suffixing only
    the later ones would move the unsuffixed name between them, and a path that
    ``ls()`` has already handed out would start naming a different object.
    """
    bases = [
        name_with_prefix(_item_hid(item), _item_name(item), numbered) for item in items
    ]
    counts: dict[str, int] = {}
    for base in bases:
        counts[base] = counts.get(base, 0) + 1

    out: list[tuple[str, dict]] = []
    used: set[str] = set()
    for base, item in zip(bases, items, strict=True):
        candidate = base if counts[base] == 1 else f"{base}__{_short_id(_item_id(item))}"
        # Two ids can still share a short suffix, and an item can be named like
        # another's suffixed form. Rare, and order-dependent, but it must not
        # produce a duplicate path.
        if candidate in used:
            suffix = 2
            while f"{candidate}__{suffix}" in used:
                suffix += 1
            candidate = f"{candidate}__{suffix}"
        used.add(candidate)
        out.append((candidate, item))
    return out


def _item_name(item: dict) -> str:
    # Collections use "name", dataset elements sometimes "name" or "element_identifier".
    return str(item.get("name") or item.get("element_identifier") or item.get("hid") or "unnamed")


def _item_hid(item: dict) -> int | None:
    hid = item.get("hid")
    if hid is None:
        return None
    try:
        return int(hid)
    except (TypeError, ValueError):
        return None


def _item_id(item: dict) -> str:
    return str(item.get("id") or item.get("element_id") or "")


def _short_id(full_id: str) -> str:
    """Return a short, filesystem-safe suffix of a Galaxy id."""
    if not full_id:
        return "x"
    # Galaxy ids are often hex-ish; take the last 6 chars.
    safe = "".join(c if c.isalnum() else "" for c in full_id)
    return safe[-6:] or "x"
