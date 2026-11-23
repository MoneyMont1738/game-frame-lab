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
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from gfl.config import ConfigError, ExperimentConfig, RunConfig, validate_tag
from gfl.launch import build_gflbench_command, build_unreal_command, command_line, kill_command
from gfl.manifest import write_manifest
from gfl.phases import pending
from gfl.platform_support import is_windows, require_macos, require_windows
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


def require_capture_platform(experiment: ExperimentConfig) -> None:
    """Fail early, with a clear message, if this OS cannot run the experiment."""
    if experiment.scene.launcher == "gflbench":
        require_macos("The Metal benchmark (gflbench)")
    else:
        require_windows("Benchmark capture with PresentMon")


def mac_power_warnings(read: Callable[[list[str]], str] | None = None) -> list[str]:
    """Reasons this Mac may run the GPU slower than usual, from `pmset`.

    On a MacBook, Low Power Mode and running on battery lower clock speeds,
    which slows every pass by the same factor and makes runs disagree. A
    capture taken that way cannot be compared with one taken plugged in.
    `read` runs a command and returns its output (replaced in tests).
    """
    if read is None:
        def read(command):
            try:
                return subprocess.run(command, capture_output=True, text=True, timeout=10).stdout
            except (OSError, subprocess.SubprocessError):
                return ""
    warnings = []
    if "Battery Power" in read(["pmset", "-g", "batt"]):
        warnings.append("the Mac is running on battery; plug it in")
    for line in read(["pmset", "-g"]).splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].lower() == "lowpowermode" and parts[1] == "1":
            warnings.append("Low Power Mode is on; turn it off in System Settings, Battery")
    return warnings


class SubprocessRunner:
    """Executes commands for real (Windows for PresentMon, macOS for gflbench)."""

    def __init__(self, echo: Callable[[str], None] = print) -> None:
        self.echo = echo

    @staticmethod
    def _native(command):
        # On Windows, pass one string so our Unreal-style quoting is kept as
        # is. Elsewhere, pass the list (no shell involved).
        return command_line(command) if is_windows() else [str(c) for c in command]

    def start(self, command):
        self.echo(f"start: {command_line(command)}")
        return subprocess.Popen(self._native(command))

    def run(self, command, timeout_s):
        self.echo(f"run:   {command_line(command)}")
        return subprocess.run(self._native(command), timeout=timeout_s, check=False).returncode

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
    if experiment.variants:
        raise ConfigError("this experiment defines variants; their names are the tags, so --tag does not apply")
    return dataclasses.replace(experiment, tag=validate_tag(tag))


def capture_dir_for(experiment: ExperimentConfig, settings: LocalSettings,
                    now: datetime | None = None) -> Path:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    label = "-vs-".join(name for name, _ in experiment.variants) if experiment.variants else experiment.tag
    return Path(settings.capture_root) / f"{experiment.name}_{label}_{stamp}"


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

    run_id = f"{run.config_id}_run{run_index}"
    if experiment.scene.launcher == "gflbench":
        files = _capture_gflbench(experiment, run, run_id, runner, settings, out_dir)
    else:
        files = _capture_presentmon(experiment, run, run_id, runner, settings, out_dir)

    cap = experiment.capture
    notes = [files.pop("notes", "")]
    if run.extra_args:
        notes.append("args=" + " ".join(run.extra_args))
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
        "frameview_csv": "",  # attached later with: gfl validate --frameview-dir
        "notes": "; ".join(n for n in notes if n),
        **files,
    }


def _capture_presentmon(experiment, run, run_id, runner, settings, out_dir) -> dict:
    """Windows: launch (or prompt), then PresentMon plus the nvidia-smi logger."""
    scene, cap = experiment.scene, experiment.capture
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
    return {"capture_tool": "presentmon", "frames_csv": pm_csv.name,
            "telemetry_csv": smi_csv.name, "captured_at": started_at.isoformat(timespec="seconds")}


def _capture_gflbench(experiment, run, run_id, runner, settings, out_dir) -> dict:
    """macOS: the benchmark renders for the run's duration and writes its own log.

    It times its passes on the GPU and records Metal memory itself, so there is
    no separate capture tool or telemetry logger. It needs no load wait: shaders
    compile before the timed loop starts, and the warmup trim still applies.
    """
    cap = experiment.capture
    frames_csv = out_dir / f"gflbench_{run_id}.csv"
    started_at = datetime.now()
    code = runner.run(build_gflbench_command(experiment.scene, run, frames_csv, cap.duration_s),
                      timeout_s=cap.duration_s + settings.presentmon_timeout_margin_s)
    if code != 0:
        raise CaptureError(f"{run_id}: gflbench exited with code {code}")
    return {"capture_tool": "gflbench", "frames_csv": frames_csv.name,
            "telemetry_csv": "", "captured_at": started_at.isoformat(timespec="seconds"),
            "notes": gflbench_note(frames_csv)}


def gflbench_note(frames_csv: Path) -> str:
    """Device and OS from the JSON file gflbench writes next to its log."""
    sidecar = frames_csv.with_suffix(".json")
    if not sidecar.exists():
        return ""
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return f"device={meta.get('device', '?')}; os={meta.get('os', '?')}"


def run_experiment(experiment: ExperimentConfig, runner: Runner, settings: LocalSettings,
                   out_dir: Path, write_files: bool = True) -> list[dict]:
    """Capture every config `capture.runs` times. Returns the manifest rows.

    With write_files=False (dry run) the manifest is not written and the
    output folder is not created.
    """
    rows: list[dict] = []
    order = experiment.capture_order()
    if write_files:
        out_dir.mkdir(parents=True, exist_ok=True)
    for n, (run, run_index) in enumerate(order, start=1):
        rows.append(capture_run(experiment, run, run_index, runner, settings, out_dir))
        if write_files:
            write_manifest(rows, out_dir)
        if n < len(order):
            runner.wait(experiment.capture.cooldown_s, "cooldown between runs")
    return rows
