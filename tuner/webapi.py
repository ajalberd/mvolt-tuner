"""Logic behind the Setup & Run page (web/run.html), kept out of the HTTP layer.

Flow the page enforces:
  1. limits  - max watts / max core voltage (or explicitly none). Nothing else
               works until they are saved: they are the only ceilings.
  2. GPU     - read-only view of every control (--status). The write-back
               probe is a separate, explicit button.
  3. plan    - starting profile + per-knob sweep/fixed values, previewed live.
  4. run     - starts `python -m tuner --from-config ...` as a child process;
               Stop creates data/stop.flag, which the run checks between and
               during trials, then restores your settings and exits cleanly.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

from . import capability, config, knobs, profiles, setup, state

_status_cache: dict[str, Any] = {"t": 0.0, "status": None, "error": None}
_STATUS_TTL_S = 5.0
_run_lock = threading.Lock()
_run: dict[str, Any] = {"proc": None, "started_utc": None, "args": None, "log": None}


# ---------------------------------------------------------------------------
# GPU state (read-only)
# ---------------------------------------------------------------------------

def _status(force: bool = False) -> tuple[dict[str, Any] | None, str | None]:
    from . import mvolt
    now = time.time()
    if force or now - _status_cache["t"] > _STATUS_TTL_S:
        try:
            _status_cache.update(t=now, status=mvolt.status(), error=None)
        except Exception as exc:
            _status_cache.update(t=now, status=None, error=str(exc)[:300])
    return _status_cache["status"], _status_cache["error"]


def _cached_writable(st: dict[str, Any]) -> dict[str, bool | None] | None:
    """Writability from an earlier probe of THIS GPU, without probing again."""
    try:
        cached = json.loads(capability.CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if cached.get("fingerprint") != capability.fingerprint(st):
        return None
    return {k: bool(v.get("writable")) for k, v in (cached.get("knobs") or {}).items()}


# Rail offsets and the settings that only make sense with them.
RAIL_GROUPS = {"rel": ("rel", "vmin", "alt"), "msvdd_rel": ("msvdd_rel", "msvdd_vmin", "msvdd_alt")}


def _dead_rails(st: dict[str, Any] | None) -> set[str]:
    """Rail-offset keys the last probe of THIS GPU/driver found ignored (e.g.
    NVIDIA 621.24 drops NVVDD offsets). Unprobed counts as usable."""
    if st is None:
        return set()
    w = _cached_writable(st) or {}
    return {k for k in RAIL_GROUPS if w.get(k) is False}


def _strip_dead_rails(params: dict[str, Any], dead: set[str]) -> dict[str, Any]:
    drop = {k for r in dead for k in RAIL_GROUPS[r]}
    return {k: v for k, v in params.items() if k not in drop}


def gpu_state() -> dict[str, Any]:
    from . import mvolt
    st, err = _status()
    out: dict[str, Any] = {
        "elevated": mvolt.is_admin(),
        "limits": setup.load_limits(),
        "plan": (setup.load() or {}).get("web"),
        "status_error": err,
        "app_running": profiles.app_running(),
        "profiles": profiles.read_profiles(),
        "run": run_status(),
    }
    try:
        out["mvolt"] = str(config.mvolt_path())
    except config.DependencyMissing as exc:
        out["mvolt"] = None
        out["status_error"] = str(exc)
    if st is None:
        return out
    wmap = _cached_writable(st)
    defaults = knobs.card_defaults(st)
    out.update({
        "gpu": st.get("gpu"), "vbios": st.get("vbios"),
        "active_profile": st.get("active_profile"),
        "xoc_active": bool(st.get("xoc")),
        "stock": setup.current_limits(st),
        "limit_specs": setup.limit_specs(st),
        "power": knobs.power_info(st),
        "rails": knobs.rail_info(st),
        "profile_only": profiles.profile_only_controls(),
        "probed": wmap is not None,
        "knobs": [{
            "key": r.key, "available": r.available, "reason": r.reason,
            "current": r.current, "bounds": r.bounds, "unit": r.unit, "group": r.group,
            "note": r.note, "writable": None if wmap is None else wmap.get(r.key),
            "default": (defaults.get(r.key) or {}).get("default"),
            "default_note": (defaults.get(r.key) or {}).get("source"),
            "suggestion": setup.SUGGESTIONS.get(r.key),
        } for r in knobs.survey(st, wmap)],
    })
    return out


def probe() -> dict[str, Any]:
    """The explicit write-back test: rewrites each control's current value."""
    from . import mvolt
    if not mvolt.is_admin():
        raise PermissionError("the server is not elevated - start it from an "
                              "Administrator terminal to probe or run")
    st, err = _status(force=True)
    if st is None:
        raise RuntimeError(err or "--status unavailable")
    return {"writable": capability.writable_map(force=True, status=st)}


