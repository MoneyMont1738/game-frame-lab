"""Small helpers for printing tables as Markdown (used in summaries and reports)."""

from __future__ import annotations

import pandas as pd


def markdown_table(df: pd.DataFrame) -> str:
    """Render a DataFrame as a GitHub-flavored Markdown table.

    Text columns are left-aligned, everything else right-aligned.
    """
    headers = [str(c) for c in df.columns]
    rows = [[str(v) for v in row] for row in df.itertuples(index=False)]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h)
              for i, h in enumerate(headers)]
    numeric = [_looks_numeric(df.iloc[:, i]) for i in range(len(headers))]

    def fmt(cells):
        out = [c.rjust(w) if num else c.ljust(w) for c, w, num in zip(cells, widths, numeric)]
        return "| " + " | ".join(out) + " |"

    sep = ["-" * (w - 1) + ":" if num else "-" * w for w, num in zip(widths, numeric)]
    return "\n".join([fmt(headers), "| " + " | ".join(sep) + " |", *(fmt(r) for r in rows)])


def _looks_numeric(col: pd.Series) -> bool:
    values = col.astype(str).str.rstrip("*")
    return bool(len(values)) and values.str.fullmatch(r"[-+]?\d*\.?\d+|n/a").all()
