"""Metric definitions, checked against values computed by hand."""

import math

import numpy as np
import pandas as pd
import pytest

from gfl import metrics as m


def frames_df(frametimes, gpu_busy=None):
    ft = np.asarray(frametimes, dtype=float)
    return pd.DataFrame({
        "time_s": np.concatenate([[0.0], np.cumsum(ft)[:-1] / 1000.0]),
        "frametime_ms": ft,
        "gpu_busy_ms": np.nan if gpu_busy is None else gpu_busy,
    })


# --- average FPS ---------------------------------------------------------- #

def test_average_fps_is_frames_over_time_not_mean_of_instant_fps():
    # 2 frames in 40 ms = 50 FPS. Averaging 100 FPS and 33.3 FPS would give 66.7.
    assert m.average_fps([10.0, 30.0]) == pytest.approx(50.0)


def test_average_fps_constant_frames():
    assert m.average_fps([16.0] * 100) == pytest.approx(62.5)


def test_empty_and_nan_inputs_are_rejected():
    with pytest.raises(ValueError):
        m.average_fps([])
    with pytest.raises(ValueError):
        m.average_fps([10.0, float("nan")])


# --- percentiles ---------------------------------------------------------- #

def test_p99_uses_linear_interpolation():
    # Values 1..100: rank position 0.99 * (100 - 1) = 98.01 (0-based), so p99 is
    # 99 + 0.01 * (100 - 99) = 99.01. Nearest-rank would give 99 or 100 instead.
    assert m.frametime_percentile(np.arange(1, 101), 99) == pytest.approx(99.01)


def test_p50_of_even_count_is_midpoint():
    assert m.frametime_percentile([10, 20, 30, 40], 50) == pytest.approx(25.0)


# --- 1% and 0.1% lows ----------------------------------------------------- #

@pytest.mark.parametrize("n, fraction, expected", [
    (1000, 0.01, 10),
    (999, 0.01, 10),     # 9.99 rounds up
    (1001, 0.01, 11),    # 10.01 rounds up
    (50, 0.01, 1),       # never zero frames
    (1000, 0.001, 1),
    (2500, 0.001, 3),    # 2.5 rounds up
])
def test_slowest_frame_count(n, fraction, expected):
    assert m.slowest_frame_count(n, fraction) == expected


def test_one_percent_low_averages_the_slowest_one_percent():
    # 990 frames at 10 ms and 10 frames at 40 ms: the slowest 1% is exactly the
    # ten 40 ms frames, so the 1% low is 1000 / 40 = 25 FPS.
    ft = [10.0] * 990 + [40.0] * 10
    assert m.low_fps(ft, 0.01) == pytest.approx(25.0)


def test_one_percent_low_mixes_values_when_tail_is_uneven():
    # Slowest 10 of 1000: five at 50 ms and five at 30 ms -> mean 40 ms -> 25 FPS.
    ft = [10.0] * 990 + [50.0] * 5 + [30.0] * 5
    assert m.low_fps(ft, 0.01) == pytest.approx(25.0)


def test_point_one_percent_low_of_1000_frames_is_the_single_worst_frame():
    ft = [10.0] * 999 + [80.0]
    assert m.low_fps(ft, 0.001) == pytest.approx(12.5)


def test_one_percent_low_differs_from_one_over_p99():
    # The two common "1% low" definitions disagree; this pins which one we use.
    ft = np.arange(1, 101, dtype=float)  # 100 frames, 1..100 ms
    assert m.low_fps(ft, 0.01) == pytest.approx(1000 / 100)          # worst frame only
    assert 1000 / m.frametime_percentile(ft, 99) == pytest.approx(1000 / 99.01)


def test_low_is_order_independent():
    rng = np.random.default_rng(0)
    ft = rng.uniform(5, 30, 5000)
    assert m.low_fps(ft, 0.01) == pytest.approx(m.low_fps(rng.permutation(ft), 0.01))


# --- stutter -------------------------------------------------------------- #

def test_stutter_threshold_is_strictly_more_than_twice_the_median():
    # Median 10 ms -> threshold 20 ms. 20.0 is not a stutter; 20.01 and 45 are.
    ft = [10.0] * 97 + [20.0, 20.01, 45.0]
    assert m.stutter_count(ft) == 2


def test_stutter_uses_median_not_mean():
    # Many big spikes raise the mean a lot but barely move the median.
    ft = [10.0] * 80 + [100.0] * 20
    assert np.mean(ft) == pytest.approx(28.0)
    assert m.stutter_count(ft) == 20  # threshold 20 ms (2 x median), not 56 ms


