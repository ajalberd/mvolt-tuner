"""Optuna knob-importance report built from the trial journal.

The journal is replayed into in-memory Optuna studies - one per question - and
Optuna's importance evaluators answer "which knobs moved this outcome?":

    score        higher is better        (scored, stable trials)
    watts        average board power     (trials with a power reading)
    efficiency   score per watt          (both)
    instability  1 = crash / driver reset / host reboot, 0 = clean
                 (every finished trial - which knobs make the card fall over)

Evaluators:
  * fANOVA (Optuna's default): fits a random forest and splits the variance of
    the outcome by knob. Works from a handful of trials, noisy below ~20.
  * PED-ANOVA: compares the best quantile of trials against all trials. Needs
    more trials; reported only when there are enough.

Importances are shares that sum to 1 within a target - they say how much a knob
matters, not which direction is good. The direction comes from the per-knob
trend (Pearson r against the outcome), reported next to it.

Nothing here touches the GPU. Optuna is optional: without it the report says so
and the eta^2 analysis in importance.py still works.
"""
from __future__ import annotations

import json
import math
from typing import Any

from .importance import _pearson

_COMPANIONS = {"vmin", "alt", "msvdd_vmin", "msvdd_alt"}
# A knob absent from a trial was left at its stock value. For most knobs that is
# 0; these are not offsets, so 0 would be a wrong (and extreme) value.
_STOCK = {"power": 100.0, "boost_lock": False}
UNSTABLE = ("crash", "driver_reset", "interrupted")
# Completed runs over the watt limit are still real measurements of score/watts.
MEASURED = ("ok", "over_power_limit")
FINISHED = MEASURED + UNSTABLE
MIN_TRIALS = 4
MIN_TRIALS_PEDANOVA = 20


def _available() -> tuple[bool, str]:
    try:
        import optuna  # noqa: F401
        return True, optuna.__version__
    except ImportError:
        return False, ""


def _value(params: dict[str, Any], key: str, fill: dict[str, Any]) -> Any:
    v = params.get(key, fill.get(key, _STOCK.get(key, 0)))
    if isinstance(v, bool) or key == "boost_lock":
        return bool(v)
    return float(v)


def _design(trials: list[dict[str, Any]], only: set[str] | None
            ) -> tuple[list[str], dict[str, Any], list[str]]:
    """Knobs to model, their fill-in stock values, and knobs never varied."""
    keys: set[str] = set()
    for t in trials:
        keys |= {k for k in (t.get("params") or {}) if k not in _COMPANIONS}
    if only:
        keys &= set(only)
    # An absent cap means "the card's own limit", i.e. at least the highest cap tried.
    caps = [float(t["params"]["cap"]) for t in trials if "cap" in (t.get("params") or {})]
    fill = {"cap": max(caps)} if caps else {}
    varied, flat = [], []
    for k in sorted(keys):
        vals = {_value(t.get("params") or {}, k, fill) for t in trials}
        (varied if len(vals) > 1 else flat).append(k)
    return varied, fill, flat


def _distribution(key: str, values: list[Any]):
    from optuna.distributions import CategoricalDistribution, FloatDistribution
    if key == "boost_lock" or all(isinstance(v, bool) for v in values):
        return CategoricalDistribution([False, True])
    return FloatDistribution(min(values), max(values))


def _study_for(rows: list[tuple[dict[str, Any], float]], knobs: list[str],
               fill: dict[str, Any], direction: str):
    import optuna
    from optuna.trial import create_trial

    columns = {k: [_value(p, k, fill) for p, _ in rows] for k in knobs}
    dists = {k: _distribution(k, columns[k]) for k in knobs}
    study = optuna.create_study(direction=direction)
    for i, (_p, value) in enumerate(rows):
        study.add_trial(create_trial(
            params={k: columns[k][i] for k in knobs},
            distributions=dists,
            value=float(value),
        ))
    return study


def _importances(study, knobs: list[str], n: int) -> dict[str, Any]:
    import warnings

    from optuna.importance import (FanovaImportanceEvaluator, PedAnovaImportanceEvaluator,
                                   get_param_importances)
    out: dict[str, Any] = {}
    try:
        # fANOVA needs scikit-learn (pip install scikit-learn).
        fan = get_param_importances(study, evaluator=FanovaImportanceEvaluator(seed=0),
                                    params=knobs)
        out["fanova"] = {k: round(float(v), 4) for k, v in fan.items()}
    except Exception as exc:  # missing sklearn, zero variance in the outcome, ...
        out["fanova"] = None
        out["fanova_error"] = str(exc)[:200]
    out["pedanova"] = None
    if n >= MIN_TRIALS_PEDANOVA or out["fanova"] is None:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # ExperimentalWarning
                ped = get_param_importances(study, evaluator=PedAnovaImportanceEvaluator(),
                                            params=knobs)
            out["pedanova"] = {k: round(float(v), 4) for k, v in ped.items()}
        except Exception as exc:
            out["pedanova_error"] = str(exc)[:200]
    return out


