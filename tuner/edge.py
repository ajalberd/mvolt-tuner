"""Edge search and confirmation - the refine stage, built for how GPUs behave.

Score rises with the core offset until the card becomes unstable, and where
that happens depends on voltage: MORE voltage makes a given offset LESS stable
(the offset shifts the whole V/F curve, and the top points have the least
margin). Measured on an RTX 5080: highest clean core +470 at REL -90..-160 mV,
+335 at REL +30..+100. So the best config at a given voltage and power budget
is "the highest stable core", found per LANE = one (power budget, REL) pair,
everything else held at the starting value.

How a lane is searched (and why - two hard freezes came from the old way):
  * PRIOR: a crash model fitted to every run so far (analysis.edge_model)
    predicts where the edge is. A lane starts at ~10% predicted crash chance
    instead of the bottom of the range.
  * CREEP, never jump: from the highest pass, step up at most CREEP_STEPS
    steps, also when halving the gap to a crash. Overshooting the edge by a
    lot is what turns a recoverable driver reset into a hard freeze (core +750,
    and +480 at REL +50, both froze the PC); 30 MHz past the edge just resets
    the driver.
  * SHARING between lanes, in the direction the hardware supports:
      - a pass at (core c, REL r, budget b) is a pass at c with any lower REL
        and any lower budget (less voltage/power = fewer risky V/F points);
      - a crash at (c, r, b) is a crash at c with any higher REL and budget.

CONFIRM then takes the best lane per budget, backs its edge off by a safety
margin, and re-runs it several times: mean +- spread, and any crash fails it.

All state lives in the journal, so every step is resume-safe.
"""
from __future__ import annotations

import json
import math
from typing import Any

UNSTABLE = ("crash", "driver_reset", "interrupted")
STABLE = ("ok", "over_power_limit")
COMPANIONS = {"vmin", "alt", "msvdd_vmin", "msvdd_alt"}
LANE_KEYS = ("cap", "rel", "core")
CREEP_STEPS = 2          # max jump above the highest pass, in core steps (2 x 15 = 30 MHz)
PRIOR_START_P = 0.10     # start a lane where the model predicts this crash chance
CANDIDATE_P = 0.15       # a confirm candidate never sits above this modelled crash chance
MAX_EXTRA_BACKOFF = 2    # ...but the model may pull it at most this many steps below edge - margin


def _q(v: float, step: float, origin: float) -> float:
    return origin + round((v - origin) / step) * step


def _q_down(v: float, step: float, origin: float) -> float:
    return origin + math.floor((v - origin) / step + 1e-9) * step


def make_plan(start: dict[str, Any], budgets: list[float], rels: list[float],
              core_lo: float, core_hi: float, step: float = 15.0,
              margin: float = 15.0, repeats: int = 3) -> dict[str, Any]:
    """Everything except the lane knobs is held at the starting value."""
    if step <= 0:
        raise ValueError("core step must be > 0")
    if core_hi <= core_lo:
        raise ValueError("core range: 'to' must be above 'from'")
    if not budgets or not rels:
        raise ValueError("need at least one power budget and one undervolt level")
    from . import knobs
    base = {k: v for k, v in start.items() if k not in LANE_KEYS}
    core_hi = core_lo + math.floor((core_hi - core_lo) / step) * step
    return {"base": base, "link": knobs.link_reference(start),
            "budgets": sorted({float(b) for b in budgets}, reverse=True),
            "rels": sorted({float(r) for r in rels}, reverse=True),
            "core": {"lo": float(core_lo), "hi": float(core_hi), "step": float(step)},
            "margin": float(margin), "repeats": int(repeats),
            "start": {k: float(start[k]) for k in ("core", "rel") if k in start}}


