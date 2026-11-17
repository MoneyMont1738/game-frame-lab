"""Frame-time metrics.

All metrics are computed from frame times in milliseconds after the warmup
window has been removed. The definitions below are deliberate; different tools
use different ones, so they are spelled out here and pinned by tests.

Average FPS
    frames / elapsed time = 1000 / mean(frame time).
    NOT the mean of per-frame FPS (1000 / frame time), which over-weights fast
    frames and inflates the number. Example: frames of 10 ms and 30 ms take
    40 ms for 2 frames = 50 FPS; the mean of per-frame FPS would say 66.7.

p99 frame time
    The 99th percentile of frame times, using linear interpolation between the
    two nearest ranks (numpy method="linear", the same as Excel PERCENTILE.INC).
    Percentiles are frame-weighted: every frame counts once regardless of how
    long it lasted.

1% low and 0.1% low (FPS)
    The average FPS of the slowest 1% (or 0.1%) of frames:
    1000 / mean(the slowest k frame times), with k = ceil(n * fraction).
    Some tools instead report 1000 / p99 frame time and call it "1% low". The
    two answer different questions (typical worst-case frame vs. the cutoff for
    the worst 1%), so this toolkit reports the average-of-worst version as the
    "low" and the cutoff separately as p99 frame time.

Stutter count
    Frames whose frame time is more than 2x the run's median frame time. Using
    the median (not the mean) keeps the threshold stable even when the run
    contains many spikes. Also reported per minute so runs of different length
    compare fairly.

GPU busy %
    100 * sum(GPU busy time) / sum(frame time) over the measured window, from
    PresentMon's MsGPUBusy. Close to 100% means the GPU is the bottleneck;
    well below 100% means the GPU waits on the CPU (or on a frame cap).

VRAM
    Device-wide memory.used from the nvidia-smi sidecar log: mean and peak
    over the measured window.

Run-to-run variance
    Each run is measured on its own; a config's headline numbers are the mean
    across its runs. Variance is the sample standard deviation (ddof=1, since 3
    runs are a sample, not the population) and the coefficient of variation
    (std / mean) of the per-run average FPS.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

STUTTER_FACTOR = 2.0
PERCENTILE_METHOD = "linear"

# The 0.1% low of a 50 s run at 60 FPS rests on only 3 frames. Below this many
# frames in the slowest 0.1%, that metric is flagged as low confidence.
MIN_FRAMES_FOR_TAIL = 5


# --------------------------------------------------------------------------- #
# Building blocks (pure functions on arrays of frame times in ms)
# --------------------------------------------------------------------------- #


def _as_array(frametimes_ms) -> np.ndarray:
    arr = np.asarray(frametimes_ms, dtype=float)
    if arr.size == 0:
        raise ValueError("no frames to measure")
    if np.isnan(arr).any():
        raise ValueError("frame times contain NaN")
    return arr


def average_fps(frametimes_ms) -> float:
    ft = _as_array(frametimes_ms)
    return 1000.0 * ft.size / ft.sum()


def frametime_percentile(frametimes_ms, q: float) -> float:
    """q-th percentile frame time in ms (q in 0..100), linear interpolation."""
    ft = _as_array(frametimes_ms)
    return float(np.percentile(ft, q, method=PERCENTILE_METHOD))


def slowest_frame_count(n_frames: int, fraction: float) -> int:
    """How many frames make up the slowest `fraction` of a run (at least one)."""
    return max(1, math.ceil(n_frames * fraction))


def low_fps(frametimes_ms, fraction: float) -> float:
    """Average FPS of the slowest `fraction` of frames (0.01 for the 1% low)."""
    ft = _as_array(frametimes_ms)
    k = slowest_frame_count(ft.size, fraction)
    slowest = np.sort(ft)[-k:]
    return 1000.0 / slowest.mean()


def stutter_count(frametimes_ms, factor: float = STUTTER_FACTOR) -> int:
    ft = _as_array(frametimes_ms)
    threshold = factor * np.median(ft)
    return int((ft > threshold).sum())


def gpu_busy_pct(gpu_busy_ms, frametimes_ms) -> float:
    """Share of wall time the GPU was busy, over frames that report GPU busy."""
    busy = np.asarray(gpu_busy_ms, dtype=float)
    ft = np.asarray(frametimes_ms, dtype=float)
    known = ~np.isnan(busy)
    if not known.any():
        return float("nan")
    return 100.0 * busy[known].sum() / ft[known].sum()


# --------------------------------------------------------------------------- #
# Per-run and per-config metrics
# --------------------------------------------------------------------------- #


def trim_window(df: pd.DataFrame, warmup_s: float, duration_s: float | None = None) -> pd.DataFrame:
    """Keep rows with warmup_s <= time_s < duration_s.

    Trimming by time (not by frame count) keeps the measured window the same
    length, 50 s by default, for every config regardless of its frame rate.
    """
    mask = df["time_s"] >= warmup_s
    if duration_s is not None:
        mask &= df["time_s"] < duration_s
    trimmed = df[mask]
    if trimmed.empty:
        raise ValueError(
            f"no frames left after trimming to [{warmup_s}, {duration_s}) s; "
            "the capture is shorter than the warmup"
        )
    return trimmed


@dataclass
class RunMetrics:
    frames: int
    measured_s: float
    avg_fps: float
    mean_frametime_ms: float
    median_frametime_ms: float
    p99_frametime_ms: float
    low_1pct_fps: float
    low_01pct_fps: float
    low_01pct_low_confidence: bool
    stutter_count: int
    stutters_per_min: float
    gpu_busy_pct: float
    vram_mean_mib: float
    vram_peak_mib: float

    def as_dict(self) -> dict:
        return asdict(self)


def compute_run_metrics(frames: pd.DataFrame, telemetry: pd.DataFrame | None = None) -> RunMetrics:
    """Metrics for one already-trimmed run.

    `frames` is the canonical frame table from gfl.parse; `telemetry` is the
    trimmed nvidia-smi table (optional).
    """
    ft = _as_array(frames["frametime_ms"])
    measured_s = ft.sum() / 1000.0
    stutters = stutter_count(ft)

    if telemetry is not None and not telemetry.empty:
        vram_mean = float(telemetry["vram_used_mib"].mean())
        vram_peak = float(telemetry["vram_used_mib"].max())
    else:
        vram_mean = vram_peak = float("nan")

    return RunMetrics(
        frames=int(ft.size),
        measured_s=measured_s,
        avg_fps=average_fps(ft),
        mean_frametime_ms=float(ft.mean()),
        median_frametime_ms=float(np.median(ft)),
        p99_frametime_ms=frametime_percentile(ft, 99),
        low_1pct_fps=low_fps(ft, 0.01),
        low_01pct_fps=low_fps(ft, 0.001),
        low_01pct_low_confidence=ft.size * 0.001 < MIN_FRAMES_FOR_TAIL,
        stutter_count=stutters,
        stutters_per_min=stutters / (measured_s / 60.0),
        gpu_busy_pct=gpu_busy_pct(frames["gpu_busy_ms"], ft),
        vram_mean_mib=vram_mean,
        vram_peak_mib=vram_peak,
    )


# Metrics averaged across runs to give a config's headline numbers.
_MEAN_ACROSS_RUNS = [
    "avg_fps",
    "mean_frametime_ms",
    "p99_frametime_ms",
    "low_1pct_fps",
    "low_01pct_fps",
    "stutter_count",
    "stutters_per_min",
    "gpu_busy_pct",
    "vram_mean_mib",
]


def summarize_runs(per_run: pd.DataFrame) -> dict:
    """Combine the per-run metrics of one config into one summary row."""
    if per_run.empty:
        raise ValueError("no runs to summarize")
    summary: dict = {"runs": len(per_run), "frames": int(per_run["frames"].sum())}
    for col in _MEAN_ACROSS_RUNS:
        summary[col] = float(per_run[col].mean())
    summary["vram_peak_mib"] = float(per_run["vram_peak_mib"].max())

    # Sample std (ddof=1). With a single run there is no spread to measure.
    fps = per_run["avg_fps"]
    std = float(fps.std(ddof=1)) if len(fps) > 1 else float("nan")
    summary["avg_fps_std"] = std
    summary["avg_fps_cv_pct"] = 100.0 * std / fps.mean() if len(fps) > 1 else float("nan")
    summary["low_01pct_low_confidence"] = bool(per_run["low_01pct_low_confidence"].any())
    return summary
