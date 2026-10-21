"""Where Night Scout keeps its local state. No user paths are hardcoded here."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def data_dir() -> Path:
    """Directory for the database, digests and sandbox scratch space.

    Order: $NIGHT_SCOUT_HOME, then the platform data dir (XDG on Linux,
    %LOCALAPPDATA% on Windows), then ~/.night-scout.
    """
    override = os.environ.get("NIGHT_SCOUT_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("linux") and os.environ.get("XDG_DATA_HOME"):
        return Path(os.environ["XDG_DATA_HOME"]) / "night-scout"
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "night-scout"
    return Path.home() / ".night-scout"


def default_config_path() -> Path:
    override = os.environ.get("NIGHT_SCOUT_CONFIG")
    if override:
        return Path(override).expanduser()
    return data_dir() / "config.yaml"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
