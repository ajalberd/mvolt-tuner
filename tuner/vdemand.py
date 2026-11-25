"""Voltage-demand sweep: does asking for less voltage at every clock buy watts
(or, under a binding power cap, clocks) at a config that is already confirmed?

REL (the NVVDD voltage limit) only caps the TOP of the V/F curve. At a power
budget the card runs below that top, where REL does nothing. The core
voltage-demand offset (`--core-vdemand`) moves the voltage requested at every
clock, so it is the remaining lever there - and it can be unstable at any
clock, not only at the top.

How it runs:
  * one LANE per base config (normally the confirmed efficiency points);
  * each lane walks the values in order (0 first, then deeper), `repeats`
    runs per value; 0 is measured again so every step is compared within the
    same session;
  * lanes are interleaved (A0 B0 A-10 B-10 ...) so slow drift hits them alike;
  * the first crash ends a lane; its edge is the last fully clean value. A
    crash at v is a crash at every deeper v of that lane.

The same lane walk sweeps the memory offset (`key="mem"`): it starts at the
base config's own memory offset and steps UP, so the first value is the
same-session reference and a crash ends the lane the same way.

All state lives in the journal, so the sweep is resume-safe.
"""
from __future__ import annotations

from typing import Any

from .edge import STABLE, UNSTABLE
from .state import param_key

KEY = "core_vdemand"
# Settings a lane sweep can walk. vdemand walks down from 0; the others walk
# up from the base config's own value.
SWEEP_KEYS = {KEY: "core voltage demand", "mem": "memory offset"}


def make_plan(bases: list[dict[str, Any]], values: list[float],
              repeats: int = 2, since_id: int = 0, key: str = KEY) -> dict[str, Any]:
    """`bases`: [{"params": {...}, "label": "..."}]. Values are walked from 0
    toward the most negative; 0 is always included. Only journal rows with an
    id above `since_id` count, so a new sweep measures afresh (scores can shift
    between sessions; the comparison must be within one sweep)."""
    if not bases:
        raise ValueError("pick at least one config to sweep")
    if not 1 <= int(repeats) <= 10:
        raise ValueError("runs per value must be between 1 and 10")
    if key not in SWEEP_KEYS:
        raise ValueError(f"cannot sweep {key!r}")
    if key == KEY:
        ref = 0.0
        vals = sorted({float(v) for v in values} | {ref}, reverse=True)
        if any(v > 0 for v in vals):
            raise ValueError("voltage-demand values must be 0 or negative (an undervolt)")
        if len(vals) < 2:
            raise ValueError("need at least one negative value")
    else:
        refs = {float(dict(b["params"]).get(key) or 0) for b in bases}
        if len(refs) != 1:
            raise ValueError(f"the picked configs must share one {SWEEP_KEYS[key]} to start from")
        ref = refs.pop()
        vals = sorted({float(v) for v in values} | {ref})
        if vals[0] < ref:
            raise ValueError(f"{SWEEP_KEYS[key]} values must be above the config's own "
                             f"{ref:g} (the sweep steps up from it)")
        if len(vals) < 2:
            raise ValueError(f"need at least one value above {ref:g}")
    out = []
    for b in bases:
        p = {k: v for k, v in dict(b["params"]).items() if k != KEY}
        out.append({"params": p, "label": str(b.get("label") or "")})
    return {"bases": out, "values": vals, "repeats": int(repeats), "since_id": int(since_id),
            "key": key, "ref": ref}


def point(base: dict[str, Any], v: float, key: str = KEY) -> dict[str, Any]:
    p = dict(base["params"])
    p[key] = float(v)
    return p


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def lanes(trials: list[dict[str, Any]], plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Per base: one row per value tried, the lane's edge, and its next run."""
    by_key: dict[str, list[dict[str, Any]]] = {}
    since = int(plan.get("since_id") or 0)
    for t in trials:
        if since and int(t.get("id") or 0) <= since:
            continue
        if t.get("status") in STABLE + UNSTABLE:
            by_key.setdefault(param_key(t.get("params") or {}), []).append(t)
    reps, key = plan["repeats"], plan.get("key") or KEY
    out = []
    for i, base in enumerate(plan["bases"]):
        rows, nxt, edge, done, crashed_at = [], None, None, False, None
        for v in plan["values"]:
            runs = by_key.get(param_key(point(base, v, key)), [])
            ok = [t for t in runs if t["status"] in STABLE]
            bad = [t for t in runs if t["status"] in UNSTABLE]
            scored = [t for t in ok if t.get("status") == "ok"]
            rows.append({
                "value": v, "runs": len(runs), "crashes": len(bad),
                "score_mean": _mean([float(t["score"]) for t in scored if t.get("score")]),
                "watts_mean": _mean([float(t["watts"]) for t in scored
                                     if t.get("watts") is not None]),
                "clk_mean": _mean([float(t["clk_avg"]) for t in scored
                                   if t.get("clk_avg") is not None]),
            })
            if bad:
                done, crashed_at = True, v
                break
            if len(ok) < reps:
                nxt = {"value": v, "params": point(base, v, key), "step": plan["values"].index(v)}
                break
            edge = v
        else:
            done = True
        # The first value is the reference: 0 mV, or the config's own memory offset.
        zero = rows[0] if rows and rows[0]["value"] == plan["values"][0] else None
        for r in rows:
            r["d_score"] = (r["score_mean"] - zero["score_mean"]
                            if zero and r["score_mean"] is not None
                            and zero["score_mean"] is not None and r is not zero else None)
            r["d_watts"] = (r["watts_mean"] - zero["watts_mean"]
                            if zero and r["watts_mean"] is not None
                            and zero["watts_mean"] is not None and r is not zero else None)
        out.append({"index": i, "label": base["label"], "params": base["params"],
                    "rows": rows, "edge": edge, "crashed_at": crashed_at,
                    "done": done, "next": nxt})
    return out


def next_point(trials: list[dict[str, Any]], plan: dict[str, Any],
               exclude: set[str] | None = None) -> dict[str, Any] | None:
    """The next run: the open lane that is least far along (interleaving)."""
    exclude = exclude or set()
    open_ = [l for l in lanes(trials, plan) if not l["done"] and l["next"]
             and param_key(l["next"]["params"]) not in exclude]
    if not open_:
        return None
    lane = min(open_, key=lambda l: (l["next"]["step"], l["index"]))
    return dict(lane["next"]["params"])


def estimate_runs(trials: list[dict[str, Any]], plan: dict[str, Any]) -> int:
    """Upper bound on the runs still needed (every lane reaching the last value)."""
    n = 0
    for l in lanes(trials, plan):
        if l["done"]:
            continue
        done_vals = len(l["rows"]) - 1
        n += (len(plan["values"]) - done_vals) * plan["repeats"] - l["rows"][-1]["runs"]
    return max(n, 0)
