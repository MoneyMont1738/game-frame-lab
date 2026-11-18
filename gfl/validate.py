"""PresentMon vs. FrameView agreement on the same runs.

Why this works: both tools read the same Windows ETW events for every Present()
call, so on the same run they should report the same frame times. Agreement
checks the capture chain (process selection, swap chain filtering, parsing,
trimming) against an independent tool. A large difference points to a bug or to
logs from different runs, not to measurement noise.

Steps for each run:
1. Align. FrameView is started by hand with a hotkey, so its log starts and
   ends at different frames than PresentMon's. Find the frame offset (lag) at
   which the two frame-time sequences match best: the one with the smallest
   mean absolute difference. Real frame-time sequences are noisy, so the
   correct offset stands out sharply and wrong offsets do not come close.
2. Compare on the overlap, after dropping the warmup (on PresentMon's clock):
   per-frame differences, and the headline metrics computed by each tool's data.
3. Judge against tolerances (below). They are tight on purpose: two readers of
   the same event stream should agree to well under 1%.

Matching FrameView files to runs: FrameView writes its logs to its own folder.
`attach_frameview` copies each log into the capture folder and assigns it to the
run whose time window contains the log's last-modified time (the moment
FrameView stopped writing).
"""

from __future__ import annotations

import math
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from gfl import manifest as mf
from gfl.metrics import average_fps, frametime_percentile, low_fps
from gfl.parse import ParseError, load_frameview_csv, load_presentmon_csv

# Agreement tolerances (relative difference between tools).
TOLERANCE_AVG_FPS_PCT = 1.0
TOLERANCE_LOW_1PCT_PCT = 3.0
TOLERANCE_P99_PCT = 3.0
# If the best alignment still differs by this much per frame on average, the
# two logs are not the same run (or one of them is filtered differently).
MAX_ALIGNED_MEAN_ABS_DIFF_MS = 0.5
# Search window for the FrameView start offset, and minimum overlap.
MAX_OFFSET_S = 10.0
MIN_OVERLAP_FRACTION = 0.5
PROBE_FRAMES = 600


@dataclass
class Alignment:
    lag: int  # FrameView frame i corresponds to PresentMon frame i + lag
    overlap: int
    mean_abs_diff_ms: float


def align(pm_ft: np.ndarray, fv_ft: np.ndarray, max_lag: int) -> Alignment:
    """Find the frame offset at which two frame-time sequences agree best.

    Each candidate offset is scored on the first PROBE_FRAMES frames of its
    overlap, which keeps the search fast; the winner is then measured on the
    full overlap. This relies on frame times varying from frame to frame, which
    they do in uncapped runs. With a frame cap (near-constant frame times) many
    offsets look alike, but then the metrics agree regardless of the offset.
    """
    min_overlap = max(1, int(MIN_OVERLAP_FRACTION * min(len(pm_ft), len(fv_ft))))
    best_lag, best_score = None, math.inf
    for lag in range(-max_lag, max_lag + 1):
        pm_start, fv_start = max(lag, 0), max(-lag, 0)
        n = min(len(pm_ft) - pm_start, len(fv_ft) - fv_start)
        if n < min_overlap:
            continue
        k = min(n, PROBE_FRAMES)
        score = np.abs(pm_ft[pm_start:pm_start + k] - fv_ft[fv_start:fv_start + k]).mean()
        if score < best_score:
            best_lag, best_score = lag, score
    if best_lag is None:
        raise ValueError("the two logs do not overlap enough to align")
    pm_start, fv_start = max(best_lag, 0), max(-best_lag, 0)
    n = min(len(pm_ft) - pm_start, len(fv_ft) - fv_start)
    diff = np.abs(pm_ft[pm_start:pm_start + n] - fv_ft[fv_start:fv_start + n]).mean()
    return Alignment(best_lag, n, float(diff))


