"""Before/after studies: variants, alternating capture order, comparison math, image check."""

import importlib.util
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from gfl.capture import capture_dir_for, run_experiment, with_tag
from gfl.compare import compare_tags, t_critical, welch_interval
from gfl.config import ConfigError, load_experiment, parse_experiment
from gfl.manifest import read_manifest
from gfl.settings import LocalSettings
from gfl.synthetic import simulate_gflbench_frames, to_gflbench

ROOT = Path(__file__).resolve().parents[1]


def fix_experiment(runs=2, **extra):
    raw = {
        "experiment": "fix_test",
        "scene": {"name": "metal", "launcher": "gflbench", "executable": "bench/metal/gflbench",
                  "process_name": "gflbench"},
        "capture": {"runs": runs, "duration_s": 60, "warmup_s": 10, "cooldown_s": 60},
        "variants": {"baseline": [], "fix": ["--shadows-scale", "0.5"]},
        "matrix": {"preset": ["high"], "resolution": ["2560x1440"]},
    }
    raw.update(extra)
    return parse_experiment(raw)


# --- config ------------------------------------------------------------------ #

def test_shipped_fix_config():
    exp = load_experiment(ROOT / "configs/metal_shadow_fix.yaml")
    runs = exp.expand()
    assert [r.tag for r in runs] == ["baseline", "fix"]
    assert runs[0].extra_args == () and runs[1].extra_args == ("--shadows-scale", "0.5")
    assert exp.capture.runs == 5 and exp.capture.cooldown_s == 60


def test_variants_alternate_run_by_run():
    order = [(r.tag, i) for r, i in fix_experiment(runs=3).capture_order()]
    assert order == [("baseline", 1), ("fix", 1), ("baseline", 2), ("fix", 2), ("baseline", 3), ("fix", 3)]


def test_without_variants_order_is_config_by_config():
    exp = parse_experiment({
        "experiment": "plain", "scene": {"name": "metal", "launcher": "gflbench",
                                         "executable": "x", "process_name": "x"},
        "capture": {"runs": 2},
        "matrix": {"preset": ["low", "high"], "resolution": ["1920x1080"]},
    })
    assert [(r.preset, i) for r, i in exp.capture_order()] == [("low", 1), ("low", 2), ("high", 1), ("high", 2)]


@pytest.mark.parametrize("extra, message", [
    ({"tag": "fix"}, "either tag or variants"),
    ({"variants": {"only": []}}, "at least two"),
    ({"variants": {"Base Line": [], "fix": []}}, "tag must be"),
    ({"variants": {"baseline": [], "fix": "--shadows-scale 0.5"}}, "list of arguments"),
])
def test_invalid_variants(extra, message):
    with pytest.raises(ConfigError, match=message):
        fix_experiment(**extra)


def test_tag_override_is_refused_for_variant_experiments():
    with pytest.raises(ConfigError, match="variants"):
        with_tag(fix_experiment(), "fix")


def test_capture_folder_names_both_variants():
    from datetime import datetime
    path = capture_dir_for(fix_experiment(), LocalSettings(), datetime(2026, 11, 18, 9, 0, 0))
    assert path.name == "fix_test_baseline-vs-fix_20261118-090000"


# --- an end-to-end before/after study with a fake renderer ----------------------- #

class FixFakeRunner:
    """Writes gflbench logs; runs with --shadows-scale 0.5 get a cheaper shadow pass."""

    def __init__(self):
        self.commands = []

    def run(self, command, timeout_s):
        self.commands.append(command)
        out = Path(command[command.index("--output") + 1])
        run = fix_experiment().expand()[0]
        sim = simulate_gflbench_frames(run, len(self.commands), 60)
        if "--shadows-scale" in command:
            saved = sim["shadows"] * 0.7
            sim["shadows"] -= saved
            sim["gpu_busy"] -= saved
            sim["frametime"] -= saved
        to_gflbench(sim, run).to_csv(out, index=False)
        out.with_suffix(".json").write_text(json.dumps({"device": "Apple M3", "os": "macOS"}))
        return 0

    def wait(self, seconds, reason):
        pass


