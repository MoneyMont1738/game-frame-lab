# Experiment configs

Each YAML file describes one experiment. `gfl run <file>` expands its `matrix`
into every combination of values and captures each combination `capture.runs`
times. Validate a file and see the run plan on any OS with:

```bash
gfl run configs/lyra_scalability.yaml --dry-run
```

## Files

| File | Experiment | Configs |
|---|---|---|
| `lyra_scalability.yaml` | 4 scalability presets x 3 resolutions, native | 12 |
| `lyra_dlss.yaml` | DLSS modes x Frame Generation at 1440p and 4K, Epic | 16 |
| `cyberpunk_dlss.yaml` | Same DLSS matrix in the Cyberpunk 2077 benchmark (launched by hand) | 16 |
| `lyra_contention.yaml` | 0, 1, 2 concurrent TensorRT inference streams, Epic 1440p | 3 |
| `lyra_lumen_fix.yaml` | Before/after a targeted Lumen fix, Epic 4K (tag `baseline` then `fix`) | 1 |
| `metal_scalability.yaml` | gflbench Metal renderer, 4 presets x 3 resolutions, on a Mac | 12 |

## Schema

```yaml
experiment: lyra_scalability     # required; lowercase letters, digits, _ or -
description: >                   # optional free text
  What this experiment measures.

scene:
  name: lyra                     # required; used in config IDs
  launcher: unreal               # required; "unreal" (gfl launches it, Windows),
                                 # "manual" (started by hand), or "gflbench" (macOS)
  executable: "C:/.../LyraGame.exe"   # required when launcher is "unreal" or "gflbench"
  process_name: LyraGame.exe     # required; PresentMon captures this process
  map: /ShooterMaps/Maps/L_Expanse    # optional
  extra_args: []                 # optional extra command-line arguments

capture:                         # optional; defaults shown
  runs: 3                        # runs per config
  duration_s: 60                 # length of each capture
  warmup_s: 10                   # dropped from the start of every run
  cooldown_s: 15                 # pause between runs so clocks and temperatures settle

tag: baseline                    # optional; default "baseline" ("fix" for the after runs)

matrix:                          # every combination is one config
  preset: [low, medium, high, epic]   # required; "manual" only with launcher "manual"
  resolution: [1920x1080, 2560x1440, 3840x2160]   # required; WIDTHxHEIGHT
  dlss_mode: [native]            # optional; native | quality | balanced | performance
  frame_generation: [false]      # optional; true | false
  inference_streams: [0]         # optional; concurrent inference streams (0, 1, 2)
```

Each expanded config gets an ID such as
`lyra_epic_3840x2160_native_fg-off_s0_baseline`, which names its output files
and its rows in the summary tables.

Validation is strict on purpose: an unknown preset, a malformed resolution, a
warmup as long as the capture, or a duplicated matrix value is an error, not a
silent default. Errors name the file and the field.
