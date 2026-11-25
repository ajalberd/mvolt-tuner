"""Seed a new benchmark's study with another study's Pareto front.

Scores from one benchmark mean nothing in another, so none are carried over.
What carries over is WHICH configs were worth measuring: the clean Pareto front
(most score for the watts) of the source study, plus its reference profile.
They run through the confirm stage - each config a few times, interleaved - so
the new benchmark's own front is measured, not assumed.
"""
from __future__ import annotations

from typing import Any

from .edge import UNSTABLE
from .state import param_key


def pareto_front(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exact configs whose mean score beats every config at fewer watts.

    Only clean configs (every run of that exact config 'ok', none unstable) and
    only runs with a comparable score (odd-state runs have score None)."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for t in trials:
        groups.setdefault(param_key(t.get("params") or {}), []).append(t)
    rows = []
    for ts in groups.values():
        if any(t.get("status") in UNSTABLE for t in ts):
            continue
        ok = [t for t in ts if t.get("status") == "ok" and t.get("score")
              and t.get("watts") is not None]
        if not ok:
            continue
        rows.append({"params": dict(ok[0]["params"]), "runs": len(ok),
                     "watts": sum(float(t["watts"]) for t in ok) / len(ok),
                     "score": sum(float(t["score"]) for t in ok) / len(ok)})
    rows.sort(key=lambda r: (r["watts"], -r["score"]))
    front, best = [], float("-inf")
    for r in rows:
        if r["score"] > best:
            front.append(r)
            best = r["score"]
    return front


def candidates(trials: list[dict[str, Any]], source: str,
               reference: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Confirm-stage candidates: the reference first (the comparison point),
    then the front from fewest watts up. The source study's numbers are kept
    for information only (`src_score`, `src_watts`)."""
    out, seen = [], set()
    if reference:
        p = dict(reference["params"])
        out.append({"params": p, "cap": p.get("cap"), "rel": p.get("rel"), "core": p.get("core"),
                    "edge": None, "pick": reference.get("pick") or "reference"})
        seen.add(param_key(p))
    for r in pareto_front(trials):
        p = r["params"]
        if param_key(p) in seen:
            continue
        seen.add(param_key(p))
        out.append({"params": p, "cap": p.get("cap"), "rel": p.get("rel"), "core": p.get("core"),
                    "edge": None, "src_score": round(r["score"]), "src_watts": round(r["watts"], 1),
                    "pick": f"{source} front · {r['watts']:.0f} W"})
    return out
