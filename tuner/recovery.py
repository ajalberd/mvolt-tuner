"""Startup recovery for stale mVolt+ leases.

WHY THIS EXISTS
---------------
Applying `--clock-range` takes a persistent "lease" file:

    %LOCALAPPDATA%\\mVolt+\\clock-range-lease-<adapter>.json
    %LOCALAPPDATA%\\mVolt+\\clock-range-lease-<adapter>.json.recovery

If the process that took the lease dies (crash, kill, host hang) the file
survives with a dead pid and an old boot_id. Every later tuning call then fails:

    {"success":false,"error":"Could not open the settings application safety marker."}

which makes the GPU look un-tunable. The release command is:

    mVolt+.exe --clock-range default

This module releases the lease. It is called only when an apply is refused with
the "safety marker" error, so a clock-range lock the user set on purpose is left
alone. Lease files are never deleted by hand.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import mvolt

MVOLT_DATA = Path(os.environ.get("LOCALAPPDATA", "")) / "mVolt+"
LEASE_GLOB = "clock-range-lease-*.json*"


def force_release(attempts: int = 10, gap_s: float = 5.0, log=print) -> bool:
    """Unconditionally release the clock-range lease, with generous backoff.

    After an unclean shutdown mVolt+ ADOPTS an orphaned clock-range lock
    ("adopted an orphaned clock-range lock" in startup.log), then refuses every
    apply with "Could not open the settings application safety marker" until it
    is released.

    The release is REFUSED if called too early after boot: observed failing ~48 s
    after boot and succeeding ~90 s after boot. Hence the long backoff - a short
    retry loop looks exactly like a permanent failure.
    """
    import time

    for i in range(1, attempts + 1):
        try:
            mvolt.apply(["--clock-range", "default"])
            if i > 1:
                log(f"lease released on attempt {i} (~{int((i - 1) * gap_s)}s)")
            return True
        except Exception as exc:
            msg = str(exc)
            if "safety marker" in msg and i < attempts:
                log(f"lease still held (attempt {i}/{attempts}); retry in {gap_s:.0f}s")
                time.sleep(gap_s)
                continue
            log(f"lease release failed after {i} attempt(s): {msg[:140]}")
            return False
    return False
