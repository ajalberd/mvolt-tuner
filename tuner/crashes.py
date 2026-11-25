"""Crash log and hard-crash guard.

Not every failure is the same, and the difference matters for an automated run:

  hard freeze on apply   the PC went down while settings were being applied
                         (journal row left in 'applying', next start sees a new
                         boot). Black screen, fans at max, manual reset. On an
                         RTX 5080 this came from fabric clocks (XBAR/SYS) set
                         too high: they run all the time, idle included.
  hard freeze under load the PC went down during the benchmark ('running').
  driver reset           the driver crashed and recovered; the run continued.
  benchmark crash        the benchmark died without a driver reset.

For each hard freeze, the likely culprits are the settings that went BEYOND the
most aggressive value that has run clean (e.g. XBAR +700 when the highest clean
XBAR is +550). The guard then skips any planned config that is at or beyond all
of a freeze's culprit values - a much narrower net than the general "at least as
aggressive in every setting" rule, which XBAR +700 alone would never trigger.
"""
from __future__ import annotations

import re
from typing import Any

from .knobs import AGGRESSION

CLEAN = ("ok", "over_power_limit")
COMPANIONS = {"vmin", "alt", "msvdd_vmin", "msvdd_alt"}

KIND_LABEL = {
    "hard_apply": "hard freeze on apply",
    "hard_load": "hard freeze under load",
    "hard": "hard freeze",
    "driver_reset": "driver reset (recovered)",
    "crash": "benchmark crash",
}


def classify(t: dict[str, Any], current_boot: str | None = None) -> str | None:
    """Kind of failure for a journal row, or None if it did not fail.

    With `current_boot`, a row still in 'applying'/'running' from an earlier
    boot counts as a hard freeze right away, before the next run reclaims it."""
    st, note = t.get("status"), t.get("note") or ""
    if st in ("applying", "running") and current_boot and t.get("boot_id") \
            and t["boot_id"] != current_boot and current_boot != "unknown":
        return "hard_apply" if st == "applying" else "hard_load"
    if st == "interrupted":
        if "host reboot" not in note and "process death" in note:
            return None
        if "'applying'" in note:
            return "hard_apply"
        if "'running'" in note:
            return "hard_load"
        return "hard"
    if st in ("driver_reset", "crash"):
        return st
    return None


def clean_extremes(trials: list[dict[str, Any]]) -> dict[str, float]:
    """Per setting: the most aggressive value that has run clean."""
    out: dict[str, float] = {}
    for t in trials:
        if t.get("status") not in CLEAN:
            continue
        for k, v in (t.get("params") or {}).items():
            d = AGGRESSION.get(k)
            if d is None or k in COMPANIONS or isinstance(v, bool):
                continue
            v = float(v)
            if k not in out or (v - out[k]) * d > 0:
                out[k] = v
    return out


def clean_core_at_voltage(trials: list[dict[str, Any]], params: dict[str, Any]) -> float | None:
    """Highest core that ran clean with at least `params`' REL (more voltage is
    riskier on this card, so such a run says the same core is safe here)."""
    if "rel" not in params:
        return None
    rel = float(params["rel"])
    vals = [float(t["params"]["core"]) for t in trials
            if t.get("status") in CLEAN and "core" in (t.get("params") or {})
            and float(t["params"].get("rel", 0) or 0) >= rel - 1e-9]
    return max(vals) if vals else None


def implicated(params: dict[str, Any], extremes: dict[str, float]) -> dict[str, dict[str, Any]]:
    """Settings in a crashed config that went past anything that ran clean."""
    out: dict[str, dict[str, Any]] = {}
    for k, v in params.items():
        d = AGGRESSION.get(k)
        if d is None or k in COMPANIONS or isinstance(v, bool):
            continue
        v = float(v)
        clean = extremes.get(k)
        if clean is None or (v - clean) * d > 0:
            out[k] = {"value": v, "highest_clean": clean}
    return out


def culprits(trials: list[dict[str, Any]], params: dict[str, Any],
             ext: dict[str, float]) -> dict[str, dict[str, Any]]:
    """implicated(), plus core judged against clean runs at this voltage or more:
    +465 is nothing new across all runs (+470 ran clean at a deep undervolt),
    but it IS past anything that ran clean with this much voltage."""
    cul = implicated(params, ext)
    core_ext = clean_core_at_voltage(trials, params)
    if "core" in params and "core" not in cul and core_ext is not None             and float(params["core"]) > core_ext:
        cul["core"] = {"value": float(params["core"]), "highest_clean": core_ext}
    return cul


def _driver_errors(note: str) -> int | None:
    m = re.search(r"(\d+) NVIDIA driver error", note)
    return int(m.group(1)) if m else None


def log(trials: list[dict[str, Any]], current_boot: str | None = None) -> dict[str, Any]:
    """Every failure, newest first, with culprits for the hard freezes."""
    ext = clean_extremes(trials)
    entries, counts = [], {}
    for t in trials:
        kind = classify(t, current_boot)
        if not kind:
            continue
        counts[kind] = counts.get(kind, 0) + 1
        e = {"id": t.get("id"), "kind": kind, "label": KIND_LABEL[kind],
             "hard": kind.startswith("hard"), "params": t.get("params") or {},
             "when": t.get("started_utc"), "note": t.get("note"),
             "driver_errors": _driver_errors(t.get("note") or "")}
        if e["hard"]:
            e["culprits"] = culprits(trials, e["params"], ext)
        entries.append(e)
    entries.reverse()
    return {"entries": entries, "counts": counts, "highest_clean": ext,
            "rules": rules(trials, current_boot)}


def rules(trials: list[dict[str, Any]], current_boot: str | None = None) -> list[dict[str, Any]]:
    """One guard rule per hard freeze: its culprit settings and values."""
    ext = clean_extremes(trials)
    out = []
    for t in trials:
        kind = classify(t, current_boot)
        if not kind or not kind.startswith("hard"):
            continue
        params = t.get("params") or {}
        cul = culprits(trials, params, ext)
        if cul:
            rule = {k: c["value"] for k, c in cul.items()}
            # How much core is safe depends strongly on voltage (a deeper
            # undervolt holds a higher offset), so a core culprit only blocks
            # configs with at least the frozen config's voltage. The budget does
            # not move the core edge (crashes hit before a power cap binds), so
            # it is left out. Fabric clocks freeze at idle, where neither matters.
            if "core" in rule:
                rule.pop("cap", None)
                if "rel" in params and "rel" not in rule:
                    rule["rel"] = float(params["rel"])
            out.append({"trial": t.get("id"), "kind": kind, "at_or_beyond": rule})
    return out


def guard(params: dict[str, Any], guard_rules: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The rule that blocks `params`: every culprit setting at or beyond the
    value that froze the GPU. Missing settings count as 0 (stock)."""
    for r in guard_rules:
        hit = True
        for k, v in r["at_or_beyond"].items():
            d = AGGRESSION.get(k, 1)
            pv = float(params.get(k, 0) or 0)
            if (pv - v) * d < -1e-9:
                hit = False
                break
        if hit:
            return r
    return None


def describe_rule(r: dict[str, Any]) -> str:
    parts = []
    for k, v in r["at_or_beyond"].items():
        d = AGGRESSION.get(k, 1)
        parts.append(f"{k} {'>=' if d > 0 else '<='} {v:+g}")
    return f"{' and '.join(parts)} froze the GPU (trial {r['trial']})"
