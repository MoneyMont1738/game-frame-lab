"""Run manifest: the index that ties capture files to the config they came from.

Every capture directory has a `manifest.csv` with one row per run. `gfl run`
appends rows as it captures (Phase 2); the synthetic generator writes the same
format. Analysis reads the manifest instead of guessing from file names, so a
run's preset, resolution, warmup, and data kind are always explicit.

`data_kind` is "synthetic" or "real". It travels into every summary table and
chart, and `gfl hypothesis-check` refuses to run when any row is synthetic.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

MANIFEST_NAME = "manifest.csv"

DATA_KINDS = ("synthetic", "real")

# presentmon: PresentMon CSV (Windows).
# gflbench:   the Metal benchmark renderer in bench/metal (macOS). It writes the
#             same column names as PresentMon plus per-pass GPU times.
CAPTURE_TOOLS = ("presentmon", "gflbench")

MANIFEST_COLUMNS = [
    "run_id",            # unique within the directory, e.g. <config_id>_run1
    "experiment",
    "config_id",
    "scene",
    "preset",
    "resolution",        # "2560x1440"
    "dlss_mode",
    "frame_generation",  # True / False
    "inference_streams",
    "tag",               # "baseline" or "fix" (or any experiment-specific tag)
    "run_index",         # 1-based
    "duration_s",
    "warmup_s",
    "data_kind",         # "synthetic" or "real"
    "capture_tool",      # which tool wrote frames_csv: see CAPTURE_TOOLS
    "frames_csv",        # per-frame log; paths are relative to the manifest's directory
    "frameview_csv",     # optional (empty if FrameView was not recorded)
    "telemetry_csv",     # optional nvidia-smi log (VRAM, GPU utilization)
    "captured_at",       # ISO 8601 timestamp
    "notes",
]

_OPTIONAL_PATH_COLUMNS = ("frameview_csv", "telemetry_csv")


class ManifestError(ValueError):
    pass


def read_manifest(run_dir: str | Path) -> pd.DataFrame:
    """Load and validate `<run_dir>/manifest.csv`."""
    run_dir = Path(run_dir)
    path = run_dir / MANIFEST_NAME
    if not path.exists():
        raise ManifestError(
            f"no {MANIFEST_NAME} in {run_dir}. Point gfl at a capture directory "
            "(for example data/sample/synthetic_lyra_scalability)."
        )
    df = pd.read_csv(path, dtype={"resolution": str, "tag": str, "notes": str})
    # Manifests written before the macOS capture path had one PresentMon column.
    if "presentmon_csv" in df.columns and "frames_csv" not in df.columns:
        df = df.rename(columns={"presentmon_csv": "frames_csv"})
        df["capture_tool"] = "presentmon"
    missing = [c for c in MANIFEST_COLUMNS if c not in df.columns]
    if missing:
        raise ManifestError(f"{path} is missing columns: {missing}")
    if df.empty:
        raise ManifestError(f"{path} has no runs")
    if df["run_id"].duplicated().any():
        dupes = df.loc[df["run_id"].duplicated(), "run_id"].tolist()
        raise ManifestError(f"{path} has duplicate run_id values: {dupes}")
    bad_kind = set(df["data_kind"]) - set(DATA_KINDS)
    if bad_kind:
        raise ManifestError(f"{path}: data_kind must be one of {DATA_KINDS}, got {bad_kind}")
    bad_tool = set(df["capture_tool"]) - set(CAPTURE_TOOLS)
    if bad_tool:
        raise ManifestError(f"{path}: capture_tool must be one of {CAPTURE_TOOLS}, got {bad_tool}")

    for col in _OPTIONAL_PATH_COLUMNS + ("notes",):
        df[col] = df[col].fillna("").astype(str)

    # A file whose name says "synthetic" must never be labeled as real data.
    for _, row in df.iterrows():
        named_synthetic = "synthetic" in str(row["frames_csv"]).lower()
        if named_synthetic and row["data_kind"] != "synthetic":
            raise ManifestError(
                f"{path}: run {row['run_id']} points at a synthetic file but is "
                "labeled data_kind=real"
            )
    return df


def resolve(run_dir: str | Path, relative: str) -> Path | None:
    """Turn a manifest path cell into an absolute path (None if empty)."""
    if not relative:
        return None
    return Path(run_dir) / relative


def data_kind_of(manifest: pd.DataFrame) -> str:
    """'synthetic' if any run is synthetic, otherwise 'real'.

    Mixing the two in one analysis is treated as synthetic so that a single
    synthetic run can never leak into results presented as measurements.
    """
    return "synthetic" if (manifest["data_kind"] == "synthetic").any() else "real"


def write_manifest(rows: list[dict], run_dir: str | Path) -> Path:
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    missing = [c for c in MANIFEST_COLUMNS if c not in df.columns]
    if missing:
        raise ManifestError(f"manifest rows are missing columns: {missing}")
    path = run_dir / MANIFEST_NAME
    df[MANIFEST_COLUMNS].to_csv(path, index=False)
    return path
