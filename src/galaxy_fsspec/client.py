"""BioBlend client construction and env-var configuration."""

from __future__ import annotations

import os

from bioblend.galaxy import GalaxyInstance

from galaxy_fsspec.exceptions import GalaxyFsspecError


def show_hid_in_names_from_env() -> bool:
    """Read the GALAXY_FSSPEC_SHOW_HID_IN_NAMES env var.

    Only the exact value ``"true"`` (case-insensitive) enables numbered names.
    Any other value (including unset) means ``False``.
    """
    return os.environ.get("GALAXY_FSSPEC_SHOW_HID_IN_NAMES", "").strip().lower() == "true"


#: Seconds a Galaxy API call may take. bioblend has no default, so it would wait forever.
DEFAULT_TIMEOUT = 60.0


def build_galaxy_instance(
    url: str | None = None,
    api_key: str | None = None,
    timeout: float | None = None,
) -> GalaxyInstance:
    """Construct a bioblend GalaxyInstance from args or environment.

    Environment variables:
        GALAXY_URL            Galaxy server URL (default: https://usegalaxy.org)
        GALAXY_USER_API_KEY   User API key (required)
    """
    url = url or os.environ.get("GALAXY_URL", "https://usegalaxy.org")
    api_key = api_key or os.environ.get("GALAXY_USER_API_KEY")
    if not api_key:
        raise GalaxyFsspecError(
            "A Galaxy API key is required. Set GALAXY_USER_API_KEY or pass api_key=..."
        )
    instance = GalaxyInstance(url=url, key=api_key)
    # Set after construction, not passed in: GalaxyInstance.__init__ accepts no timeout and drops
    # it silently. Only its parent, GalaxyClient, stores one, and every request site reads it.
    instance.timeout = DEFAULT_TIMEOUT if timeout is None else float(timeout)
    return instance
