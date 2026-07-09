"""Path handling: sanitization, numbered-name formatting/parsing, id resolution.

Galaxy dataset and collection names can:
  - contain slashes (we replace them with `_`),
  - collide within a single listing (we suffix a short id on collision),
  - contain arbitrary unicode.

When ``show_hid_in_names`` is enabled, each entry is prefixed with its ``hid``
(history item number) in the Galaxy style: ``1-my-uploaded-dataset``.
"""

from __future__ import annotations

from typing import NamedTuple

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


class ParsedNumbered(NamedTuple):
    """Result of parsing a numbered path segment."""

    hid: int | None
    name: str


def parse_numbered_name(segment: str) -> ParsedNumbered:
    """Split a leading ``<int>-`` prefix from a path segment.

    Returns ``ParsedNumbered(hid=None, name=segment)`` when there is no
    integer prefix. Always returns the full remainder (including any
    further hyphens) as ``name``.
    """
    if not segment:
        return ParsedNumbered(None, segment)
    # Find the first hyphen and check the run-up is all digits.
    dash = segment.find("-")
    if dash <= 0:
        return ParsedNumbered(None, segment)
    head = segment[:dash]
    if not head.lstrip("-").isdigit():
        return ParsedNumbered(None, segment)
    try:
        hid = int(head)
    except ValueError:
        return ParsedNumbered(None, segment)
    return ParsedNumbered(hid, segment[dash + 1 :])


def dedupe_names(items: list[dict], numbered: bool) -> list[tuple[str, dict]]:
    """Assign unique display names to a list of Galaxy content dicts.

    Returns a list of ``(display_name, original_dict)`` preserving input order.
    Collisions are broken by appending ``__<short_id>`` of the object id.
    """
    seen: dict[str, int] = {}
    out: list[tuple[str, dict]] = []
    for item in items:
        hid = _item_hid(item)
        base = name_with_prefix(hid, _item_name(item), numbered)
        candidate = base
        if candidate in seen:
            short = _short_id(_item_id(item))
            candidate = f"{base}__{short}"
        # If even the suffixed name collided, keep appending until unique.
        while candidate in seen:
            seen[candidate] += 1
            candidate = f"{base}__{seen[candidate]}"
        seen[candidate] = 0
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