# --- GPU busy --------------------------------------------------------------- #

def test_gpu_busy_pct_is_busy_time_over_wall_time():
    assert m.gpu_busy_pct([5.0, 10.0], [10.0, 10.0]) == pytest.approx(75.0)


def test_gpu_busy_pct_ignores_frames_without_data_and_is_nan_without_any():
    assert m.gpu_busy_pct([5.0, np.nan], [10.0, 30.0]) == pytest.approx(50.0)
    assert math.isnan(m.gpu_busy_pct([np.nan, np.nan], [10.0, 10.0]))


# --- trimming --------------------------------------------------------------- #

def test_trim_window_keeps_start_and_drops_end():
    df = pd.DataFrame({"time_s": [9.99, 10.0, 30.0, 59.99, 60.0], "frametime_ms": 1.0})
    kept = m.trim_window(df, warmup_s=10, duration_s=60)
    assert kept["time_s"].tolist() == [10.0, 30.0, 59.99]


def test_trim_window_raises_when_nothing_is_left():
    df = pd.DataFrame({"time_s": [1.0, 2.0], "frametime_ms": 1.0})
    with pytest.raises(ValueError, match="warmup"):
        m.trim_window(df, warmup_s=10)


# --- per-run and per-config -------------------------------------------------- #

def test_compute_run_metrics_on_known_run():
    ft = [10.0] * 990 + [40.0] * 10
    run = m.compute_run_metrics(frames_df(ft, gpu_busy=[9.0] * 990 + [9.0] * 10))
    assert run.frames == 1000
    assert run.measured_s == pytest.approx(10.3)
    assert run.avg_fps == pytest.approx(1000 / 10.3)
    assert run.median_frametime_ms == 10.0
    assert run.low_1pct_fps == pytest.approx(25.0)
    assert run.low_01pct_fps == pytest.approx(25.0)
    assert run.low_01pct_low_confidence  # 0.1% of 1000 frames is 1 frame
    assert run.stutter_count == 10
    assert run.stutters_per_min == pytest.approx(10 / (10.3 / 60))
    assert run.gpu_busy_pct == pytest.approx(100 * 9000 / 10300)
    assert math.isnan(run.vram_peak_mib)


def test_compute_run_metrics_reads_vram_from_telemetry():
    telemetry = pd.DataFrame({"time_s": [10, 10.5, 11], "vram_used_mib": [5000, 6000, 5500]})
    run = m.compute_run_metrics(frames_df([10.0] * 100), telemetry)
    assert run.vram_mean_mib == pytest.approx(5500)
    assert run.vram_peak_mib == 6000


def test_summarize_runs_uses_sample_std_across_runs():
    per_run = pd.DataFrame({
        "frames": [1000, 1000, 1000],
        "avg_fps": [100.0, 102.0, 104.0],
        "mean_frametime_ms": 10.0, "p99_frametime_ms": 12.0,
        "low_1pct_fps": 80.0, "low_01pct_fps": 70.0, "low_01pct_low_confidence": False,
        "stutter_count": [1, 2, 3], "stutters_per_min": 1.0, "gpu_busy_pct": 95.0,
        "vram_mean_mib": 5000.0, "vram_peak_mib": [5100.0, 5300.0, 5200.0],
    })
    s = m.summarize_runs(per_run)
    assert s["runs"] == 3 and s["frames"] == 3000
    assert s["avg_fps"] == pytest.approx(102.0)
    assert s["avg_fps_std"] == pytest.approx(2.0)          # ddof=1; population std is 1.63
    assert s["avg_fps_cv_pct"] == pytest.approx(100 * 2 / 102)
    assert s["stutter_count"] == pytest.approx(2.0)
    assert s["vram_peak_mib"] == 5300.0                    # peak is a max, not a mean


def test_summarize_single_run_has_no_spread():
    per_run = pd.DataFrame({
        "frames": [100], "avg_fps": [60.0], "mean_frametime_ms": 16.7, "p99_frametime_ms": 20.0,
        "low_1pct_fps": 50.0, "low_01pct_fps": 45.0, "low_01pct_low_confidence": True,
        "stutter_count": 0, "stutters_per_min": 0.0, "gpu_busy_pct": 90.0,
        "vram_mean_mib": 1.0, "vram_peak_mib": 1.0,
    })
    s = m.summarize_runs(per_run)
    assert math.isnan(s["avg_fps_std"]) and math.isnan(s["avg_fps_cv_pct"])
