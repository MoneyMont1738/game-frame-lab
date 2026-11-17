"""Game launcher (Windows only). Implemented in Phase 2.

Plan: start the packaged Lyra build with the resolution and sg.* scalability
console variables as command-line arguments (for example -ResX=2560 -ResY=1440
-ExecCmds="sg.ShadowQuality 3, ...") so every run starts from the same state.
Games with launcher "manual" (Cyberpunk 2077) are started by hand.
"""

from __future__ import annotations

from gfl.phases import pending
from gfl.platform_support import require_windows


def launch_game(*args, **kwargs):
    require_windows("Game launching")
    raise pending(2, "Game launching")
