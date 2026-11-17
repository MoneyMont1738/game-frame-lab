"""PresentMon vs. FrameView agreement check. Implemented in Phase 2.

Plan: align the two logs of the same run (FrameView starts by hotkey, so its
clock is offset), then report the difference in average FPS, 1% low, and p99
frame time, plus the per-frame frame-time error after alignment.
"""

from __future__ import annotations

from gfl.phases import pending


def validate_run(*args, **kwargs):
    raise pending(2, "PresentMon vs. FrameView validation")
