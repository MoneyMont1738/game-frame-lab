"""TensorRT load generator (Windows + NVIDIA only). Implemented in Phase 4.

Plan: run a Real-ESRGAN x2 compact model as an FP16 TensorRT engine on 1080p
frames at a fixed 30 inferences per second per stream, with 0, 1, or 2 streams,
while the game is captured. A --dry-run mode will simulate the pacing loop
without a GPU so the scheduling logic can be tested anywhere.

TensorRT is imported lazily inside the functions that need it, so this module
can be imported on machines without it.
"""

from __future__ import annotations

from gfl.phases import pending


def main() -> None:
    raise pending(4, "TensorRT load generator")


if __name__ == "__main__":
    main()