def test_fix_study_end_to_end(tmp_path):
    from gfl.analyze import analyze

    runner = FixFakeRunner()
    run_experiment(fix_experiment(runs=3), runner, LocalSettings(), tmp_path / "cap")
    assert ["--shadows-scale" in c for c in runner.commands] == [False, True] * 3

    manifest = read_manifest(tmp_path / "cap")
    fix_notes = manifest.loc[manifest["tag"] == "fix", "notes"]
    assert all(n.endswith("args=--shadows-scale 0.5") for n in fix_notes)

    result = analyze(tmp_path / "cap", tmp_path / "out")
    table = compare_tags(result.per_run)
    ft = table[table["metric"] == "mean_frametime_ms"].iloc[0]
    shadows = table[table["metric"] == "pass_shadows_ms"].iloc[0]
    assert ft["change"] < 0 and ft["real_change"]
    assert shadows["change"] == pytest.approx(ft["change"], rel=0.1)  # the saving is in the shadow pass


# --- comparison math ------------------------------------------------------------- #

def test_welch_interval_hand_computed():
    # Means 11 and 9, each sample variance 1, n = 3: se = sqrt(2/3), df = 4, t = 2.776.
    diff, lo, hi = welch_interval(pd.Series([10.0, 11.0, 12.0]), pd.Series([8.0, 9.0, 10.0]))
    half = 2.776 * math.sqrt(2 / 3)
    assert diff == pytest.approx(-2.0)
    assert (lo, hi) == pytest.approx((-2.0 - half, -2.0 + half))


def test_welch_needs_two_runs_each():
    with pytest.raises(ValueError, match="at least 2 runs"):
        welch_interval(pd.Series([1.0]), pd.Series([1.0, 2.0]))


@pytest.mark.parametrize("df, expected", [(1, 12.706), (4.7, 2.776), (8, 2.306), (31, 2.042), (500, 1.96)])
def test_t_critical(df, expected):
    assert t_critical(df) == expected


def test_compare_flags_noise_as_not_real():
    rows = []
    for tag, values in [("baseline", [49.0, 49.6, 49.3]), ("fix", [49.2, 49.5, 49.1])]:
        for v in values:
            rows.append({"scene": "metal", "preset": "high", "resolution": "2560x1440",
                         "dlss_mode": "native", "frame_generation": False, "inference_streams": 0,
                         "tag": tag, "mean_frametime_ms": v, "avg_fps": 1000 / v,
                         "low_1pct_fps": 15.0, "p99_frametime_ms": 66.0})
    table = compare_tags(pd.DataFrame(rows))
    assert not table.loc[table["metric"] == "mean_frametime_ms", "real_change"].iloc[0]
    with pytest.raises(ValueError, match="no runs tagged 'after'"):
        compare_tags(pd.DataFrame(rows), fix="after")


# --- image check ---------------------------------------------------------------- #

def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_image_difference():
    stills = load_script("compare_stills")
    a = np.zeros((8, 8, 3))
    assert math.isinf(stills.image_difference(a, a.copy())["psnr_db"])
    b = a.copy()
    b[0, 0, :] = 1.0  # one pixel fully different
    d = stills.image_difference(a, b)
    assert d["max_abs_diff_8bit"] == pytest.approx(255)
    assert d["pixels_changed_pct"] == pytest.approx(100 / 64)
    assert d["psnr_db"] == pytest.approx(10 * math.log10(64))  # mse = 1/64
    with pytest.raises(ValueError, match="differ in size"):
        stills.image_difference(a, np.zeros((4, 4, 3)))


def test_compare_stills_script_end_to_end(tmp_path, monkeypatch, capsys):
    import matplotlib.pyplot as plt
    stills = load_script("compare_stills")
    rng = np.random.default_rng(0)
    img = rng.uniform(0, 1, (64, 96, 3))
    plt.imsave(tmp_path / "a.png", img)
    plt.imsave(tmp_path / "b.png", np.clip(img + 0.01, 0, 1))
    monkeypatch.setattr("sys.argv", ["compare_stills", str(tmp_path / "a.png"), str(tmp_path / "b.png"),
                                     str(tmp_path / "out.png")])
    stills.main()
    assert (tmp_path / "out.png").exists()
    assert "PSNR" in capsys.readouterr().out


def test_shipped_edge_followup_config_alternates_three_variants():
    exp = load_experiment(ROOT / "configs/metal_shadow_fix_edge.yaml")
    order = [(r.tag, i) for r, i in exp.capture_order()][:6]
    assert order == [("baseline", 1), ("fix", 1), ("fix-edge", 1), ("baseline", 2), ("fix", 2), ("fix-edge", 2)]
    edge = next(r for r in exp.expand() if r.tag == "fix-edge")
    assert edge.extra_args == ("--shadows-scale", "0.5", "--upsample", "edge")
