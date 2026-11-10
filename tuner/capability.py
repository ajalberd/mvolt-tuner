"""Writability probing.

--status tells you what a control READS, not whether mVolt+ can WRITE it.
On this laptop the power limit reads back fine (enforced_mw = 80000) but both
`--power` and `--power-cap-watts` are rejected by the driver:

    {"success":false,"error":"Power limit is unavailable."}
    {"success":false,"error":"Power cap is unavailable or malformed."}

So capability = readback present AND a benign write accepted.
Probe results are cached because each probe is a process spawn.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .config import DATA

CACHE = DATA / "writable.json"

# Benign, side-effect-free write for each knob. These values are the neutral
# setting, so a successful probe leaves the GPU where it started.
PROBES: dict[str, list[str]] = {
    "core":          ["--core", "0"],
    "mem":           ["--mem", "0"],
    "rel":           ["--nvvdd-offsets", "0,0,0"],
    "boost":         ["--boost", "0"],
    "boost_lock":    ["--boost-lock", "off"],
    "clock_max":     ["--clock-range", "default"],
    "power":         ["--power", "100"],
    "cap":           ["--power-cap-watts", "80"],
    "core_vdemand":  ["--core-vdemand", "0"],
    "xbar_offset":   ["--xbar-offset", "0"],
    "sys_offset":    ["--sys-offset", "0"],
    "video_offset":  ["--video-offset", "0"],
    "msvdd_rel":     ["--msvdd-offsets", "0,0,0"],
}


def probe(force: bool = False) -> dict[str, Any]:
    """Try a neutral write per knob; cache the verdict."""
    if CACHE.exists() and not force:
        try:
            return json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass

    from . import mvolt

    results: dict[str, Any] = {}
    for key, args in PROBES.items():
        try:
            mvolt.apply(args)
            results[key] = {"writable": True, "error": None}
        except Exception as exc:
            results[key] = {"writable": False, "error": str(exc)[:200]}

    payload = {
        "probed_utc": datetime.now(timezone.utc).isoformat(),
        "knobs": results,
    }
    CACHE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def writable_map(force: bool = False) -> dict[str, bool | None]:
    data = probe(force)
    out: dict[str, bool | None] = {}
    for key, rec in (data.get("knobs") or {}).items():
        out[key] = bool(rec.get("writable")) if isinstance(rec, dict) else None
    return out