@dataclass
class RunValidation:
    run_id: str
    lag_frames: int
    overlap_frames: int
    mean_abs_diff_ms: float
    p99_abs_diff_ms: float
    max_abs_diff_ms: float
    pm_avg_fps: float
    fv_avg_fps: float
    avg_fps_diff_pct: float
    pm_low_1pct_fps: float
    fv_low_1pct_fps: float
    low_1pct_diff_pct: float
    pm_p99_ms: float
    fv_p99_ms: float
    p99_diff_pct: float
    aligned: bool
    agree: bool

    def as_dict(self) -> dict:
        return asdict(self)


def _pct(a: float, b: float) -> float:
    return 100.0 * (b - a) / a


def validate_pair(run_id: str, pm: pd.DataFrame, fv: pd.DataFrame,
                  warmup_s: float, duration_s: float) -> RunValidation:
    """Compare one run's PresentMon and FrameView frame tables."""
    pm_ft = pm["frametime_ms"].to_numpy()
    fv_ft = fv["frametime_ms"].to_numpy()
    pm_fps = average_fps(pm_ft)
    max_lag = max(1, math.ceil(MAX_OFFSET_S * pm_fps))
    a = align(pm_ft, fv_ft, max_lag)

    # Overlapping frames, labeled with PresentMon's timeline, then the same
    # warmup/duration window the analysis uses.
    pm_start, fv_start = max(a.lag, 0), max(-a.lag, 0)
    pm_part = pm.iloc[pm_start:pm_start + a.overlap].reset_index(drop=True)
    fv_part = fv.iloc[fv_start:fv_start + a.overlap].reset_index(drop=True)
    window = (pm_part["time_s"] >= warmup_s) & (pm_part["time_s"] < duration_s)
    if not window.any():
        raise ValueError(f"{run_id}: FrameView does not cover the measured window")
    p, f = pm_part.loc[window, "frametime_ms"], fv_part.loc[window, "frametime_ms"]
    abs_diff = (p - f).abs()

    pm_avg, fv_avg = average_fps(p), average_fps(f)
    pm_low, fv_low = low_fps(p, 0.01), low_fps(f, 0.01)
    pm_p99, fv_p99 = frametime_percentile(p, 99), frametime_percentile(f, 99)
    aligned = a.mean_abs_diff_ms <= MAX_ALIGNED_MEAN_ABS_DIFF_MS
    result = RunValidation(
        run_id=run_id,
        lag_frames=a.lag,
        overlap_frames=int(window.sum()),
        mean_abs_diff_ms=float(abs_diff.mean()),
        p99_abs_diff_ms=frametime_percentile(abs_diff, 99),
        max_abs_diff_ms=float(abs_diff.max()),
        pm_avg_fps=pm_avg, fv_avg_fps=fv_avg, avg_fps_diff_pct=_pct(pm_avg, fv_avg),
        pm_low_1pct_fps=pm_low, fv_low_1pct_fps=fv_low, low_1pct_diff_pct=_pct(pm_low, fv_low),
        pm_p99_ms=pm_p99, fv_p99_ms=fv_p99, p99_diff_pct=_pct(pm_p99, fv_p99),
        aligned=aligned,
        agree=False,
    )
    result.agree = aligned and (
        abs(result.avg_fps_diff_pct) <= TOLERANCE_AVG_FPS_PCT
        and abs(result.low_1pct_diff_pct) <= TOLERANCE_LOW_1PCT_PCT
        and abs(result.p99_diff_pct) <= TOLERANCE_P99_PCT
    )
    return result


def validate_directory(run_dir: str | Path) -> tuple[pd.DataFrame, str]:
    """Validate every run in a capture folder that has a FrameView log."""
    run_dir = Path(run_dir)
    manifest = mf.read_manifest(run_dir)
    rows = []
    for _, row in manifest.iterrows():
        fv_path = mf.resolve(run_dir, row["frameview_csv"])
        if fv_path is None or row["capture_tool"] != "presentmon":
            continue
        pm = load_presentmon_csv(mf.resolve(run_dir, row["frames_csv"]))
        fv = load_frameview_csv(fv_path)
        rows.append(validate_pair(row["run_id"], pm, fv, row["warmup_s"], row["duration_s"]).as_dict())
    if not rows:
        raise ValueError(
            f"no runs in {run_dir} have a FrameView log; attach them first with "
            "gfl validate <folder> --frameview-dir <FrameView log folder>"
        )
    return pd.DataFrame(rows), mf.data_kind_of(manifest)