def suggest_plan(status: dict[str, Any], start: dict[str, Any],
                 limits: dict[str, Any] | None,
                 trials: list[dict[str, Any]] | None = None,
                 fixed_rels: list[float] | None = None) -> dict[str, Any]:
    """Defaults for the page to show and edit, informed by the runs so far.
    `fixed_rels` replaces the suggested REL levels (e.g. [0] when the driver
    ignores core voltage offsets).

    Budgets: three, from your watt limit down to ~70% of it (card minimum).
    Voltage: undervolt is where both efficiency and core headroom are, so the
    levels go from 25 mV above the starting REL down to 90 mV below it,
    within your voltage limit and the explored range.
    Core: from the crash model's ~5% point at the highest REL level to its ~90%
    point at the lowest, so the search never starts far from the edge and
    never has room to overshoot it badly. Without enough data: start -100/+100.
    """
    from . import analysis, knobs, search

    def r5(v: float) -> float:
        return float(round(v / 5.0) * 5)

    pw = knobs.power_info(status)
    top = float((limits or {}).get("max_watts") or pw.get("max_w") or pw.get("default_w") or 300.0)
    low = max(float(pw.get("min_w") or top * 0.7), top * 0.7)
    budgets = sorted({r5(top), r5((top + low) / 2), r5(low)}, reverse=True)

    rel0 = float(start.get("rel", 0) or 0)
    room = search._voltage_room(status, limits).get("rel")
    hi_rel = min(room, rel0 + 25.0) if room is not None else rel0 + 25.0
    explored = [float(t["params"]["rel"]) for t in (trials or [])
                if "rel" in (t.get("params") or {}) and t.get("status") in STABLE]
    lo_rel = max(rel0 - 90.0, min(explored) if explored else rel0 - 90.0)
    lo_rel = min(lo_rel, hi_rel)
    rels = sorted({r5(hi_rel - i * (hi_rel - lo_rel) / 3) for i in range(4)}, reverse=True)
    if fixed_rels:
        rels = sorted({float(r) for r in fixed_rels}, reverse=True)

    core0 = float(start.get("core", 0) or 0)
    cb = next((r.bounds for r in knobs.survey(status) if r.key == "core"), None)
    model = analysis.edge_model(trials or [])
    if model:
        lo = min(model["core_at"](0.05, max(rels), b) for b in budgets)
        hi = max(model["core_at"](0.90, min(rels), b) for b in budgets)
    else:
        lo, hi = core0 - 100.0, core0 + 100.0
    lo, hi = _q_down(lo, 15.0, 0.0), _q(hi, 15.0, 0.0)
    if cb:
        lo, hi = max(lo, cb[0]), min(hi, cb[1])
    return make_plan(start, budgets, rels, lo, hi)


def point(plan: dict[str, Any], cap: float, rel: float, core: float) -> dict[str, Any]:
    """One config: the held values + this lane, with ALT moved together with REL."""
    from . import knobs
    p = dict(plan["base"])
    p.update({"cap": cap, "rel": rel, "core": core})
    return knobs.link_rails(p, plan.get("link") or {})


def _matches_base(params: dict[str, Any], base: dict[str, Any]) -> bool:
    for k, v in base.items():
        if k in COMPANIONS:
            continue
        pv = params.get(k)
        if pv is None or (isinstance(v, (int, float)) and not isinstance(v, bool)
                          and abs(float(pv) - float(v)) > 1e-6) or \
                (not isinstance(v, (int, float)) and pv != v):
            return False
    extra = set(params) - set(base) - set(LANE_KEYS) - COMPANIONS
    return not extra


