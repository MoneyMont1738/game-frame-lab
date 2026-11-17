"""Machine-specific settings for the benchmark PC (tool paths, timings).

These live in `gfl.local.yaml`, which is gitignored because paths differ per
machine. Copy `gfl.local.example.yaml` to start. Every field has a default, so
`gfl run --dry-run` works without the file.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

import yaml

from gfl.config import ConfigError

DEFAULT_SETTINGS_PATH = Path("gfl.local.yaml")


@dataclass(frozen=True)
class LocalSettings:
    presentmon_exe: str = "PresentMon.exe"  # full path, or a name on PATH
    nvidia_smi_exe: str = "nvidia-smi"      # installed with the NVIDIA driver
    capture_root: str = "data/raw"          # gitignored
    # Time from game launch until the benchmark camera path is running. The
    # flythrough loops, so a fixed wait gives every run the same kind of content.
    load_wait_s: float = 25.0
    telemetry_interval_ms: int = 500
    # Extra time PresentMon may take beyond its timed capture before we treat
    # it as hung.
    presentmon_timeout_margin_s: float = 60.0


def load_settings(path: str | Path | None = None) -> LocalSettings:
    """Load settings; a missing default file means "use defaults"."""
    explicit = path is not None
    path = Path(path) if explicit else DEFAULT_SETTINGS_PATH
    if not path.exists():
        if explicit:
            raise ConfigError(f"settings file not found: {path}")
        return LocalSettings()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    known = {f.name for f in fields(LocalSettings)}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"{path}: unknown settings {sorted(unknown)}; expected {sorted(known)}")
    return LocalSettings(**raw)
