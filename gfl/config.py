"""Experiment configuration: YAML schema, validation, and matrix expansion.

One YAML file describes one experiment. The `matrix` section lists the values to
sweep; `expand()` takes the cartesian product so a single file can describe all
12 scalability configs (4 presets x 3 resolutions). Each expanded `RunConfig` is
then captured `capture.runs` times.

See configs/README.md for the field reference and an annotated example.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Unreal Engine scalability levels, set through the sg.* console variables
# (sg.ViewDistanceQuality, sg.ShadowQuality, sg.GlobalIlluminationQuality, ...).
# Each name maps to the integer level UE uses for every sg.* group.
PRESET_LEVELS: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "epic": 3}

# For games gfl does not launch (Cyberpunk 2077), settings are picked in the game's
# own menu, so the preset is recorded as "manual" and described in the YAML notes.
MANUAL_PRESET = "manual"

# "native" means DLSS Super Resolution is off and the game renders at output resolution.
DLSS_MODES: tuple[str, ...] = ("native", "quality", "balanced", "performance")

# unreal: gfl launches the game with console variable arguments.
# manual: the person starts the game/benchmark by hand (Cyberpunk 2077); gfl only captures.
LAUNCHERS: tuple[str, ...] = ("unreal", "manual")

_RESOLUTION_RE = re.compile(r"^(\d{3,5})x(\d{3,5})$")
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ConfigError(ValueError):
    """Raised for a missing or invalid field in an experiment YAML file."""


@dataclass(frozen=True)
class Resolution:
    width: int
    height: int

    @classmethod
    def parse(cls, text: str) -> "Resolution":
        match = _RESOLUTION_RE.match(str(text).strip().lower())
        if not match:
            raise ConfigError(f"resolution must look like 2560x1440, got {text!r}")
        return cls(int(match.group(1)), int(match.group(2)))

    @property
    def pixels(self) -> int:
        return self.width * self.height

    def __str__(self) -> str:
        return f"{self.width}x{self.height}"


@dataclass(frozen=True)
class SceneConfig:
    name: str
    launcher: str
    process_name: str  # passed to PresentMon --process_name, e.g. LyraGame.exe
    executable: str | None = None  # required for launcher "unreal"
    map: str | None = None
    extra_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class CaptureSettings:
    runs: int = 3
    duration_s: float = 60.0
    # The first seconds of every run are dropped: shader compilation, texture
    # streaming, and clock ramp-up make them unrepresentative of steady state.
    warmup_s: float = 10.0
    # Pause between runs so GPU temperature and clocks settle before the next one.
    cooldown_s: float = 15.0


@dataclass(frozen=True)
class RunConfig:
    """One point in the experiment matrix. Captured `runs` times."""

    experiment: str
    scene: str
    preset: str
    resolution: Resolution
    dlss_mode: str
    frame_generation: bool
    inference_streams: int
    tag: str

    @property
    def config_id(self) -> str:
        fg = "fg-on" if self.frame_generation else "fg-off"
        return (
            f"{self.scene}_{self.preset}_{self.resolution}_{self.dlss_mode}_"
            f"{fg}_s{self.inference_streams}_{self.tag}"
        )


@dataclass(frozen=True)
class ExperimentConfig:
    name: str
    description: str
    scene: SceneConfig
    capture: CaptureSettings
    tag: str
    presets: tuple[str, ...]
    resolutions: tuple[Resolution, ...]
    dlss_modes: tuple[str, ...] = ("native",)
    frame_generation: tuple[bool, ...] = (False,)
    inference_streams: tuple[int, ...] = (0,)
    source_path: Path | None = field(default=None, compare=False)

    def expand(self) -> list[RunConfig]:
        """Cartesian product of the matrix, in a stable, readable order."""
        combos = itertools.product(
            self.presets,
            self.resolutions,
            self.dlss_modes,
            self.frame_generation,
            self.inference_streams,
        )
        return [
            RunConfig(self.name, self.scene.name, p, r, d, fg, s, self.tag)
            for p, r, d, fg, s in combos
        ]


# --------------------------------------------------------------------------- #
# Loading and validation
# --------------------------------------------------------------------------- #


def load_experiment(path: str | Path) -> ExperimentConfig:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    try:
        return parse_experiment(raw, source_path=path)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from None


def parse_experiment(raw: dict[str, Any], source_path: Path | None = None) -> ExperimentConfig:
    name = _require(raw, "experiment", str)
    if not _TAG_RE.match(name):
        raise ConfigError("experiment must be lowercase letters, digits, '_' or '-'")

    scene = _parse_scene(_require(raw, "scene", dict))
    capture = _parse_capture(raw.get("capture") or {})
    tag = str(raw.get("tag", "baseline"))
    if not _TAG_RE.match(tag):
        raise ConfigError(f"tag must be lowercase letters, digits, '_' or '-', got {tag!r}")

    matrix = _require(raw, "matrix", dict)
    presets = tuple(str(p).lower() for p in _as_list(_require(matrix, "preset", (list, str))))
    for p in presets:
        if p == MANUAL_PRESET:
            if scene.launcher != "manual":
                raise ConfigError("preset 'manual' is only valid with scene.launcher 'manual'")
        elif p not in PRESET_LEVELS:
            raise ConfigError(f"unknown preset {p!r}; expected one of {list(PRESET_LEVELS)}")

    resolutions = tuple(
        Resolution.parse(r) for r in _as_list(_require(matrix, "resolution", (list, str)))
    )

    dlss_modes = tuple(str(d).lower() for d in _as_list(matrix.get("dlss_mode", ["native"])))
    for d in dlss_modes:
        if d not in DLSS_MODES:
            raise ConfigError(f"unknown dlss_mode {d!r}; expected one of {list(DLSS_MODES)}")

    frame_gen = tuple(_as_list(matrix.get("frame_generation", [False])))
    if not all(isinstance(v, bool) for v in frame_gen):
        raise ConfigError("frame_generation values must be true or false")

    streams = tuple(_as_list(matrix.get("inference_streams", [0])))
    if not all(isinstance(s, int) and not isinstance(s, bool) and s >= 0 for s in streams):
        raise ConfigError("inference_streams values must be non-negative integers")

    for label, values in [("preset", presets), ("resolution", resolutions),
                          ("dlss_mode", dlss_modes), ("inference_streams", streams)]:
        if len(set(values)) != len(values):
            raise ConfigError(f"matrix.{label} has duplicate values")

    return ExperimentConfig(
        name=name,
        description=str(raw.get("description", "")).strip(),
        scene=scene,
        capture=capture,
        tag=tag,
        presets=presets,
        resolutions=resolutions,
        dlss_modes=dlss_modes,
        frame_generation=frame_gen,
        inference_streams=streams,
        source_path=source_path,
    )


def _parse_scene(raw: dict[str, Any]) -> SceneConfig:
    name = _require(raw, "name", str)
    if not _TAG_RE.match(name):
        raise ConfigError("scene.name must be lowercase letters, digits, '_' or '-'")
    launcher = _require(raw, "launcher", str)
    if launcher not in LAUNCHERS:
        raise ConfigError(f"scene.launcher must be one of {list(LAUNCHERS)}, got {launcher!r}")
    executable = raw.get("executable")
    if launcher == "unreal" and not executable:
        raise ConfigError("scene.executable is required when scene.launcher is 'unreal'")
    extra_args = raw.get("extra_args") or []
    if not isinstance(extra_args, list):
        raise ConfigError("scene.extra_args must be a list of strings")
    return SceneConfig(
        name=name,
        launcher=launcher,
        process_name=_require(raw, "process_name", str),
        executable=executable,
        map=raw.get("map"),
        extra_args=tuple(str(a) for a in extra_args),
    )


def _parse_capture(raw: dict[str, Any]) -> CaptureSettings:
    defaults = CaptureSettings()
    runs = raw.get("runs", defaults.runs)
    duration = float(raw.get("duration_s", defaults.duration_s))
    warmup = float(raw.get("warmup_s", defaults.warmup_s))
    cooldown = float(raw.get("cooldown_s", defaults.cooldown_s))
    if not isinstance(runs, int) or isinstance(runs, bool) or runs < 1:
        raise ConfigError("capture.runs must be an integer >= 1")
    if duration <= 0:
        raise ConfigError("capture.duration_s must be > 0")
    if not 0 <= warmup < duration:
        raise ConfigError("capture.warmup_s must be >= 0 and less than capture.duration_s")
    if cooldown < 0:
        raise ConfigError("capture.cooldown_s must be >= 0")
    return CaptureSettings(runs=runs, duration_s=duration, warmup_s=warmup, cooldown_s=cooldown)


def _require(raw: dict[str, Any], key: str, kind: type | tuple[type, ...]) -> Any:
    if key not in raw or raw[key] is None:
        raise ConfigError(f"missing required field {key!r}")
    value = raw[key]
    if not isinstance(value, kind):
        raise ConfigError(f"field {key!r} has the wrong type ({type(value).__name__})")
    return value


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]