def lane_observations(trials: list[dict[str, Any]], plan: dict[str, Any]
                      ) -> dict[tuple[float, float], dict[str, list[dict[str, Any]]]]:
    """Journal results that belong to a lane: (cap, rel) -> {"pass": [...], "fail": [...]}."""
    obs: dict[tuple[float, float], dict[str, list[dict[str, Any]]]] = {}
    for t in trials:
        p = t.get("params") or {}
        # No REL written means REL 0 (stock), as everywhere else (crash guard,
        # aggression): runs made while the driver ignored rail offsets count.
        if "rel" not in p and "core" in p and "cap" in p:
            p = dict(p, rel=0.0)
        if not all(k in p for k in LANE_KEYS) or not _matches_base(p, plan["base"]):
            continue
        st = t.get("status")
        if st not in STABLE + UNSTABLE:
            continue
        lane = (float(p["cap"]), float(p["rel"]))
        o = obs.setdefault(lane, {"pass": [], "fail": []})
        o["pass" if st in STABLE else "fail"].append(t)
    return obs


def lane_state(obs, plan: dict[str, Any], cap: float, rel: float,
               model: dict[str, Any] | None = None) -> dict[str, Any]:
    """Bounds on the stable core for one lane, and the next probe."""
    c = plan["core"]
    step = c["step"]
    creep = CREEP_STEPS * step
    passed, failed = [], []
    for (ocap, orel), o in obs.items():
        # pass with more (or equal) voltage AND budget -> pass here
        if orel >= rel - 1e-9 and ocap >= cap - 1e-6:
            passed += [float(t["params"]["core"]) for t in o["pass"]]
        # crash with less (or equal) voltage -> crash here, at ANY budget. On
        # the RTX 5080 these crashes hit seconds into the load (69-163 W
        # average), long before a power cap binds, so the budget does not move
        # the edge; treating a crash at 395 W as news-free for 335 W re-probed
        # the same core three times and the third ended in a hard freeze.
        if orel <= rel + 1e-9:
            failed += [float(t["params"]["core"]) for t in o["fail"]]
    lo = max(passed) if passed else None
    hi = min(failed) if failed else None
    if lo is not None and hi is not None and lo >= hi:
        # Contradiction (a marginal config passed once, failed once): trust the crash.
        lo = max((x for x in passed if x < hi), default=None)
    own = obs.get((cap, rel), {"pass": [], "fail": []})
    best = max(own["pass"], key=lambda t: float(t["params"]["core"]), default=None)
    prior = None
    if model:
        prior = {"p10": round(model["core_at"](0.10, rel, cap)),
                 "p50": round(model["core_at"](0.50, rel, cap)),
                 "p90": round(model["core_at"](0.90, rel, cap))}
    st: dict[str, Any] = {"cap": cap, "rel": rel, "lo": lo, "hi": hi, "done": False,
                          "next": None, "runs": len(own["pass"]) + len(own["fail"]),
                          "best_score": best.get("score") if best else None, "prior": prior}
    if hi is not None and hi <= c["lo"] and (lo is None or lo < c["lo"]):
        st.update(done=True, edge=None, note=f"unstable even at core {c['lo']:g}")
    elif lo is None:
        # No pass yet: start where the model says ~10% crash chance (or the
        # bottom of the range), and walk DOWN by creep steps after a crash.
        start = _q_down(model["core_at"](PRIOR_START_P, rel, cap), step, c["lo"]) \
            if model else c["lo"]
        start = min(max(start, c["lo"]), c["hi"])
        if hi is not None:
            start = min(start, max(c["lo"], hi - creep))
        st["next"] = start
    elif lo >= c["hi"]:
        st.update(done=True, edge=lo, note=f"stable up to the top of the range ({lo:g})")
    elif hi is not None and hi - lo <= step + 1e-9:
        st.update(done=True, edge=lo, note=f"edge between {lo:g} and {hi:g}")
    else:
        if hi is None:
            nxt = lo + creep                       # creep up, never jump to the top
        else:
            nxt = min(_q((lo + hi) / 2, step, c["lo"]), lo + creep)
            if nxt >= hi:
                nxt = hi - step
        st["next"] = min(max(nxt, lo + step), c["hi"])
    return st


