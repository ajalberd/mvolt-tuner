"""Smart search, stage 1: EXPLORE.

What someone wants from an overclock is not to hand-step offsets; it is to know
which knobs matter, by how much, and what the best score-vs-watts trade-offs are.

Explore is a Latin-hypercube spread over every included knob (wide ranges around
the starting profile, plus the power budget). It is built to measure effects
(analysis.py), not to optimise; crashes are data - they map the stability edge.
The later stages are edge.py: edge search per power budget x undervolt, then
confirmation runs.

All values in a space are ABSOLUTE (start value already added).
"""
from __future__ import annotations

import json
import math
import random
from typing import Any

from . import knobs

# Default exploration span per knob, relative to the starting value, and the
# step it is quantised to. Steps follow what the hardware resolves (NVIDIA core
# clocks move in ~15 MHz bins; mVolt+ voltage offsets in 5 mV).
SPANS: dict[str, dict[str, float]] = {
    "core":              {"lo": -200, "hi": 200, "step": 15},
    "mem":               {"lo": -1000, "hi": 1000, "step": 100},
    # hi is generous on purpose: build_space clips it to your voltage limit (or
    # to mVolt+'s standard 1.15 V ceiling when you set none).
    "rel":               {"lo": -100, "hi": 200, "step": 5},
    # Fabric clocks run all the time, idle included: too high hangs the GPU the
    # moment it is applied - a full freeze (black screen, fans at max), not a
    # recoverable driver reset. Measured on an RTX 5080: XBAR +700 and SYS +750
    # froze on apply; +550 / +450 ran clean. So: little headroom above start.
    "xbar_offset":       {"lo": -200, "hi": 150, "step": 50},
    "sys_offset":        {"lo": -200, "hi": 150, "step": 50},
    "video_offset":      {"lo": -200, "hi": 150, "step": 50},
    "msvdd_rel":         {"lo": -50, "hi": 25, "step": 5},
    "core_vdemand":      {"lo": -50, "hi": 25, "step": 5},
    "xbar_vdemand":      {"lo": -50, "hi": 25, "step": 5},
    "sys_vdemand":       {"lo": -50, "hi": 25, "step": 5},
    "video_vdemand":     {"lo": -50, "hi": 25, "step": 5},
    "boost":             {"lo": 0, "hi": 100, "step": 10},
    "msvdd_clock_ratio": {"lo": -0.1, "hi": 0.1, "step": 0.01},
    "nvvdd_ocp":         {"lo": -50, "hi": 0, "step": 10},
    "msvdd_ocp":         {"lo": -20, "hi": 0, "step": 5},
}
# Included by default: the levers that usually matter. The rest are opt-in.
DEFAULT_ON = ("core", "mem", "rel", "xbar_offset", "sys_offset", "cap")
CAP_STEP = 5.0
# Never searched: power% (the budget is searched as a watt cap), switches.
NOT_SEARCHABLE = {"power", "boost_lock", "clock_max"}
COMPANION_KEYS = {"vmin", "alt", "msvdd_vmin", "msvdd_alt"}


def _q(v: float, step: float, lo: float) -> float:
    """Quantise onto the grid lo + k*step."""
    k = round((v - lo) / step)
    out = lo + k * step
    return int(round(out)) if float(step).is_integer() and float(lo).is_integer() else round(out, 4)