def _target(name: str, rows: list[tuple[dict[str, Any], float]], knobs: list[str],
            fill: dict[str, Any], direction: str, unit: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"target": name, "direction": direction, "unit": unit,
                             "n": len(rows), "knobs": []}
    if len(rows) < MIN_TRIALS:
        entry["note"] = f"needs at least {MIN_TRIALS} trials (have {len(rows)})"
        return entry
    values = [v for _, v in rows]
    if max(values) - min(values) <= 1e-12:
        entry["note"] = "the outcome never changed - nothing to attribute"
        return entry
    usable = [k for k in knobs
              if len({_value(p, k, fill) for p, _ in rows}) > 1]
    if not usable:
        entry["note"] = "no knob varied within these trials"
        return entry

    study = _study_for(rows, usable, fill, direction)
    imp = _importances(study, usable, len(rows))
    entry.update({k: v for k, v in imp.items() if k.endswith("_error")})
    fan, ped = imp.get("fanova") or {}, imp.get("pedanova") or {}
    # The headline number comes from fANOVA; PED-ANOVA stands in when fANOVA
    # could not run. If neither ran, say so instead of showing an unranked list.
    primary, entry["evaluator"] = (fan, "fanova") if fan else (ped, "pedanova") if ped \
        else ({}, None)
    if not primary:
        entry["note"] = "importance could not be computed: " + (
            imp.get("fanova_error") or imp.get("pedanova_error") or "unknown error")
        return entry
    for k in usable:
        xs = [float(_value(p, k, fill)) for p, _ in rows]
        r = _pearson(xs, [float(v) for v in values])
        entry["knobs"].append({
            "knob": k,
            "importance": primary.get(k),
            "pedanova": ped.get(k) if ped else None,
            "trend_r": None if r is None or math.isnan(r) else round(r, 3),
            "levels": len(set(xs)),
        })
    entry["knobs"].sort(key=lambda e: (e["importance"] or 0), reverse=True)
    if len(rows) < 20:
        entry["note"] = (f"{len(rows)} trials: fANOVA is indicative only; "
                         "rankings settle after ~20+ trials with several knobs varied")
    if len(usable) == 1:
        entry["note"] = ("only one knob varied, so it trivially explains 100% - "
                         "vary two or more knobs for a meaningful ranking")
    return entry


def stability_edge(trials: list[dict[str, Any]], axis: str = "core") -> list[dict[str, Any]]:
    """How far `axis` (the core offset) went at each voltage / other setting.

    The stable core offset depends on the voltage (an undervolt lowers it), so
    there is no single "max core": for every combination of the OTHER knobs this
    lists the highest offset that ran clean and the lowest that crashed. The
    true edge lies between them. Needs no Optuna.
    """
    groups: dict[str, dict[str, Any]] = {}
    for t in trials:
        p = t.get("params") or {}
        if axis not in p or t.get("status") not in FINISHED:
            continue
        rest = {k: v for k, v in p.items() if k != axis}
        key = json.dumps(rest, sort_keys=True)
        g = groups.setdefault(key, {"setting": rest, "highest_stable": None,
                                    "lowest_failed": None, "trials": 0})
        g["trials"] += 1
        v = float(p[axis])
        if t["status"] in UNSTABLE:
            if g["lowest_failed"] is None or v < g["lowest_failed"]:
                g["lowest_failed"] = v
        elif g["highest_stable"] is None or v > g["highest_stable"]:
            g["highest_stable"] = v
    out = []
    for g in groups.values():
        hs, lf = g["highest_stable"], g["lowest_failed"]
        if hs is not None and lf is not None:
            g["edge"] = f"between {hs:g} and {lf:g}" if hs < lf else \
                f"inconsistent: {hs:g} passed but {lf:g} failed (noise or thermal drift)"
        elif lf is None:
            g["edge"] = f"at least {hs:g} (nothing failed yet)"
        else:
            g["edge"] = f"below {lf:g} (nothing passed yet)"
        out.append(g)
    return sorted(out, key=lambda g: json.dumps(g["setting"], sort_keys=True))


