"""Interactive setup: your limits, then which knobs to sweep and over what range.

Design rules:
  * The ONLY ceilings are the ones you set: a maximum board power (W) and a
    maximum core voltage (mV). Offsets have no tuner-imposed caps - how far a
    clock offset can go depends on the voltage, so a fixed cap is meaningless.
  * Values you type are clamped only to the range the driver itself reports.
  * Every default is derived from what the driver reports, never invented.
  * Anything unavailable on this GPU is shown, greyed, and never asked about.
  * `--yes` accepts all defaults without a single prompt (no limits).
  * A knob is either swept (min/max/step) or held at one fixed value.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from . import config, knobs

# Suggested sweep per knob, clamped to the driver's reported bounds.
# These are starting ranges, not limits - type anything the driver accepts.
SUGGESTIONS: dict[str, dict[str, Any]] = {
    "core":      {"min": 0,    "max": 200,  "step": 50,  "enabled": True,
                  "why": "main performance lever; its stable range depends on voltage"},
    "mem":       {"min": 0,    "max": 1000, "step": 500, "enabled": False,
                  "why": "memory clock; helps memory-bound loads"},
    "rel":       {"min": -100, "max": 0,    "step": 25,  "enabled": False,
                  "why": "voltage limit offset; negative = undervolt (fewer watts)"},
    "boost":     {"min": 0,    "max": 50,   "step": 25,  "enabled": False,
                  "why": "adds REL->ALT/OP headroom"},
}

# Knobs that take one value for the whole study rather than a sweep.
FIXED_ONLY = {"clock_max", "boost_lock"}


LIMIT_KEYS = ("max_watts", "max_core_mv", "max_fabric_mv")


def limit_specs(status: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Each limit with the card's stock value and the highest value it can be set to.

    Limits may go ABOVE stock: a watt limit above the default raises the card's
    power limit (up to its maximum); a voltage limit above stock lets the sweep
    raise REL. Above 1.15 V needs mVolt+'s XOC mode, which the tuner then uses.
    """
    st = status or {}
    pw = knobs.power_info(st)
    rails = knobs.rail_info(st)

    def plus(v: float | None, d: float, cap: float | None) -> float | None:
        if v is None:
            return None
        return min(float(v) + d, float(cap)) if cap is not None else float(v) + d

    # "Reasonable" for exploring the whole front on a modern card: the full
    # power range it allows, +50 mV of core voltage over stock (well inside
    # mVolt+'s 1.15 V standard range, no XOC), and the fabric rail kept at stock.
    suggested = {
        "max_watts": pw["max_w"],
        "max_core_mv": plus(rails["core"]["stock_mv"], 50.0, rails["core"]["standard_max_mv"]),
        "max_fabric_mv": rails["fabric"]["stock_mv"],
    }
    specs = [
        {"key": "max_watts", "label": "max board power", "unit": "W",
         "stock": pw["default_w"], "min": 1.0, "max": pw["max_w"],
         "note": (f"stock {_fmt(pw['default_w'])} W; the card allows up to "
                  f"{_fmt(pw['max_w'])} W" if pw["default_w"] and pw["max_w"] else "")},
        {"key": "max_core_mv", "label": "max core voltage (NVVDD limit)", "unit": "mV",
         "stock": rails["core"]["stock_mv"], "min": rails["core"]["min_mv"],
         "max": rails["core"]["xoc_max_mv"] or rails["core"]["standard_max_mv"],
         "standard_max": rails["core"]["standard_max_mv"],
         "note": f"standard mode up to {_fmt(rails['core']['standard_max_mv'])} mV"
                 + (f", XOC up to {_fmt(rails['core']['xoc_max_mv'])} mV"
                    if rails["core"]["xoc_max_mv"] else "")},
        {"key": "max_fabric_mv", "label": "max fabric voltage (MSVDD limit)", "unit": "mV",
         "stock": rails["fabric"]["stock_mv"], "min": rails["fabric"]["min_mv"],
         "max": rails["fabric"]["xoc_max_mv"] or rails["fabric"]["standard_max_mv"],
         "standard_max": rails["fabric"]["standard_max_mv"],
         "note": f"standard mode up to {_fmt(rails['fabric']['standard_max_mv'])} mV"
                 + (f", XOC up to {_fmt(rails['fabric']['xoc_max_mv'])} mV"
                    if rails["fabric"]["xoc_max_mv"] else "")},
    ]
    for s in specs:
        s["suggested"] = suggested.get(s["key"])
    return specs


