# Results: metal_shadow_fix (real data)

Before/after study on an Apple M3 MacBook Air (macOS 26.5.1), captured
November 18, 2026: gflbench at High 2560x1440, 5 baseline runs (shadows at full
resolution) and 5 fix runs (`--shadows-scale 0.5`, shadows at half width and
height), alternating, 60 s each with 60 s cooldowns.

`comparison_apple_m3_2026-11-18.csv` holds the values exactly as `gfl compare`
printed them on the capture Mac. `change` is fix minus baseline; `ci_low` and
`ci_high` are the 95% confidence interval of that change (Welch's t-test on the
per-run means). Raw per-frame logs stay on the capture Mac (`data/raw/`, gitignored).

Image check (`scripts/compare_stills.py`, High 2560x1440, scene time 5 s):
PSNR 44.10 dB, mean absolute difference 0.272 of 255, maximum 138 of 255,
2.34% of pixels changed by 2 or more levels.
