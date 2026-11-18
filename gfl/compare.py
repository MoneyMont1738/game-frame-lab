"""Before/after comparison of two tagged variants (for example "baseline" vs "fix").

The headline is the change in mean frame time, in milliseconds. Frame time is
used rather than FPS because it adds up: a pass that gets 5 ms cheaper makes
the frame 5 ms cheaper, at any frame rate. FPS changes are shown too.

Uncertainty: each run is one measurement. The difference between the two
variants' mean per-run values gets a 95% confidence interval from Welch's t-test
(which does not assume both variants vary by the same amount). With 5 runs per
variant, a change is called real only when its interval excludes zero.

DLSS-mode and inference-stream comparisons (Phases 3 and 4) build on this later.
"""

from __future__ import annotations

import math

import pandas as pd

# Two-sided 95% critical values of Student's t by degrees of freedom. Written
# out so the toolkit needs no statistics library; beyond 30 the normal value is
# close enough.
_T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306,
         9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
         16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086, 22: 2.074, 24: 2.064,
         26: 2.056, 28: 2.048, 30: 2.042}

# Metrics compared, in report order. Pass columns are added when present.
_METRICS = [
    ("mean_frametime_ms", "Mean frame time (ms)"),
    ("avg_fps", "Average FPS"),
    ("low_1pct_fps", "1% low FPS"),
    ("p99_frametime_ms", "p99 frame time (ms)"),
]

_MATCH_KEYS = ["scene", "preset", "resolution", "dlss_mode", "frame_generation", "inference_streams"]


def t_critical(df: float) -> float:
    """95% two-sided t value; uses the next lower tabulated df (conservative)."""
    if df >= 30:
        return 1.96 if df > 120 else _T975[30]
    key = max(k for k in _T975 if k <= max(1, math.floor(df)))
    return _T975[key]


def welch_interval(a: pd.Series, b: pd.Series) -> tuple[float, float, float]:
    """Difference of means (b - a) and its 95% confidence interval."""
    a, b = a.dropna(), b.dropna()
    if len(a) < 2 or len(b) < 2:
        raise ValueError("each variant needs at least 2 runs to estimate uncertainty")
    diff = b.mean() - a.mean()
    va, vb = a.var(ddof=1) / len(a), b.var(ddof=1) / len(b)
    se = math.sqrt(va + vb)
    if se == 0:
        return diff, diff, diff
    df = (va + vb) ** 2 / ((va ** 2) / (len(a) - 1) + (vb ** 2) / (len(b) - 1))
    half = t_critical(df) * se
    return diff, diff - half, diff + half


def compare_tags(per_run: pd.DataFrame, baseline: str = "baseline", fix: str = "fix") -> pd.DataFrame:
    """One row per metric (and render pass) per matrix point: before, after, change."""
    tags = set(per_run["tag"])
    for t in (baseline, fix):
        if t not in tags:
            raise ValueError(f"no runs tagged {t!r}; tags present: {sorted(tags)}")
    metrics = _METRICS + [(c, f"GPU {c.removeprefix('pass_').removesuffix('_ms')} pass (ms)")
                          for c in per_run.columns if c.startswith("pass_") and c.endswith("_ms")]
    rows = []
    for key, group in per_run.groupby(_MATCH_KEYS, sort=False):
        before, after = group[group["tag"] == baseline], group[group["tag"] == fix]
        if before.empty or after.empty:
            continue
        for col, label in metrics:
            diff, lo, hi = welch_interval(before[col], after[col])
            mean_before = before[col].mean()
            rows.append({
                **dict(zip(_MATCH_KEYS, key)),
                "metric": col, "label": label,
                "baseline": mean_before, "fix": after[col].mean(),
                "change": diff, "change_pct": 100 * diff / mean_before if mean_before else float("nan"),
                "ci_low": lo, "ci_high": hi,
                "runs_baseline": len(before), "runs_fix": len(after),
                "real_change": lo > 0 or hi < 0,
            })
    if not rows:
        raise ValueError("no matrix point has runs of both variants")
    return pd.DataFrame(rows)


def comparison_markdown(table: pd.DataFrame, baseline: str, fix: str) -> str:
    from gfl.tables import markdown_table

    lines = [f"# Before/after comparison: {baseline} vs {fix}", ""]
    for _, point in table.groupby(["preset", "resolution"], sort=False):
        first = point.iloc[0]
        ft = point[point["metric"] == "mean_frametime_ms"].iloc[0]
        verdict = "a real change" if ft["real_change"] else "not distinguishable from run-to-run noise"
        lines += [
            f"## {first['preset'].capitalize()} {first['resolution']}",
            "",
            f"Mean frame time changed by **{ft['change']:+.2f} ms** ({ft['change_pct']:+.1f}%), "
            f"95% interval {ft['ci_low']:+.2f} to {ft['ci_high']:+.2f} ms: {verdict}. "
            f"Runs: {first['runs_baseline']} {baseline}, {first['runs_fix']} {fix}.",
            "",
            markdown_table(pd.DataFrame({
                "Metric": point["label"],
                baseline.capitalize(): point["baseline"].map("{:.2f}".format),
                fix.capitalize(): point["fix"].map("{:.2f}".format),
                "Change": point["change"].map("{:+.2f}".format),
                "Change %": point["change_pct"].map("{:+.1f}".format),
                "95% interval": [f"{lo:+.2f} to {hi:+.2f}" for lo, hi in zip(point["ci_low"], point["ci_high"])],
            })),
            "",
        ]
    return "\n".join(lines) + "\n"
