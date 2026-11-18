"""The macOS capture path: gflbench log parsing, config, launch, capture, analysis."""

import json
from pathlib import Path

import pandas as pd
import pytest

from gfl.analyze import analyze
from gfl.capture import gflbench_note, run_experiment
from gfl.config import ConfigError, parse_experiment
from gfl.launch import build_gflbench_command
from gfl.manifest import read_manifest
from gfl.parse import ParseError, load_frames, load_gflbench_csv
from gfl.settings import LocalSettings
from gfl.synthetic import simulate_gflbench_frames, to_gflbench

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data" / "sample" / "synthetic_metal_bench"
HEADER = ("Application,ProcessID,SwapChainAddress,PresentRuntime,SyncInterval,PresentFlags,"
          "AllowsTearing,PresentMode,CPUStartTime,MsCPUBusy,MsGPUTime,MsGPUBusy,"
          "MsBetweenPresents,MsBetweenDisplayChange,MsPass_gbuffer,MsPass_shadows,MsPass_ao,"
          "MsPass_composite,MetalAllocatedMiB")


def bench_experiment(runs=1, **matrix):
    return parse_experiment({
        "experiment": "metal_test",
        "scene": {"name": "gflbench", "launcher": "gflbench", "executable": "bench/metal/gflbench",
                  "process_name": "gflbench"},
        "capture": {"runs": runs, "duration_s": 60, "warmup_s": 10, "cooldown_s": 15},
        "matrix": {"preset": ["low", "epic"], "resolution": ["2560x1440"], **matrix},
    })


def test_parse_gflbench_log(tmp_path):
    path = tmp_path / "bench.csv"
    path.write_text("\n".join([
        HEADER,
        "gflbench,1,CAMetalLayer,Metal,0,0,0,CAMetalLayer,0.000000,1.0,9.0,8.5,NA,NA,3.0,3.0,2.0,0.5,300.0",
        "gflbench,1,CAMetalLayer,Metal,0,0,0,CAMetalLayer,0.010000,1.1,9.2,8.8,10.0,10.0,3.1,3.1,2.1,0.5,301.0",
        "gflbench,1,CAMetalLayer,Metal,0,0,0,CAMetalLayer,0.020500,1.2,9.4,9.0,10.5,NA,3.2,3.2,2.1,0.5,302.0",
    ]) + "\n")
    log = load_gflbench_csv(path)
    # The first frame has no previous present, so no frame time: dropped.
    assert log.frames["frametime_ms"].tolist() == [10.0, 10.5]
    assert log.frames["time_s"].tolist() == pytest.approx([0.0, 0.0105])
    assert log.frames["gpu_busy_ms"].tolist() == [8.8, 9.0]
    assert log.pass_names == ["gbuffer", "shadows", "ao", "composite"]
    assert log.passes["shadows"].tolist() == [3.1, 3.2]
    assert log.memory["vram_used_mib"].tolist() == [301.0, 302.0]
    assert load_frames(path, "gflbench").equals(log.frames)


def test_gflbench_parser_rejects_a_presentmon_log(tmp_path):
    path = tmp_path / "pm.csv"
    path.write_text("Application,CPUStartTime,MsBetweenPresents\nx.exe,0.0,10.0\nx.exe,0.01,10.0\n")
    with pytest.raises(ParseError, match="MsPass_"):
        load_gflbench_csv(path)


def test_gflbench_config_rejects_dlss_and_inference():
    with pytest.raises(ConfigError, match="gflbench has no DLSS"):
        bench_experiment(dlss_mode=["quality"])
    with pytest.raises(ConfigError, match="gflbench has no DLSS"):
        bench_experiment(inference_streams=[0, 1])


def test_gflbench_command():
    exp = bench_experiment()
    run = exp.expand()[0]
    cmd = build_gflbench_command(exp.scene, run, Path("out/run.csv"), 60)
    assert cmd == ["bench/metal/gflbench", "--preset", "low", "--resolution", "2560x1440",
                   "--duration", "60", "--output", str(Path("out/run.csv"))]


