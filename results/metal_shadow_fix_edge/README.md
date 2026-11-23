# Results: metal_shadow_fix_edge (real data)

Follow-up to `metal_shadow_fix` on an Apple M3 MacBook Air: gflbench at High
2560x1440 with three variants captured alternately, 5 runs each, 60 s per run
with 60 s cooldowns: `baseline` (full-resolution shadows), `fix` (half-resolution
shadows, original depth-weighted filter) and `fix-edge` (half-resolution shadows,
edge-aware filter, `--upsample edge`).

`comparison_apple_m3_2026-11-23.csv` holds both comparisons exactly as
`gfl compare` printed them on the capture Mac (session started 13:18, Low Power
Mode off, on battery). Its baseline (50.12 ms per frame) matches the first
fix study (50.03 ms).

Image check (`scripts/compare_stills.py`, baseline vs fix-edge, scene time 5 s):
PSNR 45.50 dB, mean absolute difference 0.256 of 255, maximum 136 of 255,
2.23% of pixels changed by 2 or more levels. (The original fix: 44.10 dB,
0.272, 138, 2.34%.)

## Excluded session

An earlier session of the same experiment (started 12:09) ran with Low Power Mode
on and on battery (confirmed afterwards with `pmset`). Every render pass took
about 1.86 times as long (baseline 93.12 ms per frame instead of about 50 ms) and
runs disagreed widely (95% interval of the baseline vs fix-edge change: -61.6 to
-3.9 ms). It is excluded from all results. `gfl run` now checks `pmset` before a
Mac capture and asks before continuing on battery or in Low Power Mode.
