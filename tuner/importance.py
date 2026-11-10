"""Which knobs actually move the two outcomes?

Optuna answers this with fANOVA on its trial history. This is the same question
with a statistic that stays honest on a few dozen trials: **eta squared** (the
share of total variance in an outcome explained by a knob), plus the raw
best-vs-worst spread so the number has units.

    eta^2 = SS_between / SS_total      0 = no effect, 1 = explains everything

Two caveats that matter, and are reported rather than hidden:
  * eta^2 rises with the number of levels even when there is no effect, so it is
    reported next to the level count and only computed for knobs with >= 2
    distinct values.
  * a knob that was never varied cannot be scored - it is listed as `not tested`,
    not as zero importance.
"""
from __future__ import annotations

import math
from typing import Any

OUTCOMES = ("score", "watts")
MIN_REPEATS_PER_LEVEL = 2


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _eta_squared(groups: list[list[float]]) -> float | None:
    """Share of variance explained by group membership."""
    pooled = [v for g in groups for v in g]
    if len(pooled) < 3:
        return None
    grand = _mean(pooled)
    ss_total = sum((v - grand) ** 2 for v in pooled)
    if ss_total <= 1e-12:
        return None  # no variance at all - nothing can explain it
    ss_between = sum(len(g) * (_mean(g) - grand) ** 2 for g in groups if g)
    return max(0.0, min(1.0, ss_between / ss_total))


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = _mean(xs), _mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx <= 1e-12 or dy <= 1e-12:
        return None
    return num / (dx * dy)


def knob_effects(trials: list[dict[str, Any]],
                 only: set[str] | None = None) -> dict[str, Any]:
    """Per-knob effect on score and wattage.

    `trials` items: {"params": {knob: value}, "score": float|None, "watts": float|None}
    `only`: if given, score just these knobs - the ones the tuner deliberately
    swept. Without it a control that merely *appeared* in the params of one
    trial (a knob later dropped from the grid) can look important.
    """
    usable = [t for t in trials if t.get("score")]
    if not usable:
        return {"knobs": [], "trials": 0, "note": "no scored trials yet"}

    # every knob mentioned anywhere, including those only present at 0
    keys: set[str] = set()
    for t in usable:
        keys.update((t.get("params") or {}).keys())
    if only:
        keys &= set(only)

    out = []
    for key in sorted(keys):
        values = [(int((t.get("params") or {}).get(key) or 0), t) for t in usable]
        distinct = sorted({v for v, _ in values})

        entry: dict[str, Any] = {
            "knob": key,
            "levels": len(distinct),
            "tested": len(distinct) > 1,
            "n": len(values),
            "values": distinct,
        }

        if len(distinct) < 2:
            entry["note"] = "never varied - cannot be scored"
            out.append(entry)
            continue

        # A level seen once carries no variance information. Rather than throw
        # the knob away (a sweep legitimately has n=1 at its extremes), score it
        # only on the levels that were repeated, and say which were dropped.
        counts = {v: sum(1 for vv, _ in values if vv == v) for v in distinct}
        kept = [v for v in distinct if counts[v] >= MIN_REPEATS_PER_LEVEL]
        dropped = [v for v in distinct if counts[v] < MIN_REPEATS_PER_LEVEL]

        if len(kept) < 2:
            entry["tested"] = False
            entry["note"] = (f"needs >={MIN_REPEATS_PER_LEVEL} trials at two or more "
                             f"levels; counts={counts}")
            out.append(entry)
            continue

        entry["levels_scored"] = kept
        if dropped:
            entry["levels_dropped"] = dropped
        distinct = kept

        for outcome in OUTCOMES:
            groups: list[list[float]] = []
            level_stats = []
            for v in distinct:
                vals = [t[outcome] for vv, t in values
                        if vv == v and t.get(outcome) is not None]
                groups.append([float(x) for x in vals])
                level_stats.append({
                    "value": v,
                    "n": len(vals),
                    "mean": round(_mean([float(x) for x in vals]), 2) if vals else None,
                })

            eta = _eta_squared(groups)
            entry[f"{outcome}_eta2"] = round(eta, 3) if eta is not None else None
            means = [s["mean"] for s in level_stats if s["mean"] is not None]
            if means:
                entry[f"{outcome}_spread"] = round(max(means) - min(means), 2)
                entry[f"{outcome}_best"] = max(level_stats,
                                               key=lambda s: s["mean"] if s["mean"] is not None else -1e9)
                entry[f"{outcome}_worst"] = min(level_stats,
                                                key=lambda s: s["mean"] if s["mean"] is not None else 1e9)
            else:
                entry[f"{outcome}_spread"] = None
            entry[f"{outcome}_levels"] = level_stats

        # monotonic trend where the knob is numeric
        xs = [float(v) for v, _ in values]
        entry["score_trend"] = _round(_pearson(xs, [float(t["score"]) for _, t in values]))
        watts_pairs = [(float(v), float(t["watts"])) for v, t in values
                       if t.get("watts") is not None]
        entry["watts_trend"] = _round(_pearson([p[0] for p in watts_pairs],
                                               [p[1] for p in watts_pairs]))
        out.append(entry)

    scored = [e for e in out if e.get("tested")]
    scored.sort(key=lambda e: (e.get("score_eta2") or 0), reverse=True)
    untested = [e for e in out if not e.get("tested")]

    by_watts = sorted(scored, key=lambda e: (e.get("watts_eta2") or 0), reverse=True)

    return {
        "trials": len(usable),
        "knobs": scored,
        "by_wattage": [e["knob"] for e in by_watts],
        "untested": untested,
        "note": ("eta^2 = share of outcome variance explained by the knob. "
                 "Few levels and few trials make it noisy; treat <0.2 as weak."),
    }


def _round(x: float | None, nd: int = 3) -> float | None:
    return round(x, nd) if x is not None else None


def baseline_from_profile(prof: dict[str, Any]) -> dict[str, int]:
    """Turn a saved profile into the baseline a grid should be built from.

    A user who already runs +150 core should sweep *around* that, not from zero.
    Anything the profile enables becomes part of the starting point.
    """
    params = dict(prof.get("params") or {})
    return {k: int(v) for k, v in params.items() if v}
