"""Charts (matplotlib, static PNG).

Style choices: one y-axis per chart, thin lines, recessive grid, a fixed color
per preset (the same preset is always the same color in every chart), and a
legend whenever there is more than one series. Synthetic data gets a banner and
a diagonal watermark so a chart can never be mistaken for a measurement, even
when cropped.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # file output only; works on headless machines

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from gfl.config import PRESET_LEVELS  # noqa: E402

SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e4e3de"
BANNER = "#b42318"

# Fixed categorical order, validated for color-vision-deficiency separation
# between adjacent series. Presets always map to the same slot.
PRESET_COLORS = {"low": "#2a78d6", "medium": "#eb6834", "high": "#1baf7a", "epic": "#eda100",
                 "manual": "#6250d6"}

# Frame-time ticks that correspond to familiar frame rates.
_FRAMETIME_TICKS = [(4.17, "240"), (8.33, "120"), (16.7, "60"), (33.3, "30"), (66.7, "15"),
                    (133.3, "7.5")]


def _style(ax: plt.Axes) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=TEXT_SECONDARY, labelsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _figure(nrows: int = 1, height: float = 4.5, **kwargs):
    fig, axes = plt.subplots(nrows, 1, figsize=(10, height), facecolor=SURFACE, **kwargs)
    return fig, np.atleast_1d(axes)


def _finish(fig: plt.Figure, title: str, path: Path, banner: str | None) -> Path:
    # Fixed vertical stack in inches (banner, title, legend row, plot, x label),
    # so the layout holds for any figure height.
    h = fig.get_figheight()
    banner_in = 0.32 if banner else 0.0
    title_top_in = 0.1 + banner_in
    plot_top_in = title_top_in + 0.42 + 0.62  # title, then a legend row (with title)
    fig.suptitle(title, x=0.01, y=1 - title_top_in / h, ha="left", va="top", fontsize=13,
                 color=TEXT, weight="bold")
    if banner:
        fig.text(0.01, 1 - 0.08 / h, banner, ha="left", va="top", fontsize=8.5, color=BANNER,
                 weight="bold")
        fig.text(0.5, 0.45, "SYNTHETIC", ha="center", va="center", rotation=25,
                 fontsize=80, color=BANNER, alpha=0.07, weight="bold")
    fig.subplots_adjust(left=0.15, right=0.98, bottom=0.62 / h, top=1 - plot_top_in / h)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return path


def frametime_trace(traces: dict[str, pd.DataFrame], title: str, path: Path,
                    banner: str | None = None) -> Path:
    """Frame time vs. time, one line per preset.

    Log scale on y so a 4 ms preset and a 30 ms preset are both readable and
    spikes are shown in proportion (a doubling looks the same at any level).
    Ticks are labeled with the matching FPS.
    """
    fig, (ax,) = _figure(height=5.0)
    _style(ax)
    ordered = sorted(traces.items(), key=lambda kv: PRESET_LEVELS.get(kv[0], 99))
    for preset, frames in ordered:
        ax.plot(frames["time_s"], frames["frametime_ms"], color=PRESET_COLORS[preset],
                linewidth=0.8, label=preset.capitalize())

    ax.set_yscale("log")
    lo = min(f["frametime_ms"].min() for f in traces.values())
    hi = max(f["frametime_ms"].max() for f in traces.values())
    ticks = [(v, fps) for v, fps in _FRAMETIME_TICKS if lo * 0.8 <= v <= hi * 1.2]
    ax.set_yticks([v for v, _ in ticks], [f"{v:g} ms ({fps} FPS)" for v, fps in ticks])
    ax.minorticks_off()
    ax.set_xlabel("Time since capture start (s); warmup already removed", color=TEXT_SECONDARY)
    ax.set_ylabel("Frame time (log scale)", color=TEXT_SECONDARY)
    ax.legend(title="Preset", frameon=False, ncols=len(traces), loc="lower left",
              bbox_to_anchor=(0, 1.01), fontsize=9, title_fontsize=9, alignment="left")
    return _finish(fig, title, path, banner)


def fps_by_config(summary: pd.DataFrame, path: Path, banner: str | None = None) -> Path:
    """Average FPS (bars) and 1% low (markers) for each preset, grouped by resolution."""
    resolutions = list(dict.fromkeys(summary["resolution"]))
    presets = sorted(summary["preset"].unique(), key=lambda p: PRESET_LEVELS.get(p, 99))

    fig, (ax,) = _figure(height=4.8)
    _style(ax)
    group_width = 0.8
    bar_width = group_width / len(presets)
    x = np.arange(len(resolutions))
    for i, preset in enumerate(presets):
        rows = summary[summary["preset"] == preset].set_index("resolution").reindex(resolutions)
        offsets = x - group_width / 2 + bar_width * (i + 0.5)
        ax.bar(offsets, rows["avg_fps"], width=bar_width * 0.92, color=PRESET_COLORS[preset],
               label=f"{preset.capitalize()} avg FPS", edgecolor=SURFACE, linewidth=1)
        ax.scatter(offsets, rows["low_1pct_fps"], marker="_", s=180, linewidths=2.2,
                   color=TEXT, zorder=3, label="1% low" if i == 0 else None)

    ax.set_xticks(x, resolutions)
    ax.set_ylabel("Frames per second", color=TEXT_SECONDARY)
    ax.set_ylim(0, summary["avg_fps"].max() * 1.12)
    ax.legend(frameon=False, ncols=len(presets) + 1, loc="lower left",
              bbox_to_anchor=(0, 1.01), fontsize=9)
    return _finish(fig, "Average FPS and 1% low by preset and resolution", path, banner)


# Render passes get their own fixed colors (categorical slots 5 to 8), so they
# never look like presets, which use slots 1 to 4.
PASS_COLORS = ["#e87ba4", "#008300", "#6250d6", "#e34948"]


def gpu_time_by_pass(summary: pd.DataFrame, pass_cols: list[str], path: Path,
                     banner: str | None = None) -> Path:
    """Mean GPU ms per frame, split by render pass, one bar per config.

    This is the "where does the GPU time go" view: the analog of a GPU trace's
    per-pass breakdown, from timestamps the renderer records itself.
    """
    rows = summary.iloc[::-1]  # first config at the top
    labels = [f"{p.capitalize()} {r}" for p, r in zip(rows["preset"], rows["resolution"])]
    fig, (ax,) = _figure(height=max(4.0, 0.45 * len(rows) + 2.6))
    _style(ax)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    left = np.zeros(len(rows))
    for col, color in zip(pass_cols, PASS_COLORS):
        values = rows[col].to_numpy()
        name = col.removeprefix("pass_").removesuffix("_ms")
        ax.barh(labels, values, left=left, color=color, height=0.62, label=name,
                edgecolor=SURFACE, linewidth=1)
        left += values
    for y, total in enumerate(left):
        ax.text(total, y, f"  {total:.1f} ms", va="center", fontsize=8.5, color=TEXT_SECONDARY)
    ax.set_xlim(0, left.max() * 1.15)
    ax.set_xlabel("Mean GPU time per frame (ms)", color=TEXT_SECONDARY)
    ax.legend(title="Render pass", frameon=False, ncols=len(pass_cols), loc="lower left",
              bbox_to_anchor=(0, 1.01), fontsize=9, title_fontsize=9, alignment="left")
    return _finish(fig, "Where the GPU time goes, by render pass", path, banner)