def validation_markdown(table: pd.DataFrame, synthetic: bool, banner: str) -> str:
    from gfl.tables import markdown_table

    agree = int(table["agree"].sum())
    lines = [f"> **{banner}**", ""] if synthetic else []
    lines += [
        "# PresentMon vs. FrameView validation",
        "",
        f"{agree} of {len(table)} runs agree within tolerance "
        f"(average FPS {TOLERANCE_AVG_FPS_PCT:g}%, 1% low {TOLERANCE_LOW_1PCT_PCT:g}%, "
        f"p99 frame time {TOLERANCE_P99_PCT:g}%).",
        "",
        f"Largest differences across runs: average FPS {table['avg_fps_diff_pct'].abs().max():.3f}%, "
        f"1% low {table['low_1pct_diff_pct'].abs().max():.3f}%, "
        f"p99 frame time {table['p99_diff_pct'].abs().max():.3f}%, "
        f"per-frame mean absolute difference {table['mean_abs_diff_ms'].max():.4f} ms.",
        "",
    ]
    shown = pd.DataFrame({
        "Run": table["run_id"],
        "Offset (frames)": table["lag_frames"],
        "Frames compared": table["overlap_frames"],
        "Mean abs diff ms": table["mean_abs_diff_ms"].map("{:.4f}".format),
        "Avg FPS diff %": table["avg_fps_diff_pct"].map("{:+.3f}".format),
        "1% low diff %": table["low_1pct_diff_pct"].map("{:+.3f}".format),
        "p99 diff %": table["p99_diff_pct"].map("{:+.3f}".format),
        "Agree": table["agree"].map({True: "yes", False: "NO"}),
    })
    lines.append(markdown_table(shown))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Attaching FrameView logs to runs
# --------------------------------------------------------------------------- #

# Slack after a run's capture window during which FrameView may still be
# stopped by hand.
ATTACH_SLACK_S = 30.0


def attach_frameview(run_dir: str | Path, frameview_dir: str | Path) -> list[str]:
    """Copy FrameView logs into the capture folder and record them in the manifest.

    Returns human-readable notes about what was matched and what was not.
    """
    run_dir, frameview_dir = Path(run_dir), Path(frameview_dir)
    manifest = mf.read_manifest(run_dir)
    windows = []
    for i, row in manifest.iterrows():
        start = datetime.fromisoformat(row["captured_at"])
        end = start + timedelta(seconds=float(row["duration_s"]) + ATTACH_SLACK_S)
        windows.append((i, start, end))

    notes, claimed = [], {}
    for path in sorted(frameview_dir.glob("*.csv")):
        try:
            load_frameview_csv(path)  # skips FrameView summary files and anything else
        except (ParseError, ValueError):
            notes.append(f"skipped {path.name}: not a FrameView per-frame log")
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime)
        hits = [i for i, start, end in windows if start <= modified <= end]
        if len(hits) != 1:
            notes.append(f"unmatched {path.name}: last written {modified:%Y-%m-%d %H:%M:%S}, "
                         f"matches {len(hits)} runs")
            continue
        i = hits[0]
        run_id = manifest.at[i, "run_id"]
        if i in claimed:
            notes.append(f"unmatched {path.name}: run {run_id} already has {claimed[i]}")
            continue
        target = f"frameview_{run_id}.csv"
        shutil.copy2(path, run_dir / target)
        manifest.at[i, "frameview_csv"] = target
        claimed[i] = path.name
        notes.append(f"attached {path.name} -> {run_id}")

    mf.write_manifest(manifest.to_dict("records"), run_dir)
    return notes
