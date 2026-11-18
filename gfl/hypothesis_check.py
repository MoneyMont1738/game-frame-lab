"""Check the study's hypotheses against measured data. Implemented in Phase 6.

The synthetic-data guard is in place now: hypotheses can only ever be judged
against real captures, so this refuses to run if any input run is synthetic.
"""

from __future__ import annotations

from pathlib import Path

from gfl.manifest import read_manifest
from gfl.phases import pending


class SyntheticDataError(RuntimeError):
    pass


def ensure_real_data(run_dirs: list[Path]) -> None:
    for run_dir in run_dirs:
        manifest = read_manifest(run_dir)
        synthetic = manifest[manifest["data_kind"] != "real"]
        named = manifest[manifest["frames_csv"].str.lower().str.contains("synthetic")]
        if not synthetic.empty or not named.empty:
            raise SyntheticDataError(
                f"{run_dir} contains synthetic runs. Hypotheses are only checked against "
                "real captures from the benchmark PC; synthetic data cannot confirm or "
                "reject anything."
            )


def check_hypotheses(run_dirs: list[Path]) -> None:
    ensure_real_data(run_dirs)
    raise pending(6, "gfl hypothesis-check")
