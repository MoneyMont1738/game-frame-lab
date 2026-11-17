import pytest

from gfl.config import Resolution, RunConfig, SceneConfig
from gfl.launch import (SCALABILITY_GROUPS, build_unreal_command, command_line, console_commands,
                        kill_command)
from gfl.phases import NotYetImplemented

SCENE = SceneConfig(name="lyra", launcher="unreal", process_name="LyraGame.exe",
                    executable="C:/Games/Lyra Build/LyraGame.exe", map="/ShooterMaps/Maps/L_Expanse",
                    extra_args=("-nosplash",))


def run_config(preset="epic", res="2560x1440", dlss="native", fg=False):
    return RunConfig("exp", "lyra", preset, Resolution.parse(res), dlss, fg, 0, "baseline")


@pytest.mark.parametrize("preset, level", [("low", 0), ("medium", 1), ("high", 2), ("epic", 3)])
def test_every_scalability_group_is_set_to_the_preset_level(preset, level):
    commands = console_commands(run_config(preset))
    for group in SCALABILITY_GROUPS:
        assert f"{group} {level}" in commands


def test_native_runs_pin_resolution_and_uncap_frame_rate():
    commands = console_commands(run_config())
    assert "sg.ResolutionQuality 100" in commands
    assert "r.VSync 0" in commands and "t.MaxFPS 0" in commands


def test_unreal_command_layout():
    cmd = build_unreal_command(SCENE, run_config(res="3840x2160"))
    assert cmd[0] == SCENE.executable
    assert cmd[1] == SCENE.map
    assert "-ResX=3840" in cmd and "-ResY=2160" in cmd and "-fullscreen" in cmd
    assert cmd[-1] == "-nosplash"
    exec_cmds = next(a for a in cmd if a.startswith("-ExecCmds="))
    assert exec_cmds.count(",") == len(console_commands(run_config())) - 1


def test_command_line_quotes_the_way_unreal_parses():
    line = command_line(build_unreal_command(SCENE, run_config()))
    assert line.startswith('"C:/Games/Lyra Build/LyraGame.exe" /ShooterMaps/Maps/L_Expanse ')
    assert '-ExecCmds="sg.ViewDistanceQuality 3, ' in line
    assert '"-ExecCmds=' not in line


def test_dlss_and_frame_generation_wait_for_phase_3():
    with pytest.raises(NotYetImplemented, match="Phase 3"):
        console_commands(run_config(dlss="quality"))
    with pytest.raises(NotYetImplemented, match="Phase 3"):
        console_commands(run_config(fg=True))


def test_manual_scene_has_no_launch_command():
    manual = SceneConfig(name="cyberpunk", launcher="manual", process_name="Cyberpunk2077.exe")
    with pytest.raises(ValueError, match="by hand"):
        build_unreal_command(manual, run_config())


def test_kill_command_targets_the_image_name():
    assert kill_command("LyraGame.exe") == ["taskkill", "/IM", "LyraGame.exe", "/F"]
