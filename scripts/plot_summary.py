"""Draw the report charts from a summary CSV.

Usage:
    python scripts/plot_summary.py SUMMARY_CSV OUT_DIR [--prefix NAME_]

The CSV needs: preset, resolution, avg_fps, low_1pct_fps, and pass_<name>_ms
columns (the summary.csv that `gfl analyze` writes has all of them).
Charts are drawn with the same functions `gfl analyze` uses.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from gfl import plots


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("summary_csv", type=Path)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--prefix", default="")
    args = parser.parse_args()

    summary = pd.read_csv(args.summary_csv, dtype={"resolution": str})
    pass_cols = [c for c in summary.columns if c.startswith("pass_") and c.endswith("_ms")]
    out, pre = args.out_dir, args.prefix
    written = [
        plots.fps_by_config(summary, out / f"{pre}fps_by_preset_and_resolution.png"),
        plots.gpu_time_by_pass(summary, pass_cols, out / f"{pre}gpu_time_by_pass.png"),
    ]
    for resolution in dict.fromkeys(summary["resolution"]):
        written.append(plots.pass_share(summary, pass_cols, resolution,
                                        out / f"{pre}pass_share_{resolution}.png"))
    for path in written:
        print(path)


if __name__ == "__main__":
    main()
