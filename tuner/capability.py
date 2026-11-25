"""Writability probing.

--status tells you what a control READS, not whether mVolt+ can WRITE it.
On the reference laptop the power limit reads back fine (enforced_mw = 80000)
but both `--power` and `--power-cap-watts` are rejected by the driver:

    {"success":false,"error":"Power limit is unavailable."}
    {"success":false,"error":"Power cap is unavailable or malformed."}

So capability = readback present AND a write accepted.

Every probe writes back the value the control ALREADY has, read from --status
just before probing. Nothing is assumed to be "neutral": 0 MHz, 100 % or 80 W
would overwrite whatever profile the user is running. Controls whose current
value cannot be read (write-only ones) are not probed at all - their verdict
is `None` (unknown).

Results are cached per GPU/driver/mVolt+ fingerprint, and never cached from a
non-elevated process (every write fails there, which says nothing about the GPU).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from .config import DATA

CACHE = DATA / "writable.json"
RAIL_KEYS = ("rel", "msvdd_rel")


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{float(v):.3f}"


def probe_args(status: dict[str, Any]) -> dict[str, list[list[str]]]:
    """Per knob: the commands that rewrite its current value (and undo side effects)."""
    from . import knobs

    def cur(key: str) -> Any:
        k = knobs.KNOBS[key]
        try:
            return k.readback(status) if k.readback else None
        except Exception:
            return None

    out: dict[str, list[list[str]]] = {}
    for key, flag in (("core", "--core"), ("mem", "--mem"), ("boost", "--boost"),
                      ("power", "--power"), ("xbar_offset", "--xbar-offset"),
                      ("sys_offset", "--sys-offset"), ("video_offset", "--video-offset")):
        v = cur(key)
        if v is not None:
            out[key] = [[flag, _fmt(v)]]

    comps = knobs.companions_from_status(status)
    for key, flag, prefix in (("rel", "--nvvdd-offsets", ""),
                              ("msvdd_rel", "--msvdd-offsets", "msvdd_")):
        rel = cur(key)
        vmin, alt = comps.get(f"{prefix}vmin"), comps.get(f"{prefix}alt")
        if rel is not None and vmin is not None and alt is not None:
            out[key] = [[flag, ",".join(_fmt(x) for x in (vmin, rel, alt))]]

    for key, flag in (("core_vdemand", "--core-vdemand"), ("xbar_vdemand", "--xbar-vdemand"),
                      ("sys_vdemand", "--sys-vdemand"), ("video_vdemand", "--video-vdemand"),
                      ("nvvdd_ocp", "--nvvdd-ocp"), ("msvdd_ocp", "--msvdd-ocp"),
                      ("msvdd_clock_ratio", "--msvdd-clock-ratio")):
        v = cur(key)
        if v is not None:
            out[key] = [[flag, _fmt(v) if key != "msvdd_clock_ratio" else f"{float(v):.5f}"]]

    lock = cur("boost_lock")
    if lock is not None:
        out["boost_lock"] = [["--boost-lock", "on" if knobs._as_bool(lock) else "off"]]

    # A held clock-range lock belongs to the user; releasing it is not a probe.
    cr = status.get("clock_range_mhz") or {}
    if isinstance(cr, dict) and cr.get("locked") is False and not cr.get("uncertain"):
        out["clock_max"] = [["--clock-range", "default"]]

    # Writing the current request switches percentage control to watt control.
    # --return-to-percentage undoes that without touching the percentage.
    cap = cur("cap")
    if cap is not None:
        out["cap"] = [["--power-cap-watts", _fmt(cap)], ["--return-to-percentage"]]
    return out


def fingerprint(status: dict[str, Any]) -> str:
    """Identity of GPU + VBIOS + driver + mVolt+ as far as --status reveals it."""
    ident = {k: v for k, v in status.items()
             if isinstance(v, (str, int, float)) and any(
                 t in k.lower() for t in ("name", "vbios", "bios", "adapter", "driver",
                                          "version", "device", "uuid"))}
    ident["_keys"] = sorted(status.keys())
    return hashlib.sha256(json.dumps(ident, sort_keys=True, default=str).encode()).hexdigest()[:16]


def probe(force: bool = False, status: dict[str, Any] | None = None) -> dict[str, Any]:
    """Rewrite each control's current value; cache the verdict."""
    from . import mvolt

    status = status if status is not None else mvolt.status()
    fp = fingerprint(status)
    if CACHE.exists() and not force:
        try:
            cached = json.loads(CACHE.read_text(encoding="utf-8"))
            if cached.get("fingerprint") == fp:
                return cached
        except Exception:
            pass

    if not mvolt.is_admin():
        # Not elevated: every write would fail. Report unknown, cache nothing.
        return {"probed_utc": None, "fingerprint": fp, "elevated": False, "knobs": {}}

    results: dict[str, Any] = {}
    for key, cmds in probe_args(status).items():
        try:
            res = mvolt.apply(cmds[0]) or {}
            if key in RAIL_KEYS and res.get("rail_offsets_ignored_by_driver"):
                # mVolt+ says success, but the driver drops rail offsets (seen
                # on NVIDIA 621.24): any value other than the current one fails.
                results[key] = {"writable": False,
                                "error": "the NVIDIA driver ignores rail offsets "
                                         "(rail_offsets_ignored_by_driver)"}
                continue
            results[key] = {"writable": True, "error": None}
        except Exception as exc:
            results[key] = {"writable": False, "error": str(exc)[:200]}
            continue
        for undo in cmds[1:]:
            try:
                mvolt.apply(undo)
            except Exception as exc:
                results[key]["undo_error"] = str(exc)[:200]

    payload = {
        "probed_utc": datetime.now(timezone.utc).isoformat(),
        "fingerprint": fp,
        "elevated": True,
        "knobs": results,
    }
    CACHE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def writable_map(force: bool = False, status: dict[str, Any] | None = None
                 ) -> dict[str, bool | None]:
    data = probe(force, status)
    out: dict[str, bool | None] = {}
    for key, rec in (data.get("knobs") or {}).items():
        out[key] = bool(rec.get("writable")) if isinstance(rec, dict) else None
    return out
