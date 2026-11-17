"""Capture orchestration, checked with a fake runner (no Windows, no game)."""

from pathlib import Path

import pytest

from gfl.analyze import analyze
from gfl.capture import (CaptureError, DryRunRunner, nvidia_smi_command, presentmon_command,
                         run_experiment, with_tag)
from gfl.config import ConfigError, parse_experiment
from gfl.manifest import read_manifest
from gfl.settings import LocalSettings, load_settings
from gfl.synthetic import simulate_frames, to_nvidia_smi, to_presentmon

SETTINGS = LocalSettings(presentmon_exe="PresentMon.exe", load_wait_s=20)


def experiment(launcher="unreal", runs=2, presets=("epic",), streams=(0,)):
    scene = {"name": "lyra", "launcher": launcher, "process_name": "LyraGame.exe"}
    if launcher == "unreal":
        scene["executable"] = "C:/Lyra/LyraGame.exe"
    return parse_experiment({
        "experiment": "test_exp",
        "scene": scene,
        "capture": {"runs": runs, "duration_s": 60, "warmup_s": 10, "cooldown_s": 15},
        "matrix": {"preset": list(presets), "resolution": ["2560x1440"],
                   "inference_streams": list(streams)},
    })


class FakeRunner:
    """Records every step. When PresentMon or nvidia-smi is called, writes the
    file it would have written (synthetic content), like the real tools."""

    def __init__(self, fail_presentmon_on_call: int | None = None):
        self.steps: list[tuple] = []
        self.presentmon_calls = 0
        self.fail_on = fail_presentmon_on_call

    def start(self, command):
        self.steps.append(("start", command[0]))
        if "nvidia-smi" in command[0]:
            path = Path(command[command.index("-f") + 1])
            run = experiment().expand()[0]
            from datetime import datetime
            path.write_text(to_nvidia_smi(run, 1, 60, 10, datetime(2026, 1, 1)))
        return command

    def run(self, command, timeout_s):
        self.steps.append(("run", command[0]))
        if command[0] == "PresentMon.exe":
            self.presentmon_calls += 1
            if self.presentmon_calls == self.fail_on:
                return 1
            path = Path(command[command.index("--output_file") + 1])
            sim = simulate_frames(experiment().expand()[0], self.presentmon_calls, 60, 10)
            to_presentmon(sim, "LyraGame.exe").to_csv(path, index=False)
        return 0

    def stop(self, handle):
        self.steps.append(("stop", handle[0]))

    def wait(self, seconds, reason):
        self.steps.append(("wait", seconds))

    def prompt(self, message):
        self.steps.append(("prompt", message))


def test_tool_commands():
    pm = presentmon_command(SETTINGS, "LyraGame.exe", Path("out/pm.csv"), 60)
    assert pm == ["PresentMon.exe", "--process_name", "LyraGame.exe", "--output_file",
                  str(Path("out/pm.csv")), "--timed", "60", "--terminate_after_timed"]
    smi = nvidia_smi_command(SETTINGS, Path("out/smi.csv"))
    assert "--query-gpu=timestamp,memory.used,utilization.gpu" in smi
    assert "--format=csv,nounits" in smi and smi[smi.index("-lms") + 1] == "500"


def test_run_sequence_for_one_unreal_run(tmp_path):
    runner = FakeRunner()
    run_experiment(experiment(runs=1), runner, SETTINGS, tmp_path)
    assert runner.steps == [
        ("start", "C:/Lyra/LyraGame.exe"),    # launch the game
        ("wait", 20),                          # scene load
        ("start", "nvidia-smi"),               # telemetry on
        ("run", "PresentMon.exe"),             # timed capture
        ("stop", "nvidia-smi"),                # telemetry off
        ("run", "taskkill"),                   # close the game
    ]                                          # no cooldown after the last run


def test_cooldown_between_runs_only(tmp_path):
    runner = FakeRunner()
    run_experiment(experiment(runs=2, presets=("high", "epic")), runner, SETTINGS, tmp_path)
    waits = [s for s in runner.steps if s == ("wait", 15)]
    assert len(waits) == 3  # 4 runs, 3 gaps


def test_manifest_is_written_and_analyzable(tmp_path):
    rows = run_experiment(experiment(runs=2), FakeRunner(), SETTINGS, tmp_path / "cap")
    manifest = read_manifest(tmp_path / "cap")
    assert len(rows) == len(manifest) == 2
    assert (manifest["data_kind"] == "real").all()
    assert manifest["run_id"].tolist() == [
        "lyra_epic_2560x1440_native_fg-off_s0_baseline_run1",
        "lyra_epic_2560x1440_native_fg-off_s0_baseline_run2",
    ]
    result = analyze(tmp_path / "cap", tmp_path / "out")
    assert len(result.summary) == 1 and result.summary["runs"].iloc[0] == 2
    assert not any(p.name.startswith("SYNTHETIC_") for p in result.outputs)


def test_failed_capture_keeps_earlier_runs_and_stops_telemetry(tmp_path):
    runner = FakeRunner(fail_presentmon_on_call=2)
    with pytest.raises(CaptureError, match="run2"):
        run_experiment(experiment(runs=3), runner, SETTINGS, tmp_path)
    assert len(read_manifest(tmp_path)) == 1
    assert runner.steps[-2:] == [("stop", "nvidia-smi"), ("run", "taskkill")]


def test_manual_launcher_prompts_instead_of_launching(tmp_path):
    runner = FakeRunner()
    run_experiment(experiment(launcher="manual", runs=1), runner, SETTINGS, tmp_path)
    kinds = [s[0] for s in runner.steps]
    assert kinds == ["prompt", "start", "run", "stop"]  # no launch, no taskkill
    assert "2560x1440" in runner.steps[0][1]


def test_dry_run_executes_nothing_and_writes_nothing(tmp_path):
    lines = []
    out = tmp_path / "never_created"
    rows = run_experiment(experiment(runs=2), DryRunRunner(lines.append), SETTINGS, out,
                          write_files=False)
    assert len(rows) == 2
    assert not out.exists()
    assert any(line.startswith("run:   PresentMon.exe") for line in lines)


def test_tag_override():
    assert with_tag(experiment(), "fix").expand()[0].config_id.endswith("_fix")
    with pytest.raises(ConfigError):
        with_tag(experiment(), "Fix Run")


def test_inference_streams_wait_for_phase_4(tmp_path):
    with pytest.raises(NotImplementedError, match="Phase 4"):
        run_experiment(experiment(streams=(1,)), FakeRunner(), SETTINGS, tmp_path)


def test_settings_defaults_and_unknown_keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no gfl.local.yaml here
    assert load_settings(None) == LocalSettings()
    path = tmp_path / "s.yaml"
    path.write_text("presentmon_exe: C:/pm.exe\nload_wait_s: 30\n")
    assert load_settings(path).load_wait_s == 30
    path.write_text("presentmon_path: C:/pm.exe\n")
    with pytest.raises(ConfigError, match="unknown settings"):
        load_settings(path)
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "missing.yaml")
