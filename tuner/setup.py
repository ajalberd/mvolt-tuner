"""Interactive setup: choose which knobs to sweep and their safe ranges.

Design rules:
  * Every default is derived from what the driver reports, never invented.
  * Known-dangerous ranges (discovered experimentally) cap the suggestion.
  * Anything unavailable on this GPU is shown, greyed, and never asked about.
  * `--yes` accepts all defaults non-interactively (CI / power users).
"""
from __future__ import annotations

import json
from typing import Any

from . import config, knobs


# Suggested sweep per knob, clamped to the driver's reported bounds.
# These are *sweep* suggestions, not hardware limits.
SUGGESTIONS: dict[str, dict[str, Any]] = {
    "core":      {"min": 0,    "max": 200,  "step": 50,  "enabled": True,
                  "why": "the one lever proven to move score"},
    "mem":       {"min": 0,    "max": 400,  "step": 200, "enabled": False,
                  "why": "measured worse in testing; enable to re-check"},
    "rel":       {"min": -100, "max": 0,    "step": 25,  "enabled": False,
                  "why": "voltage ceiling; inert on a power-limited card"},
    "boost":     {"min": 0,    "max": 50,   "step": 25,  "enabled": False,
                  "why": "adds REL->ALT/OP headroom"},
    "clock_max": {"min": 0,    "max": 2100, "step": 0,   "enabled": False,
                  "why": "single value, not a sweep"},
}


def _ask(prompt: str, default: str) -> str:
    try:
        raw = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        return default
    return raw or default


def _ask_yes(prompt: str, default: bool) -> bool:
    d = "Y" if default else "n"
    raw = _ask(prompt, d).lower()
    return raw.startswith("y")


def _clamp_suggestion(key: str, st: knobs.KnobStatus) -> dict[str, Any]:
    sug = dict(SUGGESTIONS.get(key, {"min": 0, "max": 0, "step": 1,
                                     "enabled": False, "why": ""}))
    if key == "core":
        sug["max"] = min(sug["max"], config.MAX_AUTO_CORE_MHZ)
    if st.bounds:
        lo, hi = st.bounds
        sug["min"] = max(sug["min"], int(lo)) if sug["min"] >= lo else int(lo)
        sug["max"] = min(sug["max"], int(hi))
        if sug["min"] > sug["max"]:
            sug["min"], sug["max"] = int(lo), int(hi)
    return sug


def plan(status: dict[str, Any], assume_yes: bool = False,
         log=print) -> dict[str, Any]:
    """Build a sweep plan from a live --status payload."""
    rows = knobs.survey(status)
    plan_knobs: dict[str, Any] = {}

    log("")
    log("=" * 68)
    log(" Sweep setup - press Enter to accept each default")
    log("=" * 68)

    for st in rows:
        if not st.available:
            log(f"  {st.key:<16} unavailable  ({st.reason})")
            continue

        sug = _clamp_suggestion(st.key, st)
        cur = st.current if st.current is not None else "?"
        log("")
        log(f"  {st.key}  [{st.unit or 'switch'}]  current={cur}")
        if sug["why"]:
            log(f"    note: {sug['why']}")

        if st.write_only:
            log("    write-only: the driver never reports this back - "
                "results cannot be verified")
        if st.key not in SUGGESTIONS:
            log("    no sweep template for this knob - included as fixed only")

        if assume_yes:
            enabled = bool(sug["enabled"])
        else:
            enabled = _ask_yes("    include in sweep?", bool(sug["enabled"]))

        entry: dict[str, Any] = {"enabled": enabled, "unit": st.unit,
                                 "write_only": st.write_only}
        if enabled and sug["step"] > 0:
            entry["min"] = int(_ask("    min", str(sug["min"])))
            entry["max"] = int(_ask("    max", str(sug["max"])))
            entry["step"] = int(_ask("    step", str(sug["step"])))
            if entry["step"] <= 0:
                entry["step"] = 1
            if entry["max"] < entry["min"]:
                entry["min"], entry["max"] = entry["max"], entry["min"]
        plan_knobs[st.key] = entry

    payload = {
        "version": 1,
        "knobs": plan_knobs,
        "bench": {"args": config.BENCH_ARGS, "timeout_s": config.BENCH_TIMEOUT_S},
        "safety": {"max_core_mhz": config.MAX_AUTO_CORE_MHZ},
    }
    return payload


def save(plan_payload: dict[str, Any]) -> None:
    config.CONFIG_FILE.write_text(
        json.dumps(plan_payload, indent=2), encoding="utf-8")


def load() -> dict[str, Any] | None:
    if not config.CONFIG_FILE.exists():
        return None
    try:
        return json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def grid_from_plan(plan_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand a saved plan into explicit param dicts."""
    import itertools

    axes: list[tuple[str, list[int]]] = []
    for key, spec in (plan_payload.get("knobs") or {}).items():
        if not spec.get("enabled") or "step" not in spec:
            continue
        lo, hi, step = int(spec["min"]), int(spec["max"]), int(spec["step"])
        vals = list(range(lo, hi + 1, step)) if step > 0 else [lo]
        axes.append((key, vals or [lo]))

    if not axes:
        return []

    combos = []
    for values in itertools.product(*(v for _, v in axes)):
        params = {k: v for (k, _), v in zip(axes, values)}
        # rel needs its companions for the CLI form
        if "rel" in params:
            params.setdefault("mem", 0)
        combos.append(params)
    return combos
