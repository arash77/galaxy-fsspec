"""Pure unit tests for path handling (no network, no Galaxy)."""

from __future__ import annotations

from galaxy_fsspec.paths import (
    dedupe_names,
    name_with_prefix,
    parse_numbered_name,
    sanitize_segment,
)


class TestSanitize:
    def test_slashes_replaced(self):
        assert sanitize_segment("a/b/c") == "a_b_c"

    def test_stripped(self):
        assert sanitize_segment("  spaced  ") == "spaced"


class TestNameWithPrefix:
    def test_unnumbered(self):
        assert name_with_prefix(30, "my result", numbered=False) == "my result"

    def test_numbered(self):
        assert name_with_prefix(30, "my result", numbered=True) == "30-my result"

    def test_numbered_preserves_spaces(self):
        assert name_with_prefix(1, "my uploaded dataset", numbered=True) == (
            "1-my uploaded dataset"
        )

    def test_no_hid_no_prefix(self):
        assert name_with_prefix(None, "x", numbered=True) == "x"


class TestParseNumbered:
    def test_plain(self):
        assert parse_numbered_name("30-my result") == (30, "my result")

    def test_leading_dash_in_name_preserved(self):
        assert parse_numbered_name("1-foo-bar") == (1, "foo-bar")

    def test_no_prefix(self):
        assert parse_numbered_name("foo") == (None, "foo")

    def test_empty(self):
        assert parse_numbered_name("") == (None, "")

    def test_non_digit_head(self):
        # "abc-foo" -> no numeric prefix
        assert parse_numbered_name("abc-foo") == (None, "abc-foo")


class TestDedupe:
    def test_unique_names_unchanged(self):
        items = [{"id": "a", "name": "foo", "hid": 1}, {"id": "b", "name": "bar", "hid": 2}]
        out = dedupe_names(items, numbered=True)
        assert [n for n, _ in out] == ["1-foo", "2-bar"]

    def test_collision_suffixed(self):
        # Same name AND same hid (or unnumbered) => collision needs id suffix.
        items = [
            {"id": "abc123def456", "name": "dup", "hid": 1},
            {"id": "xyz789abc012", "name": "dup", "hid": 1},
        ]
        out = dedupe_names(items, numbered=True)
        names = [n for n, _ in out]
        assert names[0] == "1-dup"
        assert names[1].startswith("1-dup__")
        assert names[0] != names[1]

    def test_collision_unnumbered(self):
        items = [
            {"id": "abc123def456", "name": "dup"},
            {"id": "xyz789abc012", "name": "dup"},
        ]
        out = dedupe_names(items, numbered=False)
        names = [n for n, _ in out]
        assert names[0] == "dup"
        assert names[1].startswith("dup__")
        assert names[0] != names[1]

    def test_preserves_order(self):
        items = [{"id": "1", "name": "z", "hid": 5}, {"id": "2", "name": "a", "hid": 1}]
        out = dedupe_names(items, numbered=False)
        assert [i["id"] for _, i in out] == ["1", "2"]
