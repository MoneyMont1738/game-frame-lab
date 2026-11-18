# gflbench: Metal benchmark renderer

A small native renderer for Apple silicon Macs, written for this project. It
draws a ray-marched 3D scene in four GPU passes, times each pass on the GPU, and
writes a per-frame log that `gfl analyze` reads like a PresentMon capture.

It exists so the toolkit can produce real measurements on hardware without an
NVIDIA GPU, with full control over the workload: every quality setting is a
known amount of GPU work, and every pass is timed separately.

## Build and check (about 2 minutes)

Needs macOS on Apple silicon and the free Xcode command line tools. Shaders
compile when the program starts, so the full Xcode app is not needed.

```bash
xcode-select --install
```

```bash
bash bench/metal/build.sh
```

```bash
bench/metal/gflbench --preset low --resolution 1280x720 --duration 5 --output /tmp/gflbench_check.csv
```

A window shows the scene for 5 seconds, then the program prints how many frames
it wrote. If the build or the shader compilation fails, the error message says
exactly where; that output is what to report.

## What it renders

A camera orbits a ground plane covered in a grid of pillars, domes, and rings,
lit by several moving point lights. Everything is defined by a signed distance
function and drawn by ray marching, so the cost of each effect is set directly
by how many distance evaluations it makes.

| Pass | What it does | Cost grows with |
|---|---|---|
| `gbuffer` | Marches one ray per pixel to the first surface; stores position, depth, normal | march steps, detail layers |
| `shadows` | For each light, marches a shadow ray toward it; soft shadows from the closest miss | lights x shadow steps |
| `ao` | Ambient occlusion: samples the distance field along several directions around the normal | AO directions x AO steps |
| `composite` | Combines direct light, ambient light x AO, and fog; tonemaps into the window | resolution only |

## Presets

| Preset | March steps | Lights | Shadow steps | AO directions x steps | Detail layers |
|---|---:|---:|---:|---:|---:|
| low | 64 | 1 | 16 | 2 x 3 | 0 |
| medium | 96 | 2 | 24 | 4 x 4 | 1 |
| high | 128 | 3 | 32 | 6 x 5 | 2 |
| epic | 192 | 4 | 48 | 8 x 6 | 3 |

These values were chosen as a plausible quality ladder before any measurement.
Which pass dominates at each preset is a property of these choices and of the
GPU, and is something to measure, not assume.

## Options

| Option | Default | Meaning |
|---|---|---|
| `--preset` | high | low, medium, high, epic |
| `--resolution` | 1920x1080 | render resolution in pixels, independent of the window size |
| `--duration` | 60 | seconds to render before writing the log and exiting |
| `--output` | gflbench.csv | log path; a `.json` file with the same name records device, OS, and settings |
| `--shadows-scale`, `--ao-scale` | 1 | render that pass at a fraction of the resolution, then upsample with a depth-aware filter |
| `--vsync` | off | sync to the display; off by default so frame times show each frame's real cost |

## How timing works

- **Per-pass GPU time:** each pass is its own Metal command buffer, and Metal
  reports when the GPU started and finished each one (`gpuStartTime`,
  `gpuEndTime`). This is the same idea as a GPU trace's per-pass breakdown,
  recorded on every frame instead of one captured frame.
- **Frame time** (`MsBetweenPresents`): time between consecutive frames being
  handed to the display, the same definition PresentMon uses.
- **GPU busy** (`MsGPUBusy`): sum of the four pass times. With display sync off
  and a GPU-bound scene, this should be close to the frame time.
- **Display time** (`MsBetweenDisplayChange`): from Metal's presented
  timestamps; empty for frames that never reached the screen.
- **Memory** (`MetalAllocatedMiB`): memory this process has allocated through
  Metal. Apple silicon shares memory between CPU and GPU, so there is no
  separate VRAM figure.

## Running the experiment

```bash
gfl run configs/metal_scalability.yaml
```

```bash
gfl analyze data/raw/<capture folder>
```

12 configs x 3 runs x 60 s with cooldowns: about 45 minutes. Plug the Mac in,
turn off Low Power Mode, close other apps, and leave it alone while it runs.

**Thermals.** A MacBook Air has no fan and lowers its clock speed under long
sustained load. That would make later runs slower than earlier ones for reasons
that have nothing to do with the settings. Two checks in the analysis output
show whether it happened: the run-to-run coefficient of variation per config,
and whether run 3 is consistently slower than run 1. If it is, lengthen
`cooldown_s` in the config.

## The targeted fix (next step)

Once the scalability results show which pass dominates at the heaviest
setting, that pass gets one targeted change (for example rendering it at half
resolution with `--shadows-scale 0.5` or `--ao-scale 0.5`). The same config is
then captured with tag `baseline` and with tag `fix`, and the change in frame
time is reported in milliseconds, along with a visual check that image quality
held up.
