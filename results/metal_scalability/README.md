# Results: metal_scalability (real data)

Real measurements from the gflbench Metal renderer on an Apple M3 MacBook Air
(macOS 26.5.1), captured November 18, 2026: 12 configs x 3 runs x 60 s.

`summary_apple_m3_2026-11-18.csv` holds the per-config values exactly as
`gfl analyze` printed them on the capture Mac (rounded as printed), including
the per-pass GPU times. The raw per-frame logs (`data/raw/`, gitignored) and
the analysis files written on that Mac (`summary.csv`, `per_run_metrics.csv`,
charts) stay on the Mac.

Column meanings: see the [lab report](../../reports/lab_report_apple_m3_scalability.md).
Every row's 0.1% low rests on very few frames and is indicative only.
