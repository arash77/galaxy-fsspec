"""Shared pytest fixtures."""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config, items):
    # Auto-skip integration tests when no API key is configured.
    if os.environ.get("GALAXY_USER_API_KEY"):
        return
    skip_integration = pytest.mark.skip(
        reason="GALAXY_USER_API_KEY not set; live Galaxy tests skipped"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)


@pytest.fixture
def galaxy_env(monkeypatch):
    """Provide deterministic env vars for unit tests."""
    monkeypatch.setenv("GALAXY_URL", "https://galaxy.example")
    monkeypatch.setenv("GALAXY_USER_API_KEY", "test-key")
    return monkeypatch