def lanes(trials: list[dict[str, Any]], plan: dict[str, Any],
          model: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """State of every lane. The prior model is fitted to ALL runs (explore
    included), not just the lane's own, so it starts informed."""
    from . import analysis
    if model is None:
        model = analysis.edge_model(trials)
    obs = lane_observations(trials, plan)
    return [lane_state(obs, plan, cap, rel, model or None)
            for cap in plan["budgets"] for rel in plan["rels"]]


def next_point(trials: list[dict[str, Any]], plan: dict[str, Any],
               exclude: set[str] | None = None,
               model: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The next config to test, or None when every lane has found its edge.

    Highest-voltage, highest-budget lanes go first: a pass there carries over
    to every lower lane, so they finish fastest. Lanes whose next probe is
    excluded (e.g. by the hard-crash guard) are skipped."""
    from .state import param_key
    todo = [s for s in lanes(trials, plan, model) if not s["done"] and s["next"] is not None]
    for s in sorted(todo, key=lambda s: (-s["rel"], -s["cap"])):
        p = point(plan, s["cap"], s["rel"], s["next"])
        if exclude is None or param_key(p) not in exclude:
            return p
    return None


def estimate_runs(plan: dict[str, Any], trials: list[dict[str, Any]] | None = None) -> int:
    """Runs the search would take if the model's 50% points were the true edges
    (or, without a model, the middle of the range). Lane sharing included."""
    from . import analysis
    model = analysis.edge_model(trials or [])

    def crashes(p: dict[str, Any]) -> bool:
        if model:
            return model["p_crash"](p["core"], p["rel"], p["cap"]) >= 0.5
        return p["core"] > (plan["core"]["lo"] + plan["core"]["hi"]) / 2

    synth: list[dict[str, Any]] = []
    for _ in range(400):
        p = next_point(synth, plan, model=model or {})
        if p is None:
            break
        synth.append({"params": p, "status": "crash" if crashes(p) else "ok", "score": 1})
    return len(synth)


# ---------------------------------------------------------------------------
# confirm
# ---------------------------------------------------------------------------

def candidates(trials: list[dict[str, Any]], plan: dict[str, Any],
               reference: dict[str, Any] | None = None,
               reference_name: str = "") -> list[dict[str, Any]]:
    """Points to confirm, each backed off from its lane's edge by the margin:

      efficiency  per budget, the lane that holds the most core (usually the
                  deepest undervolt: same score per MHz, fewer watts). Skipped
                  when it is the same point as a lower budget's and the card
                  never wanted much more than that lower cap at this REL.
      performance at the top budget, the lane whose own runs scored best
                  (less undervolt draws more watts, which buys score).
    """
    from .analysis import edge_model
    model = edge_model(trials)
    states = lanes(trials, plan, model)
    obs = lane_observations(trials, plan)
    step = float(plan["core"]["step"])
    lo = plan["core"]["lo"]
    found = []
    for s in states:
        if s.get("edge") is None:
            continue
        core = s["edge"] - plan["margin"]
        # One lucky pass can put a lane's edge above where the card really is
        # (the edge is a probability, not a line). The model, refitted on every
        # run including this search's, pulls such a candidate back to where it
        # predicts <= CANDIDATE_P crash chance - by a bounded amount.
        if model is not None:
            safe = _q_down(model["core_at"](CANDIDATE_P, s["rel"], s["cap"]), step, lo)
            core = max(min(core, safe), core - MAX_EXTRA_BACKOFF * step)
        core = max(lo, core)
        own = [t for t in obs.get((s["cap"], s["rel"]), {"pass": []})["pass"] if t.get("score")]
        # Watts this REL drew at any budget: tells whether a cap binds.
        watts = [float(t["watts"]) for (oc, orl), o in obs.items() if abs(orl - s["rel"]) < 1e-9
                 for t in o["pass"] if t.get("watts")]
        near = min(own, key=lambda t: abs(float(t["params"]["core"]) - core), default=None)
        found.append({"cap": s["cap"], "rel": s["rel"], "edge": s["edge"], "core": core,
                      "score_hint": float(near["score"]) if near else None,
                      "own_best": max((float(t["score"]) for t in own), default=None),
                      "max_watts": max(watts, default=None),
                      "params": point(plan, s["cap"], s["rel"], core)})
    out: list[dict[str, Any]] = []

    def same(a, b):
        """Same point, or only the cap differs and the card drew clearly
        less than the lower cap at this REL (max draw seen at any budget), so
        the higher cap changes nothing."""
        if a["rel"] != b["rel"] or a["core"] != b["core"]:
            return False
        w = a.get("max_watts") or b.get("max_watts")
        # Average draw sits well below a cap that binds (335 W cap -> ~300 W
        # average on the 5080), so only call two caps equal when the card drew
        # clearly less than the lower one even without it.
        return a["cap"] == b["cap"] or (w is not None and w < min(a["cap"], b["cap"]) - 40)

    def add(c, why):
        if not any(same(o, c) for o in out):
            out.append(dict(c, pick=why))

    for cap in sorted(plan["budgets"]):
        mine = [c for c in found if c["cap"] == cap]
        if mine:
            # most core; tie -> deeper undervolt (fewer watts)
            add(max(mine, key=lambda c: (c["core"], -c["rel"])), "efficiency")
    cap_top = max(plan["budgets"])
    top = [c for c in found if c["cap"] == cap_top and c["own_best"] is not None]
    if top:
        add(max(top, key=lambda c: c["own_best"]), "performance")
    # The starting point (e.g. your saved profile) at the top budget, measured
    # the same way, so "better than what I had" is a measurement.
    ref = reference or plan.get("start") or {}
    if "core" in ref and "rel" in ref:
        rc, rr = float(ref["core"]), float(ref["rel"])
        out.append({"cap": cap_top, "rel": rr, "edge": None, "core": rc, "score_hint": None,
                    "max_watts": None, "params": point(plan, cap_top, rr, rc),
                    "pick": "reference" + (f" ({reference_name})" if reference_name else "")})
    order = {b: i for i, b in enumerate(plan["budgets"])}
    kind = {"efficiency": 0, "performance": 1}
    out.sort(key=lambda c: (order.get(c["cap"], 99), kind.get(c["pick"], 2)))
    for c in out:
        c.pop("own_best", None)
    return out


def confirm_points(cands: list[dict[str, Any]], repeats: int) -> list[dict[str, Any]]:
    """Each candidate `repeats` times, interleaved (A B C A B C ...) so slow
    thermal drift does not favour whichever candidate ran first."""
    return [dict(c["params"]) for _ in range(repeats) for c in cands]


def confirm_results(trials: list[dict[str, Any]], cands: list[dict[str, Any]]
                    ) -> list[dict[str, Any]]:
    """Every run of each candidate's exact config counts, including ones the edge
    search made - they are the same measurement. The run loop does the same, so
    'N runs per config' means N in total."""
    from .state import param_key
    out = []
    for c in cands:
        key = param_key(c["params"])
        runs = [t for t in trials if param_key(t.get("params") or {}) == key
                and t.get("status") in STABLE + UNSTABLE]
        scores = [float(t["score"]) for t in runs if t.get("status") == "ok" and t.get("score")]
        watts = [float(t["watts"]) for t in runs if t.get("status") == "ok"
                 and t.get("watts") is not None]
        crashes = sum(t["status"] in UNSTABLE for t in runs)
        n = len(scores)
        mean = sum(scores) / n if n else None
        sd = (sum((s - mean) ** 2 for s in scores) / (n - 1)) ** 0.5 if n > 1 else None
        out.append({**c, "runs": len(runs), "crashes": crashes, "score_mean": mean,
                    "score_sd": sd, "watts_mean": sum(watts) / len(watts) if watts else None,
                    "verdict": ("failed - crashed in confirmation; back off further"
                                if crashes else "confirmed" if n >= 2 else "pending")})
    return out
