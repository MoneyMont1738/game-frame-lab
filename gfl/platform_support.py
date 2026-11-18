"""Guards for code that only works on the Windows benchmark PC.

Capture (PresentMon), game launching, and TensorRT inference need Windows and an
NVIDIA GPU. Analysis runs anywhere. Modules that need Windows must stay importable
on Linux and macOS (so tests and `gfl analyze` work), and only fail when a
Windows-only action is actually requested, with a message that says what to do.
"""

from __future__ import annotations

import platform
import sys


class UnsupportedPlatformError(RuntimeError):
    """Raised when a Windows-only or macOS-only feature is used on another OS."""


def is_windows() -> bool:
    return sys.platform == "win32"


def is_macos() -> bool:
    return sys.platform == "darwin"


def require_macos(feature: str) -> None:
    """Raise a clear error if `feature` is requested off macOS."""
    if is_macos():
        return
    raise UnsupportedPlatformError(
        f"{feature} only runs on macOS (this machine is {platform.system()}). "
        "The Metal benchmark renderer needs a Mac with Apple silicon. On this "
        "machine you can still analyze existing data, for example: "
        "gfl analyze data/sample/synthetic_metal_bench"
    )


def require_windows(feature: str) -> None:
    """Raise a clear error if `feature` is requested off Windows."""
    if is_windows():
        return
    raise UnsupportedPlatformError(
        f"{feature} only runs on Windows (this machine is {platform.system()}). "
        "Capture and game launching need the Windows benchmark PC with PresentMon "
        "and an NVIDIA GPU. On this machine you can still analyze existing data, "
        "for example: gfl analyze data/sample/synthetic_lyra_scalability"
    )