# ---------------------------------------------------------------------------
# limits
# ---------------------------------------------------------------------------

def save_limits(body: dict[str, Any]) -> dict[str, Any]:
    """Validate against what THIS card allows (may be above stock), then save."""
    st, _ = _status()
    lim = setup.validate_limits(body, st)
    setup.save_limits(lim)
    return {"limits": lim}


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

def _plan_knobs(body: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, spec in (body.get("knobs") or {}).items():
        if key not in knobs.KNOBS or not isinstance(spec, dict):
            raise ValueError(f"unknown control '{key}'")
        mode = spec.get("mode", "off")
        if mode == "off":
            continue
        try:
            if mode == "fixed":
                v = spec.get("value")
                out[key] = {"enabled": True,
                            "value": bool(v) if key == "boost_lock" else float(v)}
            elif mode == "sweep":
                mn, mx, step = float(spec["min"]), float(spec["max"]), float(spec["step"])
                if step <= 0:
                    raise ValueError(f"{key}: step must be > 0")
                if mx < mn:
                    mn, mx = mx, mn
                out[key] = {"enabled": True, "min": mn, "max": mx, "step": step}
            else:
                raise ValueError(f"{key}: unknown mode '{mode}'")
        except (KeyError, TypeError) as exc:
            raise ValueError(f"{key}: incomplete or non-numeric values ({exc})")
    return out


def build_plan(body: dict[str, Any], status: dict[str, Any] | None = None
               ) -> dict[str, Any]:
    """Grid for a plan request. Raises ValueError with a user-facing message."""
    from . import orchestrator
    plan_knobs = _plan_knobs(body)
    grid = setup.grid_from_plan({"knobs": plan_knobs})
    base_name = str(body.get("baseline_profile") or "")
    base: dict[str, Any] = {}
    if base_name:
        prof = next((p for p in profiles.read_profiles()
                     if p["id"] == base_name or p["name"] == base_name), None)
        if prof is None:
            raise ValueError(f"profile '{base_name}' not found")
        base = {k: v for k, v in (prof.get("params") or {}).items() if v}
        grid = orchestrator.apply_base(grid or [{}], base)
    if not grid or grid == [{}]:
        raise ValueError("nothing to run: enable at least one knob (or pick a profile)")
    if len(grid) > 2000:
        raise ValueError(f"{len(grid)} configs is too many; widen the steps")
    orchestrator.check_grid(grid, status)
    trials = int(body.get("trials") or len(grid))
    return {"knobs": plan_knobs, "grid": grid, "count": len(grid), "base": base,
            "baseline_profile": base_name, "trials": max(1, trials),
            "include_baseline": bool(body.get("include_baseline"))}


def preview(body: dict[str, Any]) -> dict[str, Any]:
    st, _ = _status()
    p = build_plan(body, st)
    minutes = (p["count"] + (1 if p["include_baseline"] else 0)) * 1.6
    return {"count": p["count"], "sample": p["grid"][:12], "base": p["base"],
            "estimate_min": round(minutes)}


# ---------------------------------------------------------------------------
# smart search: explore -> proposal -> refine
# ---------------------------------------------------------------------------

def _start_params(profile_name: str, st: dict[str, Any] | None) -> dict[str, Any]:
    """The configuration a search spreads around: a saved profile, or what the
    card runs right now. Only searchable controls and REL companions."""
    from . import search
    keep = set(search.SPANS) | search.COMPANION_KEYS
    if profile_name:
        prof = next((p for p in profiles.read_profiles()
                     if p["id"] == profile_name or p["name"] == profile_name), None)
        if prof is None:
            raise ValueError(f"profile '{profile_name}' not found")
        out = {k: v for k, v in (prof.get("params") or {}).items() if k in keep}
    elif st is None:
        return {}
    else:
        out = {k: v for k, v in knobs.snapshot(st).items() if k in keep}
    # A rail the driver ignores cannot be held at the profile's value either.
    return _strip_dead_rails(out, _dead_rails(st))


def search_defaults(profile_name: str = "") -> dict[str, Any]:
    """Default knob selection (relative spans) for the page to edit."""
    from . import search
    st, err = _status()
    if st is None:
        raise RuntimeError(err or "--status unavailable")
    start = _start_params(profile_name, st)
    sel = search.default_selection(st, start, setup.load_limits())
    dead = _dead_rails(st)
    for k in dead:
        sel.pop(k, None)
    return {"start": start, "selection": sel, "dead_rails": sorted(dead)}


def search_plan(body: dict[str, Any]) -> dict[str, Any]:
    """Explore plan for a selection: absolute space + the spread-out points."""
    from . import search
    st, err = _status()
    if st is None:
        raise RuntimeError(err or "--status unavailable")
    start = _start_params(str(body.get("profile") or ""), st)
    sel = body.get("selection") or {}
    if not isinstance(sel, dict):
        raise ValueError("selection must be an object")
    for k in _dead_rails(st):
        if (sel.get(k) or {}).get("on"):
            raise ValueError(f"'{k}' cannot be searched: the NVIDIA driver ignores these offsets")
        sel.pop(k, None)
    for key, s in sel.items():
        if key != "cap" and key not in search.SPANS:
            raise ValueError(f"'{key}' cannot be searched")
        for f in ("lo", "hi", "step"):
            if s.get("on") and not isinstance(s.get(f), (int, float)):
                try:
                    s[f] = float(s[f])
                except (TypeError, ValueError, KeyError):
                    raise ValueError(f"{key}: '{f}' must be a number")
    space = search.build_space(st, start, sel, setup.load_limits())
    if not space["dims"]:
        raise ValueError("tick at least one control to explore")
    n = int(body.get("runs") or 30)
    if not 4 <= n <= 500:
        raise ValueError("explore runs must be between 4 and 500")
    points = search.lhs_points(space, n, seed=int(body.get("seed") or 11))
    from . import crashes
    rules = guard_rules()
    guarded = [p for p in points if crashes.guard(p, rules)]
    return {"start": start, "space": space, "points": points, "count": len(points),
            "guarded": len(guarded),
            "guard_rules": [crashes.describe_rule(r) for r in rules],
            "notes": space["notes"], "sample": points[:8],
            "estimate_min": round((len(points) + (1 if body.get("include_baseline") else 0)) * 1.6)}


_boot: dict[str, Any] = {"id": None}


def _current_boot() -> str:
    if _boot["id"] is None:
        _boot["id"] = state.boot_id()
    return _boot["id"]


def _all_trials() -> list[dict[str, Any]]:
    j = state.Journal()
    try:
        return j.all_trials()
    finally:
        j.close()


def crash_log() -> dict[str, Any]:
    """Every failure with its kind, culprits for hard freezes, and guard rules."""
    from . import crashes
    boot = None if run_status()["running"] else _current_boot()
    lg = crashes.log(_all_trials(), boot)
    # Hard freezes from the other benchmarks' journals guard this one too.
    lg["rules"] = lg["rules"] + crashes.rules(state.hardware_trials(exclude=config.journal_db()), boot)
    for r in lg["rules"]:
        r["text"] = crashes.describe_rule(r)
    return lg


def guard_rules() -> list[dict[str, Any]]:
    from . import crashes
    boot = None if run_status()["running"] else _current_boot()
    return (crashes.rules(_all_trials(), boot)
            + crashes.rules(state.hardware_trials(exclude=config.journal_db()), boot))


def bench_info() -> dict[str, Any]:
    """The selected benchmark, the choices, and which are installed."""
    found = {}
    for tool in config.BENCH_TOOLS:
        try:
            (config.gravitymark_exe if tool == "gravitymark" else config.kombustor_exe)()
            found[tool] = True
        except config.DependencyMissing:
            found[tool] = False
    counts = {}
    for tool in config.BENCH_TOOLS:
        path = config.journal_db(tool)
        if path.exists():
            j = state.Journal(path)
            try:
                counts[tool] = len(j.all_trials())
            finally:
                j.close()
        else:
            counts[tool] = 0
    tool = config.bench_tool()
    bench = (setup.load() or {}).get("bench") or {}
    args = bench.get("args") if bench.get("tool", "kombustor") == tool else None
    return {"tool": tool, "tools": config.BENCH_TOOLS, "installed": found, "runs": counts,
            "badge": bench_badge(tool, args or config.bench_defaults(tool)["args"])}


def bench_badge(tool: str, args: list[str]) -> str:
    """Short label of what each trial runs, from the benchmark's own arguments."""
    a = [str(x) for x in args]
    if tool == "gravitymark":
        api = next((x.lstrip("-") for x in a if x.lower() in
                    ("-d3d11", "-d3d12", "-vulkan", "-opengl")), "d3d11")
        def val(flag):
            return a[a.index(flag) + 1] if flag in a and a.index(flag) + 1 < len(a) else None
        res = f"{val('-width')}×{val('-height')}" if val("-width") and val("-height")             else "desktop resolution"
        return f"GravityMark {api.upper()} · {res} · {val('-asteroids') or '200,000'} asteroids"
    test = next((x.lstrip("-") for x in a if x.startswith("-gl") or x.startswith("-vk")), "?")
    w = next((x.split("=", 1)[1] for x in a if x.startswith("-width=")), None)
    h = next((x.split("=", 1)[1] for x in a if x.startswith("-height=")), None)
    return f"Kombustor {test}" + (f" · {w}×{h}" if w and h else "")


def switch_bench(tool: str) -> dict[str, Any]:
    """Select the benchmark. Each benchmark is its own study: its journal, and
    its explore/edge/confirm plans - the current ones are put away and the
    other benchmark's are brought back, so switching back resumes exactly."""
    if tool not in config.BENCH_TOOLS:
        raise ValueError(f"unknown benchmark {tool!r}")
    with _run_lock:
        if run_status()["running"]:
            raise RuntimeError("stop the run before switching benchmarks")
        _switch_tool(tool)
    return bench_info()


def _switch_tool(tool: str) -> None:
    """The switch itself, for callers that already know no run is active (the
    run queue, between two stages)."""
    if tool not in config.BENCH_TOOLS:
        raise ValueError(f"unknown benchmark {tool!r}")
    old = config.bench_tool()
    if tool == old:
        return
    saved = setup.load() or {}
    studies = saved.setdefault("studies", {})
    studies[old] = {"search": saved.get("search") or {},
                    "bench": saved.get("bench") or config.bench_defaults(old)}
    back = studies.pop(tool, None) or {}
    saved["search"] = back.get("search") or {}
    saved["bench"] = dict(back.get("bench") or config.bench_defaults(tool), tool=tool)
    setup.save(saved)
    # The Results page reads live.json: rebuild it from the new journal.
    j = state.Journal()
    try:
        j.write_live(config.LIVE_JSON, running=False)
    finally:
        j.close()


def _saved_search() -> dict[str, Any]:
    return (setup.load() or {}).get("search") or {}


def _trials() -> list[dict[str, Any]]:
    j = state.Journal()
    try:
        return j.scored_trials()
    finally:
        j.close()


def search_status() -> dict[str, Any]:
    """Where the study stands: explore progress, edge lanes, confirmation."""
    from . import edge
    from .state import param_key
    s = _saved_search()
    from . import vdemand
    out: dict[str, Any] = {"explore": None, "edge": None, "confirm": None,
                           "vdemand": None, "vdemand_bases": []}
    trials = _trials()
    finished = {param_key(t["params"]) for t in trials
                if t["status"] not in ("aborted", "cancelled", "applying", "running")}
    ex = s.get("explore")
    if ex:
        done = sum(param_key(p) in finished for p in ex.get("points") or [])
        out["explore"] = {"profile": ex.get("profile"), "runs": len(ex.get("points") or []),
                          "done": done, "dims": ex["space"]["dims"]}
    ed = (s.get("edge") or {}).get("plan")
    if ed:
        lanes = edge.lanes(trials, ed)
        out["edge"] = {"plan": ed, "lanes": lanes,
                       "done": sum(l["done"] for l in lanes), "total": len(lanes),
                       "candidates": edge.candidates(trials, ed, *_reference(s))}
    cf = s.get("confirm")
    if cf:
        out["confirm"] = {"repeats": cf.get("repeats"),
                          "results": edge.confirm_results(trials, cf.get("candidates") or [])}
        out["vdemand_bases"] = _vdemand_bases(out["confirm"]["results"])
    vd = (s.get("vdemand") or {}).get("plan")
    if vd:
        out["vdemand"] = {"plan": vd, "lanes": vdemand.lanes(trials, vd),
                          "remaining": vdemand.estimate_runs(trials, vd)}
    out["seed"] = seed_candidates() if not cf else None
    return out


SEED_SOURCE = "kombustor"


def _other_tool() -> str:
    return next(t for t in config.BENCH_TOOLS if t != config.bench_tool())


def validate_candidates() -> list[dict[str, Any]] | None:
    """The other benchmark's CONFIRMED configs, exactly as confirmed there."""
    from . import edge
    other = _other_tool()
    path = config.journal_db(other)
    if not path.exists():
        return None
    src = (((setup.load() or {}).get("studies") or {}).get(other) or {}).get("search") or {}
    cands = (src.get("confirm") or {}).get("candidates") or []
    if not cands:
        return None
    j = state.Journal(path)
    try:
        res = edge.confirm_results(j.scored_trials(), cands)
    finally:
        j.close()
    name = "GravityMark" if other == "gravitymark" else "Kombustor"
    out = []
    for r in res:
        if r.get("verdict") != "confirmed":
            continue
        out.append({"params": dict(r["params"]), "cap": r.get("cap"), "rel": r.get("rel"),
                    "core": r.get("core"), "edge": None,
                    "src_score": round(r["score_mean"]) if r.get("score_mean") else None,
                    "src_watts": round(r["watts_mean"], 1) if r.get("watts_mean") else None,
                    "pick": f"{name} confirmed · {r.get('pick') or 'config'}"})
        # Its real draw under a heavy load: the same config with the cap at
        # your watt limit (the confirmed cap may simply bind and hide it).
        top = (setup.load_limits() or {}).get("max_watts")
        if top is not None and r.get("cap") is not None and float(top) > float(r["cap"]):
            p = dict(r["params"], cap=float(top))
            out.append({"params": p, "cap": float(top), "rel": r.get("rel"), "core": r.get("core"),
                        "edge": None, "src_score": None, "src_watts": None,
                        "pick": f"{name} confirmed · {r.get('pick') or 'config'} · at your "
                                f"{float(top):g} W limit"})
    return out or None


def seed_candidates() -> list[dict[str, Any]] | None:
    """A new benchmark's starting configs: Kombustor's clean Pareto front plus
    its reference profile (none of its scores carry over). None while the
    Kombustor study itself is selected, or when it has nothing to offer."""
    from . import edge, seed
    if config.bench_tool() == SEED_SOURCE:
        return None
    st, _ = _status()
    if "rel" in _dead_rails(st):
        return None     # every front config depends on REL, which this driver ignores
    path = config.journal_db(SEED_SOURCE)
    if not path.exists():
        return None
    j = state.Journal(path)
    try:
        trials = j.scored_trials()
    finally:
        j.close()
    saved = setup.load() or {}
    src = ((saved.get("studies") or {}).get(SEED_SOURCE) or {}).get("search") or {}
    cands = ((src.get("confirm") or {}).get("candidates")) or []
    ref = next((c for c in cands if str(c.get("pick") or "").startswith("reference")), None)
    out = seed.candidates(trials, "Kombustor", ref)
    return out or None


def _vdemand_bases(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Confirmed configs the voltage-demand sweep can start from. The tuned
    ones are ticked by default; your reference profile is offered too."""
    out = []
    for i, r in enumerate(results):
        if r.get("verdict") != "confirmed":
            continue
        pick = r.get("pick") or ""
        out.append({"index": i, "pick": pick, "cap": r.get("cap"), "rel": r.get("rel"),
                    "core": r.get("core"), "mem": (r.get("params") or {}).get("mem"),
                    "score_mean": r.get("score_mean"),
                    "watts_mean": r.get("watts_mean"),
                    "default": not pick.startswith("reference")})
    return out


def analyse() -> dict[str, Any]:
    """What the explore pass found, in units, plus a suggested edge search."""
    from . import analysis, edge
    ex = _saved_search().get("explore")
    if not ex:
        raise ValueError("run an explore pass first")
    trials = _trials()
    dims = set(ex["space"]["dims"])
    inside = [t for t in trials if all(k in (t.get("params") or {}) for k in dims)]
    rep = analysis.summary(inside, dims)
    rep["stability_edge"] = [e for e in rep["stability_edge"]
                             if e.get("lowest_failed") is not None][:12]
    st, _err = _status()
    start = ex.get("start") or {}
    plan = edge.suggest_plan(st or {}, start, setup.load_limits(), inside,
                             fixed_rels=[0.0] if "rel" in _dead_rails(st) else None)
    return {"explore_runs": len(inside), "analysis": rep, "start": start,
            "suggested_edge": plan, "estimate_runs": edge.estimate_runs(plan, trials)}


def _reference(search_cfg: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """The explore starting point (your saved profile, if one was picked): it
    is confirmed alongside the candidates so the gain over it is measured."""
    ex = search_cfg.get("explore") or {}
    return (ex.get("start") or None), str(ex.get("profile") or "")


def edge_plan_from(body: dict[str, Any]) -> dict[str, Any]:
    """Edge-search plan from the page's form (budgets, REL levels, core range, held values)."""
    from . import edge
    ex = _saved_search().get("explore") or {}
    start = dict(ex.get("start") or {})
    if body.get("profile") is not None and not ex:
        st, _ = _status()
        start = _start_params(str(body.get("profile") or ""), st)
    for k, v in (body.get("held") or {}).items():
        if k in ("core", "rel", "cap"):
            continue
        try:
            start[k] = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"held value for {k} must be a number")

    def nums(name: str) -> list[float]:
        raw = body.get(name)
        vals = raw if isinstance(raw, list) else str(raw or "").replace(";", ",").split(",")
        try:
            return [float(v) for v in vals if str(v).strip() != ""]
        except ValueError:
            raise ValueError(f"{name}: comma-separated numbers please")

    core = body.get("core") or {}
    try:
        lo, hi = float(core["lo"]), float(core["hi"])
        step = float(core.get("step") or 15)
    except (KeyError, TypeError, ValueError):
        raise ValueError("core range needs numbers for from / to")
    st_now, _ = _status()
    if "rel" in _dead_rails(st_now):
        start = _strip_dead_rails(start, {"rel"})
        if any(r != 0 for r in nums("rels")):
            raise ValueError("REL levels must be 0: the NVIDIA driver ignores core voltage "
                             "offsets (use the voltage-demand sweep after confirming)")
    plan = edge.make_plan(start, nums("budgets"), nums("rels"), lo, hi, step,
                          float(body.get("margin") or 15), int(body.get("repeats") or 3))
    lim = setup.load_limits() or {}
    if lim.get("max_watts") is not None and max(plan["budgets"]) > float(lim["max_watts"]):
        raise ValueError(f"a budget is above your power limit ({lim['max_watts']:g} W)")
    return plan


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def _external_run() -> dict[str, Any] | None:
    """A run started from a terminal, detected via its heartbeat."""
    try:
        hb = json.loads(config.HEARTBEAT.read_text(encoding="utf-8"))
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(hb["utc"])).total_seconds()
    except Exception:
        return None
    if age < 30 and hb.get("phase") != "shutting_down":
        return hb
    return None