def current_limits(status: dict[str, Any]) -> dict[str, float | None]:
    """Stock reference values shown next to the limit prompts."""
    return {s["key"]: s["stock"] for s in limit_specs(status)}


def validate_limits(raw: dict[str, Any], status: dict[str, Any] | None) -> dict[str, float | None]:
    """Parse and range-check limits. Empty / 'none' = no limit on that axis."""
    out: dict[str, float | None] = {}
    for spec in limit_specs(status):
        key, v = spec["key"], raw.get(spec["key"])
        if v is None or (isinstance(v, str) and v.strip().lower() in ("", "none", "no", "-")):
            out[key] = None
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{spec['label']}: '{v}' is not a number (leave empty for none)")
        if not (fv > 0 and fv != float("inf")):
            raise ValueError(f"{spec['label']} must be a positive number")
        if status and spec["max"] is not None and fv > float(spec["max"]):
            raise ValueError(f"{spec['label']}: {fv:g} {spec['unit']} is above what this card "
                             f"allows ({spec['max']:g} {spec['unit']})")
        out[key] = fv
    return out


def ask_limits(status: dict[str, Any], log=print,
               ask: Callable[[str, str], str] | None = None,
               preset: dict[str, Any] | None = None) -> dict[str, float | None]:
    """The user's ceilings. Empty / 'none' means no limit on that axis."""
    ask = ask or _ask
    log("")
    log("  Limits - the only ceilings the tuner will respect. Enter = no limit.")
    log("  They may be above stock: that lets the tuner raise power/voltage up to them.")
    out: dict[str, float | None] = {}
    for spec in limit_specs(status):
        key = spec["key"]
        if preset is not None and key in preset:
            out[key] = preset[key]
            continue
        hint = f" ({spec['note']})" if spec["note"] else ""
        for _ in range(5):
            raw = ask(f"    {spec['label']}{hint}", "none")
            try:
                out[key] = validate_limits({key: raw}, status)[key]
                break
            except ValueError as exc:
                log(f"    {exc}")
        else:
            out[key] = None
    return out


def _ask(prompt: str, default: str) -> str:
    try:
        raw = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        return default
    return raw or default


def _ask_yes(prompt: str, default: bool, ask: Callable[[str, str], str] = _ask) -> bool:
    raw = ask(prompt, "Y" if default else "n").lower()
    return raw.startswith("y")


def _ask_num(prompt: str, default: float, ask: Callable[[str, str], str] = _ask,
             log=print) -> float:
    for _ in range(5):
        raw = ask(prompt, _fmt(default))
        try:
            v = float(raw)
            if v == v and abs(v) != float("inf"):
                return v
        except ValueError:
            pass
        log(f"    '{raw}' is not a number")
    return default


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)


def _limits(key: str, st: knobs.KnobStatus) -> tuple[float | None, float | None]:
    """The driver's own range - the only clamp applied to typed values."""
    return st.bounds if st.bounds else (None, None)


def _clamp(key: str, v: float, lo: float | None, hi: float | None, log=print) -> float:
    c = v
    if lo is not None and c < lo:
        c = lo
    if hi is not None and c > hi:
        c = hi
    if c != v:
        log(f"    {key}: {_fmt(v)} is outside the allowed range "
            f"{_fmt(lo) if lo is not None else '-inf'}..{_fmt(hi) if hi is not None else 'inf'}"
            f" - using {_fmt(c)}")
    return c


def _clamp_suggestion(key: str, st: knobs.KnobStatus) -> dict[str, Any]:
    cur = st.current if isinstance(st.current, (int, float)) and not isinstance(
        st.current, bool) else 0
    sug = dict(SUGGESTIONS.get(key, {"min": cur, "max": cur, "step": 1,
                                     "enabled": False, "why": ""}))
    lo, hi = _limits(key, st)
    quiet = lambda *_a: None  # noqa: E731
    sug["min"] = _clamp(key, sug["min"], lo, hi, quiet)
    sug["max"] = _clamp(key, sug["max"], lo, hi, quiet)
    if sug["min"] > sug["max"]:
        sug["min"], sug["max"] = sug["max"], sug["min"]
    return sug


