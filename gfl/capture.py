"""Capture orchestration: launch, wait, record with PresentMon and nvidia-smi, close.

The sequence for every run lives in `capture_run`. Commands are executed through
a Runner, so the same sequence can be:
- executed for real on the Windows benchmark PC (SubprocessRunner),
- printed without executing anything, on any OS (DryRunRunner),
- checked step by step in tests (a fake runner).

Methodology decisions:
- The game is restarted for every run, not only for every config. Runs are then
  independent measurements, and one run's leftover state (streaming, memory
  growth) cannot leak into the next.
- PresentMon targets the game by process name and records for a fixed time
  (--timed with --terminate_after_timed), so every run has the same length.
- nvidia-smi logs device-wide VRAM and GPU utilization next to PresentMon,
  because PresentMon does not record memory use.
- The manifest is rewritten after every run, so an interrupted experiment keeps
  every run captured so far.
"""

from __future__ import annotations

import dataclasses
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from gfl.config import ExperimentConfig, RunConfig, validate_tag
from gfl.launch import build_unreal_command, command_line, kill_command
from gfl.manifest import write_manifest
from gfl.phases import pending
from gfl.platform_support import require_windows
from gfl.settings import LocalSettings


class CaptureError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Tool command lines (pure functions)
# --------------------------------------------------------------------------- #


def presentmon_command(settings: LocalSettings, process_name: str, output_csv: Path,
                       duration_s: float) -> list[str]:
    # Flags from the PresentMon 2.x console application README.
    return [
        settings.presentmon_exe,
        "--process_name", process_name,
        "--output_file", str(output_csv),
        "--timed", f"{duration_s:g}",
        "--terminate_after_timed",
    ]


def nvidia_smi_command(settings: LocalSettings, output_csv: Path) -> list[str]:
    # Same query and format that gfl.parse.load_gpu_telemetry_csv reads.
    return [
        settings.nvidia_smi_exe,
        "--query-gpu=timestamp,memory.used,utilization.gpu",
        "--format=csv,nounits",
        "-lms", str(settings.telemetry_interval_ms),
        "-f", str(output_csv),
    ]


# --------------------------------------------------------------------------- #
# Runners
# --------------------------------------------------------------------------- #


class Runner(Protocol):
    def start(self, command: list[str]) -> object: ...
    def run(self, command: list[str], timeout_s: float) -> int: ...
    def stop(self, handle: object) -> None: ...
    def wait(self, seconds: float, reason: str) -> None: ...
    def prompt(self, message: str) -> None: ...


class SubprocessRunner:
    """Executes commands for real. Windows only."""

    def __init__(self, echo: Callable[[str], None] = print) -> None:
        require_windows("Benchmark capture")
        self.echo = echo

    def start(self, command):
        # A string, not a list, so Windows passes our exact quoting through.
        self.echo(f"start: {command_line(command)}")
        return subprocess.Popen(command_line(command))

    def run(self, command, timeout_s):
        self.echo(f"run:   {command_line(command)}")
        return subprocess.run(command_line(command), timeout=timeout_s, check=False).returncode

    def stop(self, handle):
        handle.terminate()
        try:
            handle.wait(timeout=10)
        except subprocess.TimeoutExpired:
            handle.kill()

    def wait(self, seconds, reason):
        self.echo(f"wait:  {seconds:g} s ({reason})")
        time.sleep(seconds)

    def prompt(self, message):
        input(f"\n{message}\nPress Enter to continue... ")


class DryRunRunner:
    """Prints every step and executes nothing. Works on any OS."""

    def __init__(self, echo: Callable[[str], None] = print) -> None:
        self.echo = echo

    def start(self, command):
        self.echo(f"start: {command_line(command)}")
        return command

    def run(self, command, timeout_s):
        self.echo(f"run:   {command_line(command)}")
        return 0

    def stop(self, handle):
        self.echo(f"stop:  {command_line(handle)}")

    def wait(self, seconds, reason):
        self.echo(f"wait:  {seconds:g} s ({reason})")

    def prompt(self, message):
        self.echo(f"ask:   {message}")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def with_tag(experiment: ExperimentConfig, tag: str | None) -> ExperimentConfig:
    """Override the experiment's tag, e.g. "fix" for the after runs."""
    if tag is None:
        return experiment
    return dataclasses.replace(experiment, tag=validate_tag(tag))


