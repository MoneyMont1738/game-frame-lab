"""PresentMon vs. FrameView alignment, agreement, and log attachment."""

import os
import shutil
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from gfl.manifest import read_manifest
from gfl.parse import load_frameview_csv, load_presentmon_csv
from gfl.validate import align, attach_frameview, validate_directory, validate_pair

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "sample" / "synthetic_lyra_scalability"
RUN = "lyra_high_2560x1440_native_fg-off_s0_baseline_run1"


def load_pair(run=RUN):
    pm = load_presentmon_csv(SAMPLE / f"SYNTHETIC_presentmon_{run}.csv.gz")
    fv = load_frameview_csv(SAMPLE / f"SYNTHETIC_frameview_{run}.csv.gz")
    return pm, fv


def test_align_recovers_a_known_offset():
    rng = np.random.default_rng(1)
    pm = rng.uniform(8, 12, 3000)
    fv = pm[137:2800] + rng.normal(0, 0.002, 2663)  # starts 137 frames late
    a = align(pm, fv, max_lag=500)
    assert a.lag == 137
    assert a.overlap == 2663
    assert a.mean_abs_diff_ms < 0.01


def test_align_handles_frameview_starting_first():
    rng = np.random.default_rng(2)
    fv = rng.uniform(8, 12, 3000)
    pm = fv[50:]
    assert align(pm, fv, max_lag=200).lag == -50


def test_synthetic_pair_agrees_and_lag_matches_the_generator():
    # Rebuild the generator's ground truth: FrameView starts at a known time,
    # so the correct lag is the number of frames presented before it.
    from gfl.config import load_experiment
    from gfl.synthetic import DEFAULT_CONFIG, _seed, simulate_frames

    root = Path(__file__).resolve().parents[1]
    exp = load_experiment(root / DEFAULT_CONFIG)
    run = next(r for r in exp.expand() if RUN.startswith(r.config_id))
    sim = simulate_frames(run, 1, exp.capture.duration_s, exp.capture.warmup_s)
    fv_start = np.random.default_rng(_seed("frameview", RUN)).uniform(0.6, 1.2)
    true_lag = int((sim["t"] < fv_start).sum())

    pm, fv = load_pair()
    result = validate_pair(RUN, pm, fv, warmup_s=10, duration_s=60)
    assert result.lag_frames == true_lag
    assert result.aligned and result.agree
    assert abs(result.avg_fps_diff_pct) < 0.01
    assert result.mean_abs_diff_ms < 0.01


def test_logs_from_different_runs_are_flagged():
    pm, _ = load_pair(RUN)
    _, other_fv = load_pair("lyra_high_2560x1440_native_fg-off_s0_baseline_run2")
    result = validate_pair(RUN, pm, other_fv, warmup_s=10, duration_s=60)
    assert not result.aligned
    assert not result.agree


def test_a_systematic_frame_time_error_is_caught():
    pm, fv = load_pair()
    fv = fv.assign(frametime_ms=fv["frametime_ms"] * 1.03)  # 3% off: e.g. wrong process filter
    result = validate_pair(RUN, pm, fv, warmup_s=10, duration_s=60)
    assert not result.agree


def test_whole_sample_directory_validates():
    table, kind = validate_directory(SAMPLE)
    assert kind == "synthetic"
    assert len(table) == 36 and table["agree"].all()


def test_attach_frameview_matches_logs_by_time(tmp_path):
    cap = tmp_path / "cap"
    cap.mkdir()
    manifest = pd.read_csv(SAMPLE / "manifest.csv").head(2)
    for name in manifest["presentmon_csv"]:
        shutil.copy(SAMPLE / name, cap / name)
    manifest["frameview_csv"] = ""
    manifest["telemetry_csv"] = ""
    manifest["captured_at"] = ["2026-03-01T10:00:00", "2026-03-01T10:02:00"]
    manifest.to_csv(cap / "manifest.csv", index=False)

    fv_dir = tmp_path / "frameview"
    fv_dir.mkdir()
    for i, run_id in enumerate(manifest["run_id"]):
        target = fv_dir / f"FrameView_LyraGame_{i}_Log.csv"
        target.write_bytes(load_frameview_csv(SAMPLE / f"SYNTHETIC_frameview_{run_id}.csv.gz")
                           .rename(columns={"time_s": "TimeInSeconds",
                                            "frametime_ms": "MsBetweenPresents"})
                           .to_csv(index=False).encode())
        # FrameView stops writing near the end of each 60 s run.
        stopped = datetime(2026, 3, 1, 10, 2 * i, 58).timestamp()
        os.utime(target, (stopped, stopped))
    (fv_dir / "FrameView_Summary.csv").write_text("Application,Avg FPS\nLyraGame.exe,90\n")
    stray = fv_dir / "FrameView_Other_Log.csv"
    shutil.copy(fv_dir / "FrameView_LyraGame_0_Log.csv", stray)
    late = datetime(2026, 3, 1, 11, 0, 0).timestamp()
    os.utime(stray, (late, late))

    notes = attach_frameview(cap, fv_dir)
    updated = read_manifest(cap)
    assert updated["frameview_csv"].tolist() == [f"frameview_{r}.csv" for r in manifest["run_id"]]
    assert any(n.startswith("skipped FrameView_Summary.csv") for n in notes)
    assert any(n.startswith("unmatched FrameView_Other_Log.csv") for n in notes)
    table, _ = validate_directory(cap)
    assert table["agree"].all()