def default_selection(status: dict[str, Any], start: dict[str, Any],
                      limits: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Per searchable knob: on/off and a RELATIVE span, for the UI to edit."""
    rows = {r.key: r for r in knobs.survey(status)}
    out: dict[str, dict[str, Any]] = {}
    for key, sp in SPANS.items():
        r = rows.get(key)
        if not r or not r.available:
            continue
        out[key] = {"on": key in DEFAULT_ON, **sp}
    pw = knobs.power_info(status)
    top = (limits or {}).get("max_watts") or pw.get("default_w")
    if top:
        # The whole power range the card allows, so the front covers every budget.
        lo = math.ceil(float(pw.get("min_w") or round(float(top) * 0.7)) / CAP_STEP) * CAP_STEP
        out["cap"] = {"on": True, "lo": lo, "hi": float(top), "step": CAP_STEP, "absolute": True}
    return out


def _voltage_room(status: dict[str, Any], limits: dict[str, Any] | None) -> dict[str, float]:
    """Highest REL offset that can still pass the user's voltage limits.

    The effective rail limit moves ~1:1 with REL, so with a 1000 mV limit on a
    card whose stock is 1050 mV, REL above -50 would only be refused after the
    apply - a wasted trial. Clip the space instead.
    """
    out: dict[str, float] = {}
    rails = knobs.rail_info(status)
    ms_now = knobs._mv_from_uv(knobs._get(status, "msvdd_offsets_uv", "rel"))
    for key, rail, lim_key in (("rel", "core", "max_core_mv"),
                               ("msvdd_rel", "fabric", "max_fabric_mv")):
        cap = (limits or {}).get(lim_key)
        stock = rails[rail]["stock_mv"]
        if cap is None:
            # No limit of yours: stop at mVolt+'s standard ceiling - above it
            # needs XOC, which is only used when your limit asks for it.
            cap = rails[rail]["standard_max_mv"]
        if cap is None or stock is None:
            continue
        # The offset at which the rail sits at its stock limit: 0, except on the
        # fabric rail when the driver's own -50 mV is in effect.
        base = knobs.MSVDD_DRIVER_REL_MV if (rail == "fabric" and
                                             ms_now == knobs.MSVDD_DRIVER_REL_MV) else 0.0
        out[key] = float(cap) - float(stock) + base
    return out


def build_space(status: dict[str, Any], start: dict[str, Any],
                selection: dict[str, dict[str, Any]],
                limits: dict[str, Any] | None) -> dict[str, Any]:
    """Absolute search space: ranges clamped to the driver's and the user's limits."""
    rows = {r.key: r for r in knobs.survey(status)}
    room = _voltage_room(status, limits)
    dims: dict[str, dict[str, float]] = {}
    notes: list[str] = []
    for key, sel in selection.items():
        if not sel.get("on") or key in NOT_SEARCHABLE:
            continue
        step = float(sel.get("step") or SPANS.get(key, {}).get("step") or 1)
        if step <= 0:
            raise ValueError(f"{key}: step must be > 0")
        if sel.get("absolute") or key == "cap":
            lo, hi = float(sel["lo"]), float(sel["hi"])
        else:
            base = float(start.get(key, rows[key].current if key in rows and
                                    isinstance(rows[key].current, (int, float)) else 0) or 0)
            lo, hi = base + float(sel["lo"]), base + float(sel["hi"])
        if hi < lo:
            lo, hi = hi, lo
        b = rows[key].bounds if key in rows else None
        if b:
            lo, hi = max(lo, b[0]), min(hi, b[1])
        if key == "cap":
            pw = knobs.power_info(status)
            if pw.get("max_w"):
                hi = min(hi, float(pw["max_w"]))
            if (limits or {}).get("max_watts") is not None:
                hi = min(hi, float(limits["max_watts"]))
        if key in room and hi > room[key]:
            notes.append(f"{key} capped at {room[key]:g} mV by your voltage limit")
            hi = room[key]
        if hi < lo:
            notes.append(f"{key}: nothing left of the range inside your limits - skipped")
            continue
        if key == "cap":
            lo = math.ceil(lo / step - 1e-9) * step   # whole-step watts, e.g. 248.4 -> 250
        lo = _q(lo, step, lo)
        hi = lo + math.floor((hi - lo) / step + 1e-9) * step
        dims[key] = {"lo": lo, "hi": round(hi, 4), "step": step}
    fixed = {k: v for k, v in start.items() if k not in dims}
    return {"dims": dims, "fixed": fixed, "notes": notes,
            "link": knobs.link_reference(start)}


def _levels(d: dict[str, float]) -> int:
    return int(round((d["hi"] - d["lo"]) / d["step"])) + 1


def lhs_points(space: dict[str, Any], n: int, seed: int = 1) -> list[dict[str, Any]]:
    """Latin hypercube: every knob's range is cut into n strata and each stratum
    is hit exactly once - an even spread in few runs, ideal for importance."""
    rnd = random.Random(seed)
    dims = space["dims"]
    cols: dict[str, list[float]] = {}
    for key, d in dims.items():
        strata = list(range(n))
        rnd.shuffle(strata)
        span = d["hi"] - d["lo"]
        cols[key] = [_q(d["lo"] + (s + rnd.random()) / n * span, d["step"], d["lo"])
                     for s in strata]
        cols[key] = [min(max(v, d["lo"]), d["hi"]) for v in cols[key]]
    pts, seen = [], set()
    for i in range(n):
        p = knobs.link_rails({**space["fixed"], **{k: cols[k][i] for k in dims}},
                             space.get("link") or {})
        key = json.dumps(p, sort_keys=True)
        if key not in seen:
            seen.add(key)
            pts.append(p)
    # Run from mild to aggressive. The spread is the same; what changes is that
    # a crash comes after the safer configs have been measured, and it then
    # rules out the more aggressive ones still queued (orchestrator.ruled_out).
    return sorted(pts, key=lambda p: risk(p, space))


def risk(p: dict[str, Any], space: dict[str, Any]) -> float:
    """0 = mildest corner of the space, 1 per knob at its most aggressive end."""
    total = 0.0
    for key, d in space["dims"].items():
        span = d["hi"] - d["lo"]
        if span <= 0 or key not in p:
            continue
        frac = (float(p[key]) - d["lo"]) / span
        direction = knobs.AGGRESSION.get(key, 0)
        total += frac if direction > 0 else (1 - frac) if direction < 0 else 0.0
    return total
