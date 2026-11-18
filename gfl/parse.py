"""Parsers that turn tool-specific CSV logs into one canonical frame table.

Every parser returns a DataFrame with the same columns (FRAME_COLUMNS), so the
metrics code never needs to know which tool produced the data:

    time_s         seconds since the first frame of the target process
    frametime_ms   present-to-present interval (PresentMon "MsBetweenPresents")
    gpu_busy_ms    time the GPU was busy with this process's work for the frame
    gpu_time_ms    total GPU time for the frame
    cpu_busy_ms    CPU time spent on the frame before Present()
    gpu_util_pct   per-frame GPU utilization sample (FrameView only)

Columns a tool does not provide are filled with NaN.

Methodology note: "frame time" here is the interval between consecutive
Present() calls. That is what both PresentMon and FrameView report as
MsBetweenPresents, and it is the standard basis for FPS and percentile metrics.
It measures the game's output cadence, not display timing; display-side metrics
(MsBetweenDisplayChange) matter more for Frame Generation and are handled in the
DLSS phase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

FRAME_COLUMNS = ["time_s", "frametime_ms", "gpu_busy_ms", "gpu_time_ms", "cpu_busy_ms", "gpu_util_pct"]
TELEMETRY_COLUMNS = ["time_s", "vram_used_mib", "gpu_util_pct"]

# PresentMon writes "NA" for values it could not compute (for example
# DisplayedTime for frames that never reached the screen).
_NA_VALUES = ["NA", "N/A", ""]


class ParseError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# PresentMon
# --------------------------------------------------------------------------- #

# Column names verified against the PresentMon console application README
# (2.x, GameTechDev/PresentMon main branch, read November 2026). The 1.x format
# (and 2.x with --v1_metrics) uses TimeInSeconds and lowercase "ms" prefixes,
# so lookups are case-insensitive and fall back to the 1.x names.
_PRESENTMON_ALIASES = {
    "time": ["CPUStartTime", "TimeInSeconds"],
    "frametime": ["MsBetweenPresents"],
    "gpu_busy": ["MsGPUBusy"],
    "gpu_time": ["MsGPUTime"],
    "cpu_busy": ["MsCPUBusy"],
}


def load_presentmon_csv(path: str | Path, process_name: str | None = None) -> pd.DataFrame:
    """Parse a PresentMon CSV (plain or .gz) into the canonical frame table.

    If the log contains several processes, `process_name` selects one
    (case-insensitive). Capture should use --process_name so this is rare.
    """
    raw = _read_csv(path)
    raw = _select_process(raw, process_name, path)
    raw = _select_main_swapchain(raw)

    cols = _resolve_columns(raw, _PRESENTMON_ALIASES, path, required=("time", "frametime"))
    if any(c.lower().startswith("cpustartq") or c == "CPUStartDateTime" for c in raw.columns):
        raise ParseError(
            f"{path}: captured with --qpc_time or --date_time. Re-capture without "
            "those flags so CPUStartTime is relative to the start of recording."
        )

    frames = pd.DataFrame(
        {
            "time_s": pd.to_numeric(raw[cols["time"]], errors="coerce"),
            "frametime_ms": pd.to_numeric(raw[cols["frametime"]], errors="coerce"),
            "gpu_busy_ms": _optional(raw, cols.get("gpu_busy")),
            "gpu_time_ms": _optional(raw, cols.get("gpu_time")),
            "cpu_busy_ms": _optional(raw, cols.get("cpu_busy")),
            "gpu_util_pct": np.nan,
        }
    )
    frames = _clean(frames, path)
    # TimeInSeconds is documented in seconds. CPUStartTime's unit is not stated
    # explicitly, so infer it: the timestamp span must roughly equal the sum of
    # the frame times. A ~1000x mismatch means the column is in milliseconds.
    if cols["time"].lower() == "cpustarttime":
        frames["time_s"] = _time_in_seconds(frames["time_s"], frames["frametime_ms"])
    return _rebase(frames)


# --------------------------------------------------------------------------- #
# FrameView
# --------------------------------------------------------------------------- #

# FrameView is built on PresentMon and its per-frame log follows the 1.x
# PresentMon layout (TimeInSeconds, MsBetweenPresents), plus NVIDIA telemetry
# columns. NOTE: these names have not yet been checked against a real FrameView
# export from the benchmark PC. That check is part of Phase 2; until then the
# telemetry columns are matched by pattern rather than exact name.
_FRAMEVIEW_ALIASES = {
    "time": ["TimeInSeconds"],
    "frametime": ["MsBetweenPresents"],
    "gpu_busy": ["MsGPUBusy"],
    "cpu_busy": ["MsCPUBusy"],
}
_FRAMEVIEW_GPU_UTIL_RE = re.compile(r"^gpu\d*\s*util.*%", re.IGNORECASE)


def load_frameview_csv(path: str | Path, process_name: str | None = None) -> pd.DataFrame:
    """Parse a FrameView per-frame log (plain or .gz) into the canonical frame table."""
    raw = _read_csv(path)
    raw = _select_process(raw, process_name, path)
    cols = _resolve_columns(raw, _FRAMEVIEW_ALIASES, path, required=("time", "frametime"))
    util_col = next((c for c in raw.columns if _FRAMEVIEW_GPU_UTIL_RE.match(c.strip())), None)

    frames = pd.DataFrame(
        {
            "time_s": pd.to_numeric(raw[cols["time"]], errors="coerce"),
            "frametime_ms": pd.to_numeric(raw[cols["frametime"]], errors="coerce"),
            "gpu_busy_ms": _optional(raw, cols.get("gpu_busy")),
            "gpu_time_ms": np.nan,
            "cpu_busy_ms": _optional(raw, cols.get("cpu_busy")),
            "gpu_util_pct": _optional(raw, util_col),
        }
    )
    return _rebase(_clean(frames, path))


# --------------------------------------------------------------------------- #
# nvidia-smi telemetry (VRAM and GPU utilization)
# --------------------------------------------------------------------------- #

# PresentMon does not log VRAM, so capture runs nvidia-smi alongside it:
#   nvidia-smi --query-gpu=timestamp,memory.used,utilization.gpu
#              --format=csv,nounits -lms 500 -f telemetry.csv
# memory.used is device-wide (all processes, including the desktop and any
# inference workload), which is what matters for "will it fit in 12 GB".


def load_gpu_telemetry_csv(path: str | Path) -> pd.DataFrame:
    """Parse an nvidia-smi --query-gpu CSV log (with or without units)."""
    raw = _read_csv(path, skipinitialspace=True)
    raw.columns = [c.strip() for c in raw.columns]
    ts_col = _find(raw, lambda c: c.lower() == "timestamp")
    mem_col = _find(raw, lambda c: c.lower().startswith("memory.used"))
    util_col = _find(raw, lambda c: c.lower().startswith("utilization.gpu"))
    if ts_col is None or mem_col is None:
        raise ParseError(f"{path}: expected nvidia-smi columns 'timestamp' and 'memory.used'")

    stamps = pd.to_datetime(raw[ts_col].astype(str).str.strip(), format="%Y/%m/%d %H:%M:%S.%f")
    telemetry = pd.DataFrame(
        {
            "time_s": (stamps - stamps.min()).dt.total_seconds(),
            "vram_used_mib": _strip_units(raw[mem_col]),
            "gpu_util_pct": _strip_units(raw[util_col]) if util_col else np.nan,
        }
    )
    return telemetry.dropna(subset=["vram_used_mib"]).sort_values("time_s").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# gflbench (the Metal benchmark renderer in bench/metal, macOS)
# --------------------------------------------------------------------------- #

# gflbench writes PresentMon's column names for the shared metrics, so the same
# definitions apply: CPUStartTime (seconds since the run started),
# MsBetweenPresents (interval between present calls), MsGPUBusy (sum of the
# frame's pass GPU times), MsGPUTime (first pass start to last pass end). It
# adds one MsPass_<name> column per render pass and MetalAllocatedMiB, the
# Metal memory the benchmark process has allocated (Apple silicon has unified
# memory, so there is no separate device-wide VRAM number).
GFLBENCH_PASS_PREFIX = "MsPass_"


@dataclass
class GflbenchLog:
    frames: pd.DataFrame  # canonical frame table (FRAME_COLUMNS)
    passes: pd.DataFrame  # time_s plus one column of GPU ms per pass
    memory: pd.DataFrame  # same layout as load_gpu_telemetry_csv (TELEMETRY_COLUMNS)

    @property
    def pass_names(self) -> list[str]:
        return [c for c in self.passes.columns if c != "time_s"]


def load_gflbench_csv(path: str | Path) -> GflbenchLog:
    raw = _read_csv(path)
    cols = _resolve_columns(raw, _PRESENTMON_ALIASES, path, required=("time", "frametime"))
    pass_cols = [c for c in raw.columns if c.startswith(GFLBENCH_PASS_PREFIX)]
    if not pass_cols:
        raise ParseError(f"{path}: no {GFLBENCH_PASS_PREFIX}* columns; is this a gflbench log?")
    pass_names = [c[len(GFLBENCH_PASS_PREFIX):] for c in pass_cols]

    combined = pd.DataFrame(
        {
            "time_s": pd.to_numeric(raw[cols["time"]], errors="coerce"),
            "frametime_ms": pd.to_numeric(raw[cols["frametime"]], errors="coerce"),
            "gpu_busy_ms": _optional(raw, cols.get("gpu_busy")),
            "gpu_time_ms": _optional(raw, cols.get("gpu_time")),
            "cpu_busy_ms": _optional(raw, cols.get("cpu_busy")),
            "gpu_util_pct": np.nan,
            "vram_used_mib": _optional(raw, "MetalAllocatedMiB" if "MetalAllocatedMiB" in raw else None),
            **{name: pd.to_numeric(raw[col], errors="coerce") for name, col in zip(pass_names, pass_cols)},
        }
    )
    # Same cleaning as the other parsers, applied once so the three tables stay
    # row-aligned (the first frame has no previous present, so no frame time).
    combined = combined.dropna(subset=["time_s", "frametime_ms"])
    combined = combined[combined["frametime_ms"] > 0]
    if combined.empty:
        raise ParseError(f"{path}: no valid frames")
    combined = _rebase(combined.sort_values("time_s").reset_index(drop=True))
    memory = combined[["time_s", "vram_used_mib", "gpu_util_pct"]].dropna(subset=["vram_used_mib"])
    return GflbenchLog(
        frames=combined[FRAME_COLUMNS].copy(),
        passes=combined[["time_s", *pass_names]].copy(),
        memory=memory.reset_index(drop=True),
    )


def load_frames(path: str | Path, capture_tool: str) -> pd.DataFrame:
    """Canonical frame table from any supported per-frame log."""
    if capture_tool == "presentmon":
        return load_presentmon_csv(path)
    if capture_tool == "gflbench":
        return load_gflbench_csv(path).frames
    raise ParseError(f"unknown capture tool {capture_tool!r}")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _read_csv(path: str | Path, **kwargs) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        raise ParseError(f"file not found: {path}")
    try:
        # pandas decompresses .gz automatically based on the extension.
        return pd.read_csv(path, na_values=_NA_VALUES, low_memory=False, **kwargs)
    except pd.errors.EmptyDataError as exc:
        raise ParseError(f"{path}: file is empty") from exc


def _select_process(raw: pd.DataFrame, process_name: str | None, path) -> pd.DataFrame:
    if "Application" not in raw.columns:
        return raw
    apps = raw["Application"].astype(str)
    if process_name:
        mask = apps.str.lower() == process_name.lower()
        if not mask.any():
            raise ParseError(
                f"{path}: no frames for process {process_name!r}; found {sorted(apps.unique())}"
            )
        return raw[mask]
    if apps.nunique() > 1:
        raise ParseError(
            f"{path}: log contains several processes {sorted(apps.unique())}; "
            "pass process_name to pick one"
        )
    return raw


def _select_main_swapchain(raw: pd.DataFrame) -> pd.DataFrame:
    # A game can own more than one swap chain (for example an overlay or a
    # launcher window). The one that presents the most frames is the game view.
    if "SwapChainAddress" not in raw.columns or raw["SwapChainAddress"].nunique() <= 1:
        return raw
    main = raw["SwapChainAddress"].value_counts().idxmax()
    return raw[raw["SwapChainAddress"] == main]


def _resolve_columns(raw, aliases, path, required) -> dict[str, str]:
    lower = {c.strip().lower(): c for c in raw.columns}
    found: dict[str, str] = {}
    for key, names in aliases.items():
        for name in names:
            if name.lower() in lower:
                found[key] = lower[name.lower()]
                break
    missing = [k for k in required if k not in found]
    if missing:
        wanted = {k: aliases[k] for k in missing}
        raise ParseError(f"{path}: missing required columns {wanted}")
    return found


def _optional(raw: pd.DataFrame, column: str | None) -> pd.Series | float:
    if column is None:
        return np.nan
    return pd.to_numeric(raw[column], errors="coerce").to_numpy()


def _clean(frames: pd.DataFrame, path) -> pd.DataFrame:
    frames = frames.dropna(subset=["time_s", "frametime_ms"])
    frames = frames[frames["frametime_ms"] > 0]
    if frames.empty:
        raise ParseError(f"{path}: no valid frames")
    return frames.sort_values("time_s").reset_index(drop=True)[FRAME_COLUMNS]


def _rebase(frames: pd.DataFrame) -> pd.DataFrame:
    # Time zero is the first frame we see from the target process, so the
    # warmup window ("drop the first 10 s") is measured from when the game
    # actually starts presenting, not from when the capture tool started.
    frames = frames.copy()
    frames["time_s"] = frames["time_s"] - frames["time_s"].iloc[0]
    return frames


def _time_in_seconds(time_values: pd.Series, frametime_ms: pd.Series) -> pd.Series:
    span = time_values.iloc[-1] - time_values.iloc[0]
    expected_s = frametime_ms.iloc[1:].sum() / 1000.0
    if expected_s > 0 and span / expected_s > 100:
        return time_values / 1000.0
    return time_values


def _find(raw: pd.DataFrame, predicate) -> str | None:
    return next((c for c in raw.columns if predicate(c)), None)


def _strip_units(series: pd.Series) -> pd.Series:
    # "5432 MiB" -> 5432.0, "97 %" -> 97.0, "5432" -> 5432.0
    return pd.to_numeric(series.astype(str).str.extract(r"([-+]?\d*\.?\d+)")[0], errors="coerce")
