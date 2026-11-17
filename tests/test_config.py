from pathlib import Path

import pytest

from gfl.config import ConfigError, Resolution, load_experiment, parse_experiment

CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"


def minimal(**overrides):
    raw = {
        "experiment": "test",
        "scene": {"name": "lyra", "launcher": "unreal", "executable": "C:/x.exe",
                  "process_name": "LyraGame.exe"},
        "matrix": {"preset": ["low", "epic"], "resolution": ["1920x1080", "3840x2160"]},
    }
    raw.update(overrides)
    return raw


@pytest.mark.parametrize("path", sorted(CONFIG_DIR.glob("*.yaml")), ids=lambda p: p.name)
def test_every_shipped_config_loads(path):
    assert load_experiment(path).expand()


def test_scalability_matrix_is_4_presets_by_3_resolutions():
    exp = load_experiment(CONFIG_DIR / "lyra_scalability.yaml")
    runs = exp.expand()
    assert len(runs) == 12
    assert {r.preset for r in runs} == {"low", "medium", "high", "epic"}
    assert {str(r.resolution) for r in runs} == {"1920x1080", "2560x1440", "3840x2160"}
    assert exp.capture.runs == 3 and exp.capture.duration_s == 60 and exp.capture.warmup_s == 10


def test_config_id_is_readable_and_unique():
    runs = parse_experiment(minimal()).expand()
    assert runs[0].config_id == "lyra_low_1920x1080_native_fg-off_s0_baseline"
    assert len({r.config_id for r in runs}) == len(runs)


def test_defaults():
    exp = parse_experiment(minimal())
    assert exp.tag == "baseline"
    assert exp.dlss_modes == ("native",) and exp.frame_generation == (False,)
    assert exp.inference_streams == (0,)


@pytest.mark.parametrize("override, message", [
    ({"matrix": {"preset": ["ultra"], "resolution": ["1920x1080"]}}, "unknown preset"),
    ({"matrix": {"preset": ["low"], "resolution": ["1080p"]}}, "resolution"),
    ({"matrix": {"preset": ["low"], "resolution": ["1920x1080"], "dlss_mode": ["ultra"]}}, "dlss_mode"),
    ({"matrix": {"preset": ["low"], "resolution": ["1920x1080"], "inference_streams": [-1]}},
     "inference_streams"),
    ({"matrix": {"preset": ["low", "low"], "resolution": ["1920x1080"]}}, "duplicate"),
    ({"capture": {"duration_s": 60, "warmup_s": 60}}, "warmup_s"),
    ({"capture": {"runs": 0}}, "runs"),
    ({"tag": "Fix Run"}, "tag"),
    ({"matrix": {"preset": ["manual"], "resolution": ["1920x1080"]}}, "manual"),
])
def test_invalid_configs_are_rejected(override, message):
    with pytest.raises(ConfigError, match=message):
        parse_experiment(minimal(**override))


def test_unreal_launcher_requires_executable():
    raw = minimal()
    del raw["scene"]["executable"]
    with pytest.raises(ConfigError, match="executable"):
        parse_experiment(raw)


def test_resolution_parsing():
    r = Resolution.parse("2560x1440")
    assert (r.width, r.height, r.pixels, str(r)) == (2560, 1440, 2560 * 1440, "2560x1440")
