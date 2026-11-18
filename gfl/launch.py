"""Build the command line that starts a packaged Unreal Engine build in a known state.

The function here only builds the argument list; it never starts anything, so
it is pure and testable on any OS. gfl.capture runs the command on Windows.

Methodology decisions:
- Scalability is set per group with sg.* console variables (the same variables
  the in-game presets write), all to the same level, so "Epic" means exactly
  the engine's Epic scalability level for every group.
- sg.ResolutionQuality 100 pins the internal render resolution to the output
  resolution (native). DLSS runs change this in Phase 3.
- VSync off and t.MaxFPS 0 remove frame caps, so frame time reflects the work
  the CPU and GPU do, not the display refresh rate.
- Console commands are passed with -ExecCmds, which runs them after startup and
  therefore after saved user settings are applied. Check on the benchmark PC
  that Lyra does not re-apply its own settings afterwards (see
  docs/windows_runbook.md, "First-time checks").
"""

from __future__ import annotations

from gfl.config import PRESET_LEVELS, RunConfig, SceneConfig
from gfl.phases import pending

# Standard UE5 scalability groups. Engine/Config/BaseScalability.ini in the
# installed engine version is the reference for what each level changes.
SCALABILITY_GROUPS = (
    "sg.ViewDistanceQuality",
    "sg.AntiAliasingQuality",
    "sg.ShadowQuality",
    "sg.GlobalIlluminationQuality",
    "sg.ReflectionQuality",
    "sg.PostProcessQuality",
    "sg.TextureQuality",
    "sg.EffectsQuality",
    "sg.FoliageQuality",
    "sg.ShadingQuality",
)

UNCAPPED_FRAME_RATE = ("r.VSync 0", "t.MaxFPS 0")


def console_commands(run: RunConfig) -> list[str]:
    """Console commands that put the engine into this run's configuration."""
    if run.dlss_mode != "native" or run.frame_generation:
        raise pending(3, "DLSS and Frame Generation launch settings")
    level = PRESET_LEVELS[run.preset]
    commands = [f"{group} {level}" for group in SCALABILITY_GROUPS]
    commands.append("sg.ResolutionQuality 100")
    commands.extend(UNCAPPED_FRAME_RATE)
    return commands


def build_unreal_command(scene: SceneConfig, run: RunConfig) -> list[str]:
    """Argument list for launching the packaged game for one run."""
    if scene.launcher != "unreal":
        raise ValueError(f"scene {scene.name!r} is launched by hand (launcher: {scene.launcher})")
    command = [scene.executable]
    if scene.map:
        command.append(scene.map)
    command += [
        f"-ResX={run.resolution.width}",
        f"-ResY={run.resolution.height}",
        "-fullscreen",
        # -ExecCmds takes one comma-separated string of console commands.
        "-ExecCmds=" + ", ".join(console_commands(run)),
    ]
    command.extend(scene.extra_args)
    return command


def build_gflbench_command(scene: SceneConfig, run: RunConfig, output_csv, duration_s: float) -> list[str]:
    """Argument list for one run of the Metal benchmark renderer (bench/metal)."""
    if scene.launcher != "gflbench":
        raise ValueError(f"scene {scene.name!r} is not the gflbench renderer")
    return [
        scene.executable,
        "--preset", run.preset,
        "--resolution", str(run.resolution),
        "--duration", f"{duration_s:g}",
        "--output", str(output_csv),
        *scene.extra_args,
    ]


def command_line(args: list[str]) -> str:
    """Join arguments into one Windows command line, quoted the way Unreal parses it.

    Unreal reads its raw command line itself: for -Key=value it expects the
    quotes around the value (-ExecCmds="a, b"), not around the whole argument.
    Python's default Windows quoting would produce "-ExecCmds=a, b", and
    Unreal would read the value only up to the first space. Other arguments with spaces (paths) are quoted
    whole, which is what PresentMon and nvidia-smi expect.
    """
    parts = []
    for arg in map(str, args):
        if " " not in arg:
            parts.append(arg)
        elif arg.startswith("-") and "=" in arg:
            key, value = arg.split("=", 1)
            parts.append(f'{key}="{value}"')
        else:
            parts.append(f'"{arg}"')
    return " ".join(parts)


def kill_command(process_name: str) -> list[str]:
    """Close the game by image name.

    A packaged UE game's top-level .exe can start the real game binary as a
    child process, so closing the process we started is not enough. taskkill
    by image name closes whichever process is presenting frames.
    """
    return ["taskkill", "/IM", process_name, "/F"]
