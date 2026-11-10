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

This module detects the stale lease and releases it before a run starts.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
from pathlib import Path
from typing import Any

from . import mvolt

MVOLT_DATA = Path(os.environ.get("LOCALAPPDATA", "")) / "mVolt+"
LEASE_GLOB = "clock-range-lease-*.json*"


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def find_stale_leases() -> list[Path]:
    out: list[Path] = []
    for path in glob.glob(str(MVOLT_DATA / LEASE_GLOB)):
        p = Path(path)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            out.append(p)          # unreadable counts as stale
            continue
        pid = data.get("pid")
        if not _pid_alive(pid):
            out.append(p)
    return out


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


def clear_stale_lease(backup_dir: Path, log=print) -> bool:
    """Release a stale clock-range lease. Returns True if it acted."""
    stale = find_stale_leases()
    if not stale:
        return False

    log(f"stale clock-range lease found: {len(stale)} file(s)")
    backup_dir.mkdir(parents=True, exist_ok=True)
    for p in stale:
        try:
            shutil.copy2(p, backup_dir / p.name)
        except Exception:
            pass

    try:
        mvolt.apply(["--clock-range", "default"])
        log("released stale lease with --clock-range default")
        return True
    except Exception as exc:
        log(f"WARNING: lease release failed ({exc}); removing files")
        for p in stale:
            try:
                p.unlink()
            except Exception:
                pass
        return True
