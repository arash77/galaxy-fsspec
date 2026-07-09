export GALAXY_URL := env_var_or_default("GALAXY_URL", "https://usegalaxy.org")
export GALAXY_USER_API_KEY := env_var_or_default("GALAXY_USER_API_KEY", "")
export GALAXY_FSSPEC_SHOW_HID_IN_NAMES := env_var_or_default("GALAXY_FSSPEC_SHOW_HID_IN_NAMES", "")

# Default: list available recipes.
default:
    @just --list

# Install the project + dev dependencies into the uv venv.
install:
    uv sync

# Run unit tests (no Galaxy connection required).
test:
    uv run pytest -m "not integration"

# Collect integration tests without running them — catches import/collection
# errors even when GALAXY_USER_API_KEY is not set.
collect-integration:
    uv run pytest --collect-only -m "integration" -q

# Run live integration tests (requires GALAXY_USER_API_KEY).
test-integration:
    @if [ -z "$GALAXY_USER_API_KEY" ]; then echo "GALAXY_USER_API_KEY not set"; exit 1; fi
    uv run pytest -m "integration"

# Run all tests (unit + integration, if key is set).
test-all:
    uv run pytest

# Lint with ruff + mypy.
lint:
    uv run ruff check .
    uv run mypy src

# Auto-format with ruff.
fmt:
    uv run ruff format .
    uv run ruff check --fix .

# Build a wheel.
build:
    uv build
