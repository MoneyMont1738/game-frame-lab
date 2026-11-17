"""End-to-end: synthetic sample data -> analyze, plus the data-kind and platform guards."""

import importlib
import sys
from pathlib import Path

import pandas as pd
import pytest

from gfl.analyze import analyze
from gfl.hypothesis_check import SyntheticDataError, ensure_real_data
from gfl.manifest import ManifestError, read_manifest
from gfl.platform_support import UnsupportedPlatformError, require_windows

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "sample" / "synthetic_lyra_scalability"


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    return analyze(SAMPLE, tmp_path_factory.mktemp("out"))


def test_analyze_sample_produces_one_row_per_config(result):
    assert len(result.summary) == 12
    assert len(result.per_run) == 36
    assert (result.summary["runs"] == 3).all()


def test_sample_output_is_labeled_synthetic(result):
    assert result.is_synthetic
    assert (result.summary["data_kind"] == "synthetic").all()
    for path in result.outputs:
        assert path.exists()
        assert path.name.startswith("SYNTHETIC_")
    summary_md = next(p for p in result.outputs if p.suffix == ".md").read_text()
    assert "SYNTHETIC DATA" in summary_md


def test_written_summary_matches_returned_summary(result):
    on_disk = pd.read_csv(next(p for p in result.outputs if p.name == "SYNTHETIC_summary.csv"))
    assert on_disk["avg_fps"].round(3).tolist() == result.summary["avg_fps"].round(3).tolist()


def test_summary_metrics_are_internally_consistent(result):
    s = result.summary
    assert (s["low_1pct_fps"] <= s["avg_fps"]).all()
    assert (s["low_01pct_fps"] <= s["low_1pct_fps"]).all()
    assert (1000 / s["p99_frametime_ms"] <= s["avg_fps"]).all()
    assert ((s["gpu_busy_pct"] > 0) & (s["gpu_busy_pct"] <= 100)).all()


def test_hypothesis_check_refuses_synthetic_data():
    with pytest.raises(SyntheticDataError):
        ensure_real_data([SAMPLE])


def test_manifest_rejects_synthetic_file_labeled_real(tmp_path):
    manifest = pd.read_csv(SAMPLE / "manifest.csv")
    manifest["data_kind"] = "real"
    manifest.to_csv(tmp_path / "manifest.csv", index=False)
    with pytest.raises(ManifestError, match="synthetic"):
        read_manifest(tmp_path)


def test_windows_only_modules_import_everywhere():
    for name in ["gfl.capture", "gfl.launch", "inference.load_generator"]:
        importlib.import_module(name)


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows message")
def test_windows_only_features_fail_clearly_off_windows():
    with pytest.raises(UnsupportedPlatformError, match="only runs on Windows"):
        require_windows("PresentMon capture")
    from gfl.capture import SubprocessRunner
    with pytest.raises(UnsupportedPlatformError):
        SubprocessRunner()