class BenchFakeRunner:
    """Stands in for running gflbench: writes the log and JSON it would write."""

    def __init__(self):
        self.steps = []

    def run(self, command, timeout_s):
        self.steps.append(("run", command[0], timeout_s))
        out = Path(command[command.index("--output") + 1])
        preset = command[command.index("--preset") + 1]
        run = next(r for r in bench_experiment().expand() if r.preset == preset)
        to_gflbench(simulate_gflbench_frames(run, 1, 60), run).to_csv(out, index=False)
        out.with_suffix(".json").write_text(json.dumps({"device": "Apple M3", "os": "macOS 26.5.1"}))
        return 0

    def wait(self, seconds, reason):
        self.steps.append(("wait", seconds))

    def start(self, command):  # gflbench needs no background tools
        raise AssertionError("gflbench capture should not start background processes")

    def stop(self, handle):
        raise AssertionError("unexpected stop")

    def prompt(self, message):
        raise AssertionError("unexpected prompt")


def test_gflbench_capture_sequence_and_manifest(tmp_path):
    runner = BenchFakeRunner()
    run_experiment(bench_experiment(runs=2), runner, LocalSettings(), tmp_path)
    assert [s[0] for s in runner.steps] == ["run", "wait", "run", "wait", "run", "wait", "run"]
    assert runner.steps[0][2] == 60 + LocalSettings().presentmon_timeout_margin_s
    manifest = read_manifest(tmp_path)
    assert (manifest["capture_tool"] == "gflbench").all()
    assert (manifest["telemetry_csv"] == "").all()
    assert manifest["notes"].iloc[0] == "device=Apple M3; os=macOS 26.5.1"


def test_gflbench_note_without_sidecar(tmp_path):
    assert gflbench_note(tmp_path / "missing.csv") == ""


def test_analyze_reports_gpu_time_per_pass(tmp_path):
    result = analyze(SAMPLE, tmp_path)
    s = result.summary
    pass_cols = ["pass_gbuffer_ms", "pass_shadows_ms", "pass_ao_ms", "pass_composite_ms"]
    assert all(c in s.columns for c in pass_cols)
    # Passes are timed separately; together they make up the GPU busy time.
    per_run = result.per_run
    busy_ms = per_run["gpu_busy_pct"] / 100 * per_run["mean_frametime_ms"]
    assert per_run[pass_cols].sum(axis=1).to_numpy() == pytest.approx(busy_ms.to_numpy(), rel=0.01)
    assert any(p.name == "SYNTHETIC_gpu_time_by_pass.png" for p in result.outputs)
    assert result.is_synthetic


def test_old_manifests_still_load(tmp_path):
    manifest = pd.read_csv(ROOT / "data/sample/synthetic_lyra_scalability/manifest.csv")
    old = manifest.drop(columns=["capture_tool"]).rename(columns={"frames_csv": "presentmon_csv"})
    old.to_csv(tmp_path / "manifest.csv", index=False)
    loaded = read_manifest(tmp_path)
    assert (loaded["capture_tool"] == "presentmon").all()
    assert loaded["frames_csv"].tolist() == manifest["frames_csv"].tolist()


def test_pass_share_chart_from_a_summary_csv(tmp_path):
    from gfl import plots
    summary = pd.read_csv(ROOT / "results/metal_scalability/summary_apple_m3_2026-11-18.csv",
                          dtype={"resolution": str})
    pass_cols = [c for c in summary.columns if c.startswith("pass_")]
    out = plots.pass_share(summary, pass_cols, "3840x2160", tmp_path / "share.png")
    assert out.exists() and out.stat().st_size > 10_000
    with pytest.raises(ValueError, match="no rows"):
        plots.pass_share(summary, pass_cols, "1280x720", tmp_path / "none.png")