def capture_dir_for(experiment: ExperimentConfig, settings: LocalSettings,
                    now: datetime | None = None) -> Path:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return Path(settings.capture_root) / f"{experiment.name}_{experiment.tag}_{stamp}"


def manual_instructions(run: RunConfig, process_name: str) -> str:
    fg = "on" if run.frame_generation else "off"
    return (
        f"[{run.config_id}] Start {process_name} by hand and set: resolution {run.resolution}, "
        f"DLSS {run.dlss_mode}, Frame Generation {fg}. Start the built-in benchmark, then press "
        "Enter as soon as it begins. Start FrameView with its hotkey at the same time."
    )


def capture_run(experiment: ExperimentConfig, run: RunConfig, run_index: int,
                runner: Runner, settings: LocalSettings, out_dir: Path) -> dict:
    """Capture one run and return its manifest row."""
    if run.inference_streams:
        raise pending(4, "Capture with concurrent inference streams")

    scene, cap = experiment.scene, experiment.capture
    run_id = f"{run.config_id}_run{run_index}"
    pm_csv = out_dir / f"presentmon_{run_id}.csv"
    smi_csv = out_dir / f"nvidia-smi_{run_id}.csv"

    if scene.launcher == "unreal":
        runner.start(build_unreal_command(scene, run))
        runner.wait(settings.load_wait_s, "load the map and start the camera path")
    else:
        runner.prompt(manual_instructions(run, scene.process_name))

    started_at = datetime.now()
    telemetry = runner.start(nvidia_smi_command(settings, smi_csv))
    try:
        code = runner.run(presentmon_command(settings, scene.process_name, pm_csv, cap.duration_s),
                          timeout_s=cap.duration_s + settings.presentmon_timeout_margin_s)
    finally:
        runner.stop(telemetry)
        if scene.launcher == "unreal":
            runner.run(kill_command(scene.process_name), timeout_s=30)

    if code != 0:
        raise CaptureError(f"{run_id}: PresentMon exited with code {code}")

    return {
        "run_id": run_id,
        "experiment": run.experiment,
        "config_id": run.config_id,
        "scene": run.scene,
        "preset": run.preset,
        "resolution": str(run.resolution),
        "dlss_mode": run.dlss_mode,
        "frame_generation": run.frame_generation,
        "inference_streams": run.inference_streams,
        "tag": run.tag,
        "run_index": run_index,
        "duration_s": cap.duration_s,
        "warmup_s": cap.warmup_s,
        "data_kind": "real",
        "presentmon_csv": pm_csv.name,
        "frameview_csv": "",  # attached later with: gfl validate --frameview-dir
        "telemetry_csv": smi_csv.name,
        "captured_at": started_at.isoformat(timespec="seconds"),
        "notes": "",
    }


def run_experiment(experiment: ExperimentConfig, runner: Runner, settings: LocalSettings,
                   out_dir: Path, write_files: bool = True) -> list[dict]:
    """Capture every config `capture.runs` times. Returns the manifest rows.

    With write_files=False (dry run) the manifest is not written and the
    output folder is not created.
    """
    rows: list[dict] = []
    runs = experiment.expand()
    if write_files:
        out_dir.mkdir(parents=True, exist_ok=True)
    for n, run in enumerate(runs, start=1):
        for run_index in range(1, experiment.capture.runs + 1):
            rows.append(capture_run(experiment, run, run_index, runner, settings, out_dir))
            if write_files:
                write_manifest(rows, out_dir)
            last = n == len(runs) and run_index == experiment.capture.runs
            if not last:
                runner.wait(experiment.capture.cooldown_s, "cooldown between runs")
    return rows
