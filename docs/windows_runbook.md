# Windows run book

Step-by-step guide for a capture session on a Windows PC with an NVIDIA RTX
40-series GPU. Everything here is planned so that one session on borrowed or
rented hardware is enough: the code is tested beforehand with dry runs and
synthetic data, so time at the PC goes to capturing, not debugging.

## 1. Install (about 1 hour)

- Python 3.11, then `pip install -e ".[dev]"` in the repository folder
- PresentMon 2.x console application (GameTechDev/PresentMon releases)
- NVIDIA FrameView
- Nsight Graphics (for the GPU Trace in the Lumen study)
- The packaged Lyra build, copied from the machine that built it

Then:

```bash
copy gfl.local.example.yaml gfl.local.yaml
```

Edit `gfl.local.yaml` with the PresentMon path, and the `executable` paths in
`configs/lyra_*.yaml` with the Lyra path. Use an administrator terminal:
PresentMon needs it to read ETW events.

## 2. First-time checks (about 20 minutes)

Do these once, before any experiment. Each one protects the data.

1. **Dry run matches the plan.**
   ```bash
   gfl run configs/lyra_scalability.yaml --dry-run
   ```
2. **Settings are applied.** Launch one config by hand with the command the dry
   run prints. Open the console and type `sg.ShadowQuality` and `t.MaxFPS`; the
   values must match the command line. If Lyra re-applies its own saved
   settings after startup, the sg.* values will differ: fix that before
   capturing anything.
3. **Lumen state per preset.** In the same session, type
   `r.DynamicGlobalIlluminationMethod` at each preset. The engine's own
   scalability rules may turn Lumen off at Low and Medium. That is part of
   what the presets measure, but record which presets actually run Lumen.
4. **Camera path.** The benchmark map needs a repeatable camera path: a Level
   Sequence with a camera cut track, set to auto play and loop, in a copy of the
   map with no bots (AI makes frames depend on what the bots do). This is set
   up in the Unreal editor before packaging.
5. **One short test capture.** Run one config with `capture.runs: 1`, then
   `gfl analyze` on the result. Check the frame-time chart looks like a steady
   scene with a few hitches, not a loading screen.

## 3. Capture (about 3 to 4 hours)

Run the experiments in this order. Keep the PC idle otherwise: close browsers,
overlays, and launchers, and keep it plugged in.

| Experiment | Command | Time |
|---|---|---|
| Scalability | `gfl run configs/lyra_scalability.yaml` | about 60 min |
| Lumen baseline | `gfl run configs/lyra_lumen_fix.yaml` | about 5 min |
| Lumen fix | apply the fix, then `gfl run configs/lyra_lumen_fix.yaml --tag fix` | about 5 min |
| DLSS (Lyra) | `gfl run configs/lyra_dlss.yaml` (Phase 3) | about 80 min |
| Cyberpunk | `gfl run configs/cyberpunk_dlss.yaml` (Phase 3, by hand) | about 90 min |
| Contention | `gfl run configs/lyra_contention.yaml` (Phase 4) | about 15 min |

The Lumen fix should be a console variable change, so it needs no rebuild.

**FrameView, on every run:** press the FrameView benchmark hotkey a few
seconds after each run starts and again before it ends. Its logs go to its own
output folder; they are matched to runs by time afterwards.

## 4. Before leaving the PC

```bash
gfl validate data/raw/<capture folder> --frameview-dir "<FrameView log folder>"
```

```bash
gfl analyze data/raw/<capture folder>
```

Then copy all of `data/raw/` and the FrameView log folder to your own storage.
Raw logs are gitignored, so they exist only where you copy them.
