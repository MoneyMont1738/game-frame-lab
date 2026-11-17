"""PresentMon capture wrapper (Windows only). Implemented in Phase 2.

Plan: start PresentMon 2.x with
    --process_name <exe> --output_file <csv> --timed <duration_s>
    --terminate_after_timed
alongside an nvidia-smi telemetry logger, then append the run to manifest.csv.
Importing this module is safe on any OS; calling it off Windows raises
UnsupportedPlatformError with an explanation.
"""

from __future__ import annotations

from gfl.phases import pending
from gfl.platform_support import require_windows


def capture_run(*args, **kwargs):
    require_windows("PresentMon capture")
    raise pending(2, "PresentMon capture")