def run_status() -> dict[str, Any]:
    proc = _run["proc"]
    running = proc is not None and proc.poll() is None
    tail: list[str] = []
    try:
        tail = config.RUN_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
    except OSError:
        pass
    ext = None if running else _external_run()
    return {
        "running": running or ext is not None,
        "source": "web" if running else ("terminal" if ext else None),
        "pid": proc.pid if proc is not None else (ext or {}).get("pid"),
        "exit_code": None if proc is None or running else proc.returncode,
        "started_utc": _run["started_utc"],
        "args": _run["args"],
        "heartbeat": ext if ext else _heartbeat(),
        "stopping": state.stop_requested(),
        "log": tail,
    }


def _heartbeat() -> dict[str, Any] | None:
    try:
        return json.loads(config.HEARTBEAT.read_text(encoding="utf-8"))
    except Exception:
        return None


def prepare_run(body: dict[str, Any], st: dict[str, Any] | None) -> list[str]:
    """Build and save the plan for one run (any mode); return its command line.
    Shared by the Start button and the run queue (tuner.pipeline)."""
    simulate = bool(body.get("simulate"))
    saved = setup.load() or {}
    saved["version"] = 3
    if (saved.get("bench") or {}).get("tool", "kombustor") != config.bench_tool() \
            or not saved.get("bench"):
        saved["bench"] = config.bench_defaults(config.bench_tool())
    mode = body.get("mode") or "grid"
    base_args = [sys.executable, "-u", "-m", "tuner"]
    if mode == "explore":
        plan = search_plan(body)
        saved.setdefault("search", {})["explore"] = {
            "space": plan["space"], "points": plan["points"], "start": plan["start"],
            "profile": body.get("profile") or "", "selection": body.get("selection"),
            "runs": plan["count"], "created_utc": state.utcnow()}
        saved["search"].pop("refine", None)   # a new exploration starts a new study
        args = base_args + ["--search", "explore", "--trials", str(plan["count"])]
    elif mode == "edge":
        from . import edge
        plan = edge_plan_from(body)
        sc = saved.setdefault("search", {})
        sc["edge"] = {"plan": plan, "created_utc": state.utcnow()}
        sc.pop("confirm", None)
        args = base_args + ["--search", "edge",
                            "--trials", str(2 * edge.estimate_runs(plan, _trials()) + 20)]
    elif mode == "ab":
        # Does WRITING core voltage demand (even 0) change the score regime?
        # A = the confirmed config as is, B = the same with core_vdemand 0.
        from . import edge
        cf = (saved.get("search") or {}).get("confirm") or {}
        results = edge.confirm_results(_trials(), cf.get("candidates") or [])
        i = int(body.get("base", -1))
        if not 0 <= i < len(results) or results[i].get("verdict") != "confirmed":
            raise ValueError("pick a confirmed config")
        seq = str(body.get("sequence") or "AAABBBAA").upper()
        if not seq or set(seq) - {"A", "B"} or len(seq) > 20:
            raise ValueError("sequence: up to 20 letters A/B")
        a = {k: v for k, v in results[i]["params"].items() if k != "core_vdemand"}
        pts = [a if c == "A" else dict(a, core_vdemand=0.0) for c in seq]
        saved.setdefault("search", {})["ab"] = {"points": pts, "sequence": seq, "base": i,
                                                "created_utc": state.utcnow()}
        args = base_args + ["--search", "ab", "--trials", str(len(pts))]
    elif mode == "vdemand":
        from . import edge, vdemand
        sc = saved.get("search") or {}
        cf = sc.get("confirm") or {}
        results = edge.confirm_results(_trials(), cf.get("candidates") or [])
        ok = {b["index"]: b for b in _vdemand_bases(results)}
        picked = [int(i) for i in body.get("bases") or []]
        if not picked or any(i not in ok for i in picked):
            raise ValueError("pick at least one confirmed config to sweep")
        bases = [{"params": results[i]["params"],
                  "label": f"{results[i].get('pick') or 'config'} · "
                           f"{results[i].get('cap'):g} W / REL {results[i].get('rel'):g} / "
                           f"core +{results[i].get('core'):g}"} for i in picked]
        vals = [float(v) for v in body.get("values") or []]
        trials = _trials()
        # Count only runs made after this point - in the journal the run
        # will write to. A dry run has its own journal (a copy of the real
        # one once made), whose ids differ from the real one's.
        run_trials = trials
        dry_db = config.journal_db(dry=True)
        if simulate and dry_db.exists():
            j = state.Journal(dry_db)
            try:
                run_trials = j.scored_trials()
            finally:
                j.close()
        since = max((int(t.get("id") or 0) for t in run_trials), default=0)
        plan = vdemand.make_plan(bases, vals, int(body.get("repeats") or 2), since,
                                 str(body.get("key") or vdemand.KEY))
        saved.setdefault("search", {})["vdemand"] = {"plan": plan,
                                                     "created_utc": state.utcnow()}
        n = vdemand.estimate_runs(trials, plan)
        args = base_args + ["--search", "vdemand", "--trials", str(n + 10)]
    elif mode == "seed":
        # First stage of a new benchmark's study: measure the other
        # study's Pareto front here, through the confirm stage.
        from . import edge
        cands = seed_candidates()
        if not cands:
            raise ValueError("nothing to seed from: select GravityMark, and have Kombustor results")
        repeats = int(body.get("repeats") or 2)
        if not 1 <= repeats <= 20:
            raise ValueError("runs per config must be between 1 and 20")
        pts = edge.confirm_points(cands, repeats)
        saved.setdefault("search", {})["confirm"] = {
            "candidates": cands, "points": pts, "repeats": repeats,
            "source": SEED_SOURCE + "-front", "created_utc": state.utcnow()}
        args = base_args + ["--search", "confirm", "--trials", str(len(pts))]
    elif mode == "validate":
        # Re-measure the OTHER benchmark's confirmed configs here, e.g. a
        # light-load GravityMark result under Kombustor's heavy load to see
        # its real wattage. Runs through the confirm stage of this study.
        from . import edge
        cands = validate_candidates()
        if not cands:
            raise ValueError("the other benchmark has no confirmed configs to validate")
        repeats = int(body.get("repeats") or 2)
        if not 1 <= repeats <= 20:
            raise ValueError("runs per config must be between 1 and 20")
        sc = saved.setdefault("search", {})
        if sc.get("confirm") and not sc.get("confirm", {}).get("source", "").startswith("validate"):
            sc["confirm_prev"] = sc["confirm"]      # keep this study's own confirm plan
        pts = edge.confirm_points(cands, repeats)
        sc["confirm"] = {"candidates": cands, "points": pts, "repeats": repeats,
                         "source": "validate-" + _other_tool(), "created_utc": state.utcnow()}
        args = base_args + ["--search", "confirm", "--trials", str(len(pts))]
    elif mode == "confirm":
        from . import edge
        ed = (saved.get("search") or {}).get("edge") or {}
        if not ed.get("plan"):
            raise ValueError("run the edge search first")
        plan = dict(ed["plan"])
        plan["margin"] = float(body.get("margin") or plan.get("margin") or 15)
        repeats = int(body.get("repeats") or plan.get("repeats") or 3)
        if not 2 <= repeats <= 20:
            raise ValueError("repeats must be between 2 and 20")
        cands = edge.candidates(_trials(), plan, *_reference(saved.get("search") or {}))
        if not any(c.get("edge") is not None for c in cands):
            raise ValueError("no lane has found its edge yet - finish the edge search")
        pts = edge.confirm_points(cands, repeats)
        saved.setdefault("search", {})["confirm"] = {
            "candidates": cands, "points": pts, "repeats": repeats,
            "margin": plan["margin"], "created_utc": state.utcnow()}
        args = base_args + ["--search", "confirm", "--trials", str(len(pts))]
    else:
        p = build_plan(body, st)
        saved.update({
            "knobs": p["knobs"],
            "web": {k: body.get(k) for k in ("baseline_profile", "knobs", "trials",
                                             "include_baseline")},
        })
        args = base_args + ["--from-config", "--trials", str(p["trials"])]
        if p["baseline_profile"]:
            args += ["--baseline-profile", p["baseline_profile"]]
    setup.save(saved)
    if body.get("include_baseline") and mode != "refine":
        args.append("--baseline")
    if simulate:
        args.append("--simulate")

    return args


def start_run(body: dict[str, Any]) -> dict[str, Any]:
    from . import mvolt
    with _run_lock:
        if run_status()["running"]:
            raise RuntimeError("a run is already in progress")
        if setup.load_limits() is None:
            raise RuntimeError("set your limits first (step 1)")
        simulate = bool(body.get("simulate"))
        if not simulate and not mvolt.is_admin():
            raise PermissionError("the server is not elevated - start it from an "
                                  "Administrator terminal to run")
        st, err = (None, None) if simulate else _status(force=True)
        if not simulate and st is None:
            raise RuntimeError(err or "--status unavailable")

        args = prepare_run(body, st)
        state.clear_stop()
        log = open(config.RUN_LOG, "w", encoding="utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        _run.update(
            proc=subprocess.Popen(args, cwd=str(config.ROOT), stdout=log,
                                  stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                  creationflags=flags),
            started_utc=state.utcnow(), args=args[3:], log=log)
        return run_status()


def stop_run() -> dict[str, Any]:
    """Ask the run to stop. It finishes the current step, restores your
    settings, and exits - it is never killed mid-apply."""
    config.STOP_FLAG.write_text(state.utcnow(), encoding="utf-8")
    return run_status()