def report(trials: list[dict[str, Any]], only: set[str] | None = None) -> dict[str, Any]:
    """Optuna importance for score, watts, efficiency and instability.

    `trials` items: {"params": {...}, "score", "watts", "status"} (Journal.scored_trials()).
    `only`: restrict to the knobs the study deliberately swept.
    """
    ok, version = _available()
    if not ok:
        return {"available": False, "stability_edge": stability_edge(trials),
                "note": "Optuna is not installed: pip install optuna  (eta^2 below still works)"}

    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    finished = [t for t in trials if t.get("status") in FINISHED]
    knobs, fill, flat = _design(finished, only)

    stable = [t for t in finished if t.get("status") in MEASURED]
    score_rows = [(t["params"] or {}, float(t["score"])) for t in stable if t.get("score")]
    watt_rows = [(t["params"] or {}, float(t["watts"])) for t in stable
                 if t.get("watts") is not None]
    eff_rows = [(t["params"] or {}, float(t["score"]) / float(t["watts"])) for t in stable
                if t.get("score") and t.get("watts")]
    crash_rows = [(t["params"] or {}, 1.0 if t["status"] in UNSTABLE else 0.0)
                  for t in finished]

    targets = {
        "score": _target("score", score_rows, knobs, fill, "maximize", "pts"),
        "watts": _target("watts", watt_rows, knobs, fill, "minimize", "W"),
        "efficiency": _target("efficiency", eff_rows, knobs, fill, "maximize", "pts/W"),
        "instability": _target("instability", crash_rows, knobs, fill, "minimize",
                               "P(crash)"),
    }
    if not any(v for _, v in crash_rows):
        targets["instability"]["note"] = "no crashes or driver resets yet"
        targets["instability"]["knobs"] = []

    used = {t.get("evaluator") for t in targets.values() if t.get("evaluator")}
    engine = " + ".join(e for e in ("fanova", "pedanova") if e in used) or "no evaluator ran"
    try:
        import sklearn  # noqa: F401
        missing = ""
    except ImportError:
        missing = " (fANOVA needs scikit-learn: pip install scikit-learn)"
    return {
        "available": True,
        "stability_edge": stability_edge(trials),
        "engine": f"optuna {version}: {engine.replace('fanova', 'fANOVA').replace('pedanova', 'PED-ANOVA')}"
                  + missing,
        "trials": len(finished),
        "targets": targets,
        "not_varied": flat,
        "note": ("Importance = share of the outcome's variance a knob explains "
                 "(sums to 1 per target). trend_r gives the direction: positive "
                 "means raising the knob raises the outcome."),
    }


def render(rep: dict[str, Any]) -> str:
    """Plain-text table for the CLI."""
    if not rep.get("available"):
        return rep.get("note", "Optuna unavailable")
    lines = [f"{rep['engine']}  -  {rep['trials']} finished trials", ""]
    for name, t in rep["targets"].items():
        arrow = "higher is better" if t["direction"] == "maximize" else "lower is better"
        lines.append(f"{name.upper()} ({t['unit']}, {arrow}, n={t['n']})")
        if not t["knobs"]:
            lines.append(f"  - {t.get('note', 'no data')}")
        for k in t["knobs"]:
            imp = k["importance"]
            bar = "#" * int(round((imp or 0) * 30))
            r = k["trend_r"]
            # A direction is only worth stating for a knob that matters; a
            # correlation on a 0.00-importance knob is incidental.
            trend = "" if r is None or (imp or 0) < 0.05 else (
                "  raises it" if r > 0.2 else "  lowers it" if r < -0.2 else "  no clear trend")
            ped = f"  ped={k['pedanova']:.2f}" if k.get("pedanova") is not None else ""
            lines.append(f"  {k['knob']:<18} {('%.2f' % imp) if imp is not None else '  - '} "
                         f"{bar:<30}{ped}  r={r if r is not None else '-'}{trend}")
        if t["knobs"] and t.get("note"):
            lines.append(f"  note: {t['note']}")
        lines.append("")
    edges = [e for e in rep.get("stability_edge") or [] if e["lowest_failed"] is not None]
    if edges:
        lines.append("STABILITY EDGE - core offset per voltage/other setting")
        for e in edges:
            s = ", ".join(f"{k}={v}" for k, v in e["setting"].items()
                          if k not in _COMPANIONS or v) or "stock"
            lines.append(f"  {s:<40} core {e['edge']}  ({e['trials']} trials)")
        lines.append("")
    if rep.get("not_varied"):
        lines.append("not varied (cannot be scored): " + ", ".join(rep["not_varied"]))
    lines.append(rep["note"])
    return "\n".join(lines)