def plan(status: dict[str, Any], assume_yes: bool = False, log=print,
         writable: dict[str, bool | None] | None = None,
         ask: Callable[[str, str], str] = _ask) -> dict[str, Any]:
    """Build a sweep plan from a live --status payload."""
    rows = knobs.survey(status, writable)
    plan_knobs: dict[str, Any] = {}

    def q(prompt: str, default: str) -> str:
        return default if assume_yes else ask(prompt, default)

    log("")
    log("=" * 68)
    log(" Setup - press Enter to accept each default")
    log("=" * 68)

    # --yes means "no limits" unless they were already saved; never invent one.
    prev = (load() or {}).get("limits")
    limits = ask_limits(status, log, q, preset=prev if assume_yes and prev else None)

    for st in rows:
        if not st.available:
            log(f"  {st.key:<16} unavailable  ({st.reason})")
            continue

        sug = _clamp_suggestion(st.key, st)
        lo, hi = _limits(st.key, st)
        cur = st.current if st.current is not None else "?"
        log("")
        log(f"  {st.key}  [{st.unit or 'switch'}]  current={cur}")
        if sug["why"]:
            log(f"    note: {sug['why']}")
        if st.write_only:
            log("    write-only: the driver never reports this back - "
                "results cannot be verified")

        entry: dict[str, Any] = {"enabled": False, "unit": st.unit,
                                 "write_only": st.write_only}
        if st.key in FIXED_ONLY:
            if _ask_yes("    hold at a fixed value for the study?", False,
                        lambda p, d: q(p, d)):
                entry["enabled"] = True
                if st.key == "boost_lock":
                    entry["value"] = _ask_yes("    boost lock on?", False, lambda p, d: q(p, d))
                else:
                    v = _ask_num("    value", float(st.current or 0), lambda p, d: q(p, d), log)
                    entry["value"] = _clamp(st.key, v, lo, hi, log)
            plan_knobs[st.key] = entry
            continue

        if not _ask_yes("    include in sweep?", bool(sug["enabled"]), lambda p, d: q(p, d)):
            plan_knobs[st.key] = entry
            continue

        entry["enabled"] = True
        mn = _clamp(st.key, _ask_num("    min", sug["min"], lambda p, d: q(p, d), log),
                    lo, hi, log)
        mx = _clamp(st.key, _ask_num("    max", sug["max"], lambda p, d: q(p, d), log),
                    lo, hi, log)
        step = _ask_num("    step", sug["step"], lambda p, d: q(p, d), log)
        if mx < mn:
            mn, mx = mx, mn
        if step <= 0:
            step = (mx - mn) or 1
        entry.update({"min": mn, "max": mx, "step": step})
        plan_knobs[st.key] = entry

    return {
        "version": 3,
        "limits": limits,
        "knobs": plan_knobs,
        "bench": config.bench_defaults(config.bench_tool()),
    }


def load_limits() -> dict[str, float | None] | None:
    """Saved limits, or None if the user has never been asked (first launch)."""
    saved = load() or {}
    lim = saved.get("limits")
    return lim if isinstance(lim, dict) else None


def save_limits(limits: dict[str, float | None]) -> None:
    saved = load() or {"version": 3}
    saved["limits"] = limits
    save(saved)


def save(plan_payload: dict[str, Any]) -> None:
    config.CONFIG_FILE.write_text(json.dumps(plan_payload, indent=2), encoding="utf-8")


def load() -> dict[str, Any] | None:
    if not config.CONFIG_FILE.exists():
        return None
    try:
        return json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _frange(lo: float, hi: float, step: float) -> list[float | int]:
    out: list[float | int] = []
    if step <= 0:
        return [lo]
    n = int((hi - lo) / step + 1e-9)
    for i in range(n + 1):
        v = round(lo + i * step, 3)
        out.append(int(v) if float(v).is_integer() else v)
    return out or [lo]


def grid_from_plan(plan_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand a saved plan into explicit param dicts (swept axes x fixed values).

    REL's VMIN/ALT companions are filled in by the orchestrator from the live
    GPU, so they are not invented here.
    """
    import itertools

    axes: list[tuple[str, list[Any]]] = []
    fixed: dict[str, Any] = {}
    for key, spec in (plan_payload.get("knobs") or {}).items():
        if not spec.get("enabled"):
            continue
        if "value" in spec:
            fixed[key] = spec["value"]
        elif "step" in spec:
            axes.append((key, _frange(float(spec["min"]), float(spec["max"]),
                                      float(spec["step"]))))

    if not axes and not fixed:
        return []
    if not axes:
        return [dict(fixed)]
    return [{**fixed, **{k: v for (k, _), v in zip(axes, values)}}
            for values in itertools.product(*(v for _, v in axes))]
