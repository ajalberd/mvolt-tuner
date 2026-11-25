"""Trial orchestrator: apply -> benchmark -> verify -> record, with crash recovery.

Run elevated:  python -m tuner --trials 20
Test safely:   python -m tuner --simulate --trials 5
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import random
import sys
import time
from typing import Any

from . import config, kombustor, knobs, mvolt, state

# Last-resort restore when the user's own settings could not be captured.
SAFE = {"core": 0, "mem": 0, "rel": 0, "vmin": 0, "alt": 0, "boost_lock": False}

# Trial outcomes that are evidence the config is unstable. A grid point at least
# as aggressive as one of these is never applied again (see knobs.AGGRESSION).
UNSTABLE = ("crash", "driver_reset")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# grid construction and safety
# ---------------------------------------------------------------------------

def build_grid(core_vals: list[int], mems: list[int], caps: list[float] | None = None,
               rels: list[int] | None = None) -> list[dict[str, Any]]:
    """Cartesian grid from CLI ranges. Values are offsets relative to the baseline."""
    rels = rels or [0]
    caps = caps or [None]
    combos = []
    for core, mem, rel, cap in itertools.product(core_vals, mems, rels, caps):
        p: dict[str, Any] = {"core": core, "mem": mem}
        if rel:
            p["rel"] = rel
        if cap is not None:
            p["cap"] = cap
        combos.append(p)
    return combos


def apply_base(grid: list[dict[str, Any]], base: dict[str, Any]) -> list[dict[str, Any]]:
    """Sweep around an existing profile: offset swept knobs by the profile's value
    and keep every other control the profile sets at the profile's value."""
    if not base:
        return grid
    out = []
    for p in grid:
        q = dict(p)
        for k, v in base.items():
            if k in ("power", "cap") and (("cap" if k == "power" else "power") in q):
                continue  # mutually exclusive; the swept one wins
            if k in q and isinstance(q[k], (int, float)) and not isinstance(q[k], bool):
                q[k] = q[k] + v
            else:
                q.setdefault(k, v)
        out.append(q)
    return out


def fill_companions(grid: list[dict[str, Any]], companions: dict[str, Any]) -> None:
    """REL is written together with VMIN/ALT. Use the user's current values for
    the companions so a REL sweep does not silently zero them."""
    for p in grid:
        for knob, comps in knobs.COMPANIONS.items():
            if knob in p:
                for c in comps:
                    p.setdefault(c, companions.get(c, 0))


def check_grid(grid: list[dict[str, Any]], status: dict[str, Any] | None = None) -> None:
    """Raise ValueError if any point is malformed or outside the driver's own range.

    No tuner-imposed ceilings: what the driver reports through mVolt+ is the limit.
    """
    bad = []
    for p in grid:
        problems = knobs.validate_params(p, status)
        if problems:
            bad.append(f"{p}: {'; '.join(problems)}")
    if bad:
        more = f" (+{len(bad) - 3} more)" if len(bad) > 3 else ""
        raise ValueError("refusing grid:\n  " + "\n  ".join(bad[:3]) + more)


def enforce_power_limit(grid: list[dict[str, Any]], limits: dict[str, Any] | None,
                        status: dict[str, Any] | None,
                        writable: dict[str, bool | None] | None) -> str:
    """Make the GPU itself hold the user's watt limit on every grid point.

    A stress test pulls the card up to its own power limit, so measuring and
    stopping alone would cut every run short. Instead each point gets a watt
    cap of min(its own cap, your limit); a swept power % above the limit is
    replaced by that cap. Returns how the limit is enforced, for the log.
    """
    max_w = (limits or {}).get("max_watts")
    if max_w is None:
        return "no watt limit"
    writable = writable or {}
    default_w = None
    pl = (status or {}).get("power_limits") or {}
    if pl.get("default_mw"):
        default_w = float(pl["default_mw"]) / 1000.0

    if writable.get("cap") is not False:
        for p in grid:
            pct = p.pop("power", None)
            cap = float(p.get("cap", max_w))
            if pct is not None and default_w:
                cap = min(cap, default_w * float(pct) / 100.0)
            p["cap"] = round(min(cap, float(max_w)), 1)
        return f"watt cap {max_w:g} W on every trial (stop backstop at +5%)"

    if writable.get("power") is not False and default_w:
        lo = (status or {}).get("power_limit_percent_limit_min")
        pct_cap = int(float(max_w) / default_w * 100)
        if lo is not None and pct_cap < float(lo):
            return (f"WARNING: {max_w:g} W is below the lowest power limit this card "
                    f"allows ({lo}% of {default_w:g} W); runs will be stopped instead")
        for p in grid:
            p["power"] = min(int(p.get("power", pct_cap)), pct_cap)
        return f"power limit {pct_cap}% (~{max_w:g} W) on every trial"
    return "WARNING: no writable power control; runs over the limit will be stopped"


def ruled_out(point: dict[str, Any], failures: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The failure that makes `point` too risky to try, if any."""
    for f in failures:
        if knobs.at_least_as_aggressive(point, f):
            return f
    return None


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------

def capture_restore_point(st: dict[str, Any], keys: set[str]) -> dict[str, Any]:
    """What to put back at the end: the user's profile if one is verifiably
    applied, otherwise the readback values of every control the grid touches."""
    prof = st.get("active_profile")
    snap = knobs.snapshot(st, sorted(keys))
    # If no watt cap was held by mVolt+ before the study, the right way back is
    # --return-to-percentage, not re-writing the old wattage as a new cap.
    managed = bool((st.get("power_client") or {}).get("managed_cap"))
    release_cap = "cap" in keys and not managed
    if release_cap or ("power" in snap and "cap" in snap):
        snap.pop("cap", None)
    # --return-to-percentage keeps the LAST cap's wattage as the new percentage
    # (a 250 W cap came back as 69 %), so the percentage is written back too.
    pct = st.get("power_limit_percent")
    return {"profile": prof if isinstance(prof, str) and prof and prof != "Custom" else None,
            "params": snap, "release_cap": release_cap,
            "power_pct": int(pct) if release_cap and isinstance(pct, (int, float)) else None,
            # settings already above 1.15 V can only be written back in XOC mode
            "xoc": bool(st.get("xoc"))}


# Controls whose neutral value is 0 (an offset): safe to reset when the
# restore point predates them.
NEUTRAL_ZERO = ("core", "mem", "xbar_offset", "sys_offset", "video_offset",
                "core_vdemand", "xbar_vdemand", "sys_vdemand", "video_vdemand")


def extend_restore_point(point: dict[str, Any], touched: set[str]) -> list[str]:
    """Add controls this stage touches that `point` does not cover: offsets go
    back to 0, a watt cap is released. Returns what was added."""
    params = point.setdefault("params", {})
    added = []
    for k in sorted(touched):
        if k in params:
            continue
        if k in NEUTRAL_ZERO:
            params[k] = 0
            added.append(k)
        elif k == "cap" and "power" not in params and not point.get("release_cap"):
            point["release_cap"] = True
            added.append("power cap (released)")
    return added


def restore(point: dict[str, Any] | None, simulate: bool) -> None:
    """Put back the readback values first (a normal profile only re-applies the
    controls it enables, so anything else the sweep touched would stay changed),
    then re-apply the user's profile so curves and the rest come back too."""
    if simulate:
        return
    params = (point or {}).get("params") or {}
    restored = False
    if params:
        try:
            mvolt.apply(mvolt.settings_args(params) + (["--xoc"] if point.get("xoc") else []))
            log(f"restored your previous settings: {params}")
            restored = True
        except Exception as exc:
            log(f"WARNING: restoring previous settings failed: {exc}")
    if point and point.get("release_cap"):
        try:
            mvolt.apply(["--return-to-percentage"])
            # It keeps the last cap's wattage as the percentage: set yours back.
            # Restore points from before this was recorded fall back to stock.
            pct = int(point.get("power_pct") or 100)
            mvolt.apply(["--power", str(pct)])
            log(f"released the power cap (back to percentage power limit, {pct} %)")
        except Exception as exc:
            log(f"WARNING: could not release the power cap: {exc}")
    if point and point.get("profile"):
        try:
            mvolt.apply_profile(point["profile"])
            log(f"re-applied your profile '{point['profile']}'")
            restored = True
        except Exception as exc:
            log(f"WARNING: could not re-apply profile '{point['profile']}': {exc}")
    if restored:
        return
    try:
        mvolt.apply(mvolt.settings_args(SAFE))
        log("restored stock offsets (your previous settings were not available)")
    except Exception as exc:
        log(f"WARNING: restore failed: {exc}")


# ---------------------------------------------------------------------------
# one trial
# ---------------------------------------------------------------------------

SIM_DELAY_S = float(__import__("os").environ.get("TUNER_SIM_DELAY", "2.0"))


def simulate_bench(params: dict[str, Any] | None = None
                   ) -> tuple[kombustor.BenchResult, dict[str, Any]]:
    """Dry-run benchmark modelled on the first real RTX 5080 study (64 runs), so
    a dry run of any stage behaves like the card did:
      * crash edge: core ~418 at REL 0, +0.8 MHz per mV of undervolt, a little
        lower with a bigger budget; probabilistic within ~+-20 MHz of it;
      * far past the edge (+90 MHz), or XBAR >= 650 / SYS >= 700: the real card
        froze the whole PC - reported here as a crash noting it;
      * watts: ~340 W natural draw, -0.6 W per mV of undervolt, +3 W per 500 MHz
        memory, held at the budget;
      * score (stock ~37,800): +356 per 10 W, +665 per 100 MHz core,
        +322 per 100 MHz XBAR, a little extra per mV of undervolt; +-400 noise.
      * core voltage demand (NOT measured yet - a guess so the sweep can be
        dry-run): crash edge around -45 mV, -0.5 W per mV deeper; where the
        budget binds, the saved watts turn into clocks (score)."""
    time.sleep(SIM_DELAY_S)
    p = params or {}
    core, mem = float(p.get("core") or 0), float(p.get("mem") or 0)
    rel, xbar = float(p.get("rel") or 0), float(p.get("xbar_offset") or 0)
    sys_off = float(p.get("sys_offset") or 0)
    cap = float(p.get("cap") or 396)
    vd = float(p.get("core_vdemand") or 0)
    if vd < 0 and random.random() < 1.0 / (1.0 + math.exp(-(-vd - 45) / 6.0)):
        res = kombustor.BenchResult(exit_code=1, elapsed_s=8.0, crash=True,
                                    note="sim crash (voltage demand)")
        return res, {"watts_avg": None, "samples": 0}
    edge50 = 418 - 0.8 * rel - 0.15 * (cap - 320)
    if xbar >= 650 or sys_off >= 700 or core > edge50 + 90:
        res = kombustor.BenchResult(exit_code=1, elapsed_s=5.0, crash=True,
                                    note="sim crash - the real card hard-froze in this region")
        return res, {"watts_avg": None, "samples": 0}
    if random.random() < 1.0 / (1.0 + math.exp(-(core - edge50) / 12.0)):
        res = kombustor.BenchResult(exit_code=1, elapsed_s=20.0, crash=True, note="sim crash")
        return res, {"watts_avg": None, "samples": 0}
    want = 340 + 0.6 * rel + 0.0065 * mem
    vd_gain = -vd * (4.0 if want >= cap else 1.0)   # clocks bought back under a binding cap
    want += 0.5 * vd
    watts = min(want, cap) + random.uniform(-1.5, 1.5)
    score = (37800 + 35.6 * (watts - 352) + 6.65 * core + 3.22 * xbar - 2.7 * rel
             + vd_gain + random.uniform(-400, 400))
    res = kombustor.BenchResult(exit_code=0, elapsed_s=62.0, score=int(score),
                                avg_fps=round(score / 60), min_fps=98, max_fps=103)
    stats = {"watts_avg": round(watts, 1), "watts_max": round(watts + 4, 1),
             "clk_avg": round(2700 + core * 0.5 - max(0.0, want - cap) * 3), "clk_max": 3000.0,
             "temp_max": 70.0, "samples": 120}
    return res, stats


def _status_after_bench(tries: int = 6, gap_s: float = 5.0) -> dict[str, Any] | None:
    """--status after a run; the driver may still be reconnecting after a reset."""
    for i in range(tries):
        try:
            return mvolt.status()
        except Exception:
            if i < tries - 1:
                time.sleep(gap_s)
    return None


def voltage_over_limit(st: dict[str, Any], limits: dict[str, Any] | None) -> str | None:
    """Why the applied config breaks one of the user's voltage limits, or None.

    Uses the effective voltage limit mVolt+ reports per rail after an apply
    (`nvvdd_max_mv` core, `msvdd_max_mv` fabric; REL/ALT/boost move them).
    Unreadable counts as a breach: a limit the tuner cannot check is not a limit.
    """
    for key, field, name in (("max_core_mv", "nvvdd_max_mv", "core"),
                             ("max_fabric_mv", "msvdd_max_mv", "fabric")):
        cap = (limits or {}).get(key)
        if cap is None:
            continue
        v = st.get(field)
        if v is None:
            return f"{name} voltage limit unreadable (no {field} in --status)"
        if float(v) > float(cap):
            return f"{name} voltage limit {v} mV > your max {cap:g} mV"
    return None


def needs_xoc(limits: dict[str, Any] | None, status: dict[str, Any] | None) -> bool:
    """mVolt+ only accepts voltage limits above the standard ceiling (1.15 V)
    with --xoc. Use it exactly when the user's own limits go above that."""
    rails = knobs.rail_info(status or {})
    for key, rail in (("max_core_mv", "core"), ("max_fabric_mv", "fabric")):
        cap = (limits or {}).get(key)
        if cap is not None and float(cap) > float(rails[rail]["standard_max_mv"]):
            return True
    return False


def run_trial(journal: state.Journal, hb: state.Heartbeat, params: dict[str, Any],
              boot: str, simulate: bool, bench: dict[str, Any] | None = None,
              limits: dict[str, Any] | None = None,
              revert: Any = None, reference: bool = False, xoc: bool = False) -> dict[str, Any]:
    """Apply, check the voltage limit, benchmark under the watt limit, verify, record.

    `revert` is called to take the settings back immediately when an applied
    config breaks the voltage limit, before any load runs. `reference` marks the
    baseline (your own settings): it is measured even if its voltage limit is
    above yours, because the tuner did not raise it - it is the comparison point.
    """
    bench = bench or {}
    limits = limits or {}
    max_watts = limits.get("max_watts")
    # When the GPU itself holds the limit (cap / power %), allow measurement
    # noise and brief overshoot before calling it a breach.
    enforced = max_watts is not None and ("cap" in params or "power" in params)
    kill_at = max_watts * 1.05 if enforced else max_watts
    avg_over = max_watts * 1.02 if enforced else max_watts
    timeout_s = int(bench.get("timeout_s") or config.BENCH_TIMEOUT_S)
    tid = journal.begin_trial(params, boot, limits)
    hb.trial = tid
    hb.enter("applying", 180)
    log(f"trial {tid}{' (baseline reference)' if reference else ''}: {params}")

    t_apply = time.time()
    try:
        if not simulate:
            st_applied = mvolt.apply_params(params, log=log, xoc=xoc)
            why = voltage_over_limit(st_applied, limits)
            if why and reference:
                log(f"baseline: {why} - measured anyway as the reference point")
                why = None
            if why:
                if revert:
                    revert()
                payload = {"status": "over_voltage_limit", "note": why + " - not benchmarked"}
                journal.finish_trial(tid, payload)
                journal.write_live(_live_path(), running=True)
                log(f"trial {tid}: over_voltage_limit ({why}); reverted before any load")
                return payload
        journal.mark_running(tid)
        hb.enter("benchmark", timeout_s + 120)

        t0 = time.time()
        if simulate:
            res, stats = simulate_bench(params)
        else:
            runner = kombustor
            if (bench.get("tool") or "kombustor") == "gravitymark":
                from . import gravitymark as runner
            res, stats = runner.run_benchmark(timeout_s, bench.get("args"),
                                              int(bench.get("nvsmi_index") or 0),
                                              watt_limit=kill_at,
                                              stop_check=state.stop_requested)

        status = "crash" if res.crash else "ok"
        note = res.note or ""
        score = res.score
        if res.over_limit is not None:
            status = "over_power_limit"
        elif (status == "ok" and max_watts is not None
              and stats.get("watts_avg") is not None and stats["watts_avg"] > avg_over):
            # Completed, but the whole-run average still broke the limit.
            status = "over_power_limit"
            note = f"average {stats['watts_avg']} W > limit {max_watts:g} W"

        # A driver reset clears applied tuning and mVolt+ does not reapply it, so
        # the rest of the run happened at different settings. Re-read and compare.
        if not simulate:
            hb.enter("verifying", 120)
            st = _status_after_bench()
            drift = knobs.verify(params, st) if st is not None else ["--status unavailable"]
            resets = kombustor.driver_resets_since(t0)
            if drift or resets:
                why = []
                if resets:
                    why.append(f"{resets} driver reset event(s) in the System log")
                if drift:
                    why.append("settings changed during the run: " + "; ".join(drift))
                if score is not None:
                    why.append(f"partial score {score} discarded")
                status, score = "driver_reset", None
                note = "; ".join(why + ([note] if note else []))

        # A score far below what its clock should give: the GPU was not fully
        # loaded (the window lost focus, or another app shared the GPU). Not a
        # result; the run stops so it is not repeated while you use the PC.
        if status == "ok" and not reference and not state.is_stock(params):
            share = state.underloaded(score, stats.get("clk_avg"),
                                      state.stock_ratio(journal.all_trials(), boot))
            if share is not None:
                payload = {"status": "aborted", "score": None,
                           "elapsed_s": round(res.elapsed_s, 1), **stats,
                           "note": f"under-loaded: {score} pts at {stats.get('clk_avg')} MHz is "
                                   f"{share:.0%} of stock's score per MHz - the benchmark lost "
                                   "focus or shared the GPU"}
                journal.finish_trial(tid, payload)
                journal.write_live(_live_path(), running=True)
                raise kombustor.GPUBusy(
                    f"trial {tid} was under-loaded ({share:.0%} of stock's score per MHz): "
                    "the benchmark window lost focus or another app used the GPU. Leave the PC "
                    "alone while it runs, then press Start - the study resumes where it stopped.")

        payload = {
            "status": status,
            "score": score,
            "avg_fps": res.avg_fps if score is not None else None,
            "elapsed_s": round(res.elapsed_s, 1),
            "note": note or None,
            **stats,
        }
        journal.finish_trial(tid, payload)
        journal.write_live(_live_path(), running=True)
        log(f"trial {tid}: {status} score={score} W={stats.get('watts_avg')} "
            f"clk={stats.get('clk_avg')}{' (' + note + ')' if note else ''}")
        return payload
    except KeyboardInterrupt:
        journal.finish_trial(tid, {"status": "cancelled", "note": "interrupted by user"})
        raise
    except Exception as exc:
        # "apply did not stick" with everything reading back 0 means the driver
        # reset WHILE the settings were going on: that is instability, not a
        # neutral failure to retry (6 such cases in the first RTX 5080 study).
        msg = str(exc)
        resets = None
        if not simulate and "did not stick" in msg:
            resets = kombustor.driver_resets_since(t_apply)
        if not simulate and "did not stick" in msg and (
                resets or "core: reported 0MHz" in msg or "core: no readback" in msg):
            payload = {"status": "driver_reset",
                       "note": (f"driver reset during apply ({resets or 0} event(s) logged): "
                                + msg)[:300]}
            journal.finish_trial(tid, payload)
            journal.write_live(_live_path(), running=True)
            log(f"trial {tid}: driver_reset during apply ({msg[:120]})")
            return payload
        journal.finish_trial(tid, {"status": "aborted", "note": msg[:300]})
        log(f"trial {tid}: ABORTED {exc}")
        raise
    finally:
        hb.enter("idle", 600)


# Dry runs keep their own journal and output files so the pages never show them.
_DRY = {"on": False}


def _live_path():
    return config.DATA / "live-dryrun.json" if _DRY["on"] else config.LIVE_JSON


def write_importance(journal: state.Journal) -> dict[str, Any]:
    """Effect sizes + crash model (always), Optuna fANOVA and eta^2 (secondary;
    Optuna only if installed) over the whole journal -> data/importance.json."""
    from . import analysis, importance, optuna_report
    trials = journal.scored_trials()
    only = {k for k in (journal.get_meta("swept_knobs", "") or "").split(",") if k} or None
    rep = {"generated_utc": state.utcnow(),
           "analysis": analysis.summary(trials, only),
           "optuna": optuna_report.report(trials, only),
           "eta2": importance.knob_effects(trials, only=only)}
    state._atomic_write(config.DATA / ("importance-dryrun.json" if _DRY["on"] else "importance.json"),
                        json.dumps(rep, indent=2))
    return rep


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fmt_limits(lim: dict[str, Any] | None) -> str:
    lim = lim or {}
    w, mv, fv = lim.get("max_watts"), lim.get("max_core_mv"), lim.get("max_fabric_mv")
    return (f"max power {f'{w:g} W' if w is not None else 'none'}, "
            f"max core voltage {f'{mv:g} mV' if mv is not None else 'none'}, "
            f"max fabric voltage {f'{fv:g} mV' if fv is not None else 'none'}")


_LIST_OPTS = ("--core-vals", "--rels", "--mems", "--caps", "--max-watts", "--max-mv",
              "--max-fabric-mv")


def _glue_negative_lists(argv: list[str]) -> list[str]:
    """argparse reads `--core-vals -50,0,25` as a missing value followed by an
    unknown option `-50,0,25`. Rewrite such pairs to `--core-vals=-50,0,25`."""
    out: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if a in _LIST_OPTS and nxt is not None and nxt.startswith("-") and \
                nxt[1:2].isdigit():
            out.append(f"{a}={nxt}")
            i += 2
            continue
        out.append(a)
        i += 1
    return out


def _ints(s: str) -> list[int]:
    return [int(x) for x in s.split(",") if x.strip()]


def _floats(s: str) -> list[float]:
    return [float(x) for x in s.split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tuner")
    ap.add_argument("--trials", type=int, default=3, help="how many new trials to run")
    ap.add_argument("--simulate", action="store_true", help="no GPU writes, fake results")
    ap.add_argument("--baseline", action="store_true", help="measure the baseline before the grid")
    ap.add_argument("--max-core", type=int, default=config.DEFAULT_SWEEP_CORE_MHZ,
                    help="top of the core sweep (a range, not a limit), relative to "
                         "--baseline-profile if given")
    ap.add_argument("--cores", type=int, default=3, help="number of core offset steps (0..max)")
    ap.add_argument("--mems", type=str, default="0")
    ap.add_argument("--caps", type=str, default="",
                    help="comma list of watt caps to sweep (default: leave the power limit alone)")
    ap.add_argument("--list", action="store_true", help="print the grid and exit (no GPU writes)")
    ap.add_argument("--recovery", choices=["auto", "hardmode"], default="auto",
                    help="auto (default): rely on mVolt+/the driver recovering after a "
                         "crash. hardmode: arm the external watchdog so a stale "
                         "heartbeat reboots the machine (needs scripts/watchdog.ps1)")
    ap.add_argument("--check", action="store_true",
                    help="verify the install and this GPU, then exit")
    ap.add_argument("--setup", action="store_true",
                    help="interactively choose which knobs to sweep, save to state/config.json")
    ap.add_argument("--search", choices=["explore", "edge", "confirm", "vdemand", "ab"], default=None,
                    help="run a saved smart-search stage: explore (wide spread, to learn what "
                         "matters), edge (find the highest stable core per power budget and "
                         "undervolt), confirm (re-run the best configs several times) or vdemand "
                         "(step the core voltage-demand offset down at confirmed configs)")
    ap.add_argument("--from-config", action="store_true",
                    help="run the grid saved by --setup instead of the CLI ranges")
    ap.add_argument("--yes", action="store_true",
                    help="with --setup: accept every default without prompting")
    ap.add_argument("--baseline-profile", type=str, default="",
                    help="sweep around an existing mVolt+ profile "
                         "('active' = whatever mVolt+ is currently using)")
    ap.add_argument("--serve", action="store_true",
                    help="serve the web UI + local API (writes profiles on click)")
    ap.add_argument("--port", type=int, default=8712)
    ap.add_argument("--save-picks", action="store_true",
                    help="write one mVolt+ profile per recommended config, then exit")
    ap.add_argument("--close-app", action="store_true",
                    help="with --save-picks: close mVolt+ first (it rewrites the profile file)")
    ap.add_argument("--survey", action="store_true",
                    help="print which knobs this GPU exposes, then exit")
    ap.add_argument("--reprobe", action="store_true",
                    help="re-run the writability probe instead of using the cache")
    ap.add_argument("--core-vals", type=str, default="",
                    help="explicit comma list of core offsets, e.g. 0,150")
    ap.add_argument("--rels", type=str, default="0",
                    help="comma list of NVVDD REL offsets in mV, e.g. 0,-25,-50")
    ap.add_argument("--retry-failed", action="store_true",
                    help="do not skip grid points ruled out by earlier crashes/driver resets")
    ap.add_argument("--limits", action="store_true",
                    help="(re)set your max board power and max core/fabric voltage, then "
                         "exit. These are the only ceilings the tuner respects; they may be "
                         "above stock")
    ap.add_argument("--max-watts", type=str, default=None,
                    help="set the max board power in W (or 'none') and save it")
    ap.add_argument("--max-mv", type=str, default=None,
                    help="set the max core (NVVDD) voltage limit in mV (or 'none') and save it")
    ap.add_argument("--max-fabric-mv", type=str, default=None,
                    help="set the max fabric (MSVDD) voltage limit in mV (or 'none') and save it")
    ap.add_argument("--importance", action="store_true",
                    help="print which knobs moved score, watts, efficiency and stability "
                         "(Optuna fANOVA + eta^2), write data/importance.json, then exit")
    ap.add_argument("--chaos-core", action="store_true", help=argparse.SUPPRESS)
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    args = ap.parse_args(_glue_negative_lists(raw_argv))

    from . import capability, setup as _setup

    if args.check:
        from . import preflight
        rep = preflight.run_checks()
        print("Preflight:")
        print(rep.render())
        print()
        if not rep.ok:
            print(f"{len(rep.failures())} problem(s) must be fixed before tuning.")
            return 2
        st = mvolt.status()
        if _setup.load_limits() is None and sys.stdin.isatty():
            # Limits come first - before the write-back probe touches anything.
            lim = _setup.ask_limits(st, print)
            _setup.save_limits(lim)
            print(f"limits saved: {_fmt_limits(lim)}\n")
        wmap = capability.writable_map(args.reprobe, status=st)
        print("This GPU exposes:")
        print()
        print(knobs.describe(st, wmap))
        print()
        print("sweepable now:", ", ".join(k.key for k in knobs.survey(st, wmap) if k.available))
        return 0

    if args.setup:
        from . import preflight
        preflight.require_ok()
        st = mvolt.status()
        p = _setup.plan(st, assume_yes=args.yes, log=print,
                        writable=capability.writable_map(args.reprobe, status=st))
        _setup.save(p)
        n = len(_setup.grid_from_plan(p))
        enabled = [k for k, v in p["knobs"].items() if v.get("enabled")]
        print()
        print(f"saved {config.CONFIG_FILE}")
        print(f"  sweeping: {', '.join(enabled) if enabled else '(nothing)'}")
        print(f"  grid size: {n} combination(s)")
        return 0

    # ---- the user's limits: the only ceilings ---------------------------
    given = {k: v for k, v in (("max_watts", args.max_watts), ("max_core_mv", args.max_mv),
                               ("max_fabric_mv", args.max_fabric_mv)) if v is not None}
    if given:
        lim = dict(_setup.load_limits() or {k: None for k in _setup.LIMIT_KEYS})
        try:
            st_lim = None if args.simulate else mvolt.status()
        except Exception:
            st_lim = None
        try:
            parsed = _setup.validate_limits(given, st_lim)
        except ValueError as exc:
            log(f"FATAL: {exc}")
            return 2
        lim.update({k: parsed[k] for k in given})
        _setup.save_limits(lim)
        log(f"limits saved: {_fmt_limits(lim)}")

    if args.limits:
        lim = _setup.ask_limits(mvolt.status(), print)
        _setup.save_limits(lim)
        print(f"\nlimits saved: {_fmt_limits(lim)}")
        return 0

    if args.importance:
        from . import analysis
        rep = write_importance(state.Journal())
        print(analysis.render(rep["analysis"]))
        if rep["optuna"].get("available"):
            print("\n(secondary) Optuna fANOVA importance shares are in importance.json")
        print(f"\nwritten to {config.DATA / 'importance.json'} "
              "(also at /api/importance and web/importance.html with --serve)")
        return 0

    if args.serve:
        from . import serve as _serve
        _serve.serve(args.port)
        return 0

    if args.save_picks:
        from . import profiles as _profiles
        if _profiles.app_running():
            if not args.close_app:
                log("FATAL: mVolt+ is running and would overwrite the profile file.")
                log("       re-run with --close-app (or close mVolt+ yourself)")
                return 6
            log("closing mVolt+ ...")
            if not _profiles.close_app():
                log("FATAL: could not close mVolt+")
                return 6

        picks = [pk for pk in _profiles.pick_configs(state.Journal().scored_trials())
                 if pk[1]]  # the stock reference has no settings to save
        if not picks:
            log("no scored trials yet - run the tuner first")
            return 7

        written = 0
        for i, (label, params, stats) in enumerate(picks, 1):
            name = f"tuner-best-{i}"
            pretty = " ".join(f"{k}={v}" for k, v in params.items() if v)
            try:
                info = _profiles.write_profile(name, params)
            except _profiles.ProfileError as exc:
                log(f"profile '{name}' ({label}) NOT written: {exc}")
                continue
            written += 1
            log(f"profile '{name}' ({label}): {pretty} "
                f"[score={stats.get('score')} W={stats.get('watts')} n={stats.get('n')}] "
                f"enabled={','.join(info['enabled'])} rev={info['revision']} -> {info['path']}")
        return 0 if written else 7

    if args.survey:
        st = mvolt.status()
        wmap = capability.writable_map(args.reprobe, status=st)
        print(knobs.describe(st, wmap))
        print()
        print("sweepable now:", ", ".join(k.key for k in knobs.survey(st, wmap) if k.available))
        return 0

    # ---- the grid -------------------------------------------------------
    saved = _setup.load()
    bench = dict((saved or {}).get("bench") or {})

    edge_plan: dict[str, Any] | None = None
    vd_plan: dict[str, Any] | None = None
    link_ref: dict[str, float] = {}    # manual grid: REL/ALT pair ALT is linked against
    search_cfg = (saved or {}).get("search") or {}
    if args.search == "edge":
        # Each next config comes from the edge search, rebuilt from the journal.
        edge_plan = (search_cfg.get("edge") or {}).get("plan")
        if not edge_plan:
            log("FATAL: no edge-search plan saved (config.json search.edge.plan)")
            return 5
        grid = []
        log(f"edge search: budgets {edge_plan['budgets']} W x REL {edge_plan['rels']} mV, "
            f"core {edge_plan['core']['lo']:g}..{edge_plan['core']['hi']:g} "
            f"step {edge_plan['core']['step']:g}")
    elif args.search == "ab":
        # A fixed sequence, run exactly as listed - repeats of configs that
        # already have results are the point (it tests for a state change).
        pts = (search_cfg.get("ab") or {}).get("points")
        if not pts:
            log("FATAL: no A/B sequence saved (config.json search.ab.points)")
            return 5
        grid = [dict(p) for p in pts]
        log(f"A/B sequence: {(search_cfg.get('ab') or {}).get('sequence')} ({len(grid)} runs)")
    elif args.search == "vdemand":
        vd_plan = (search_cfg.get("vdemand") or {}).get("plan")
        if not vd_plan:
            log("FATAL: no voltage-demand plan saved (config.json search.vdemand.plan)")
            return 5
        grid = []
        from . import vdemand as _vdk
        log(f"{_vdk.SWEEP_KEYS[vd_plan.get('key') or _vdk.KEY]} sweep: "
            f"{len(vd_plan['bases'])} config(s) x "
            f"{', '.join(f'{v:g}' for v in vd_plan['values'])}, "
            f"{vd_plan['repeats']} run(s) each; a lane stops at its first crash")
    elif args.search == "confirm":
        # The same configs several times each (interleaved) - repeats are the point.
        pts = (search_cfg.get("confirm") or {}).get("points")
        if not pts:
            log("FATAL: no confirmation plan saved (config.json search.confirm.points)")
            return 5
        grid = [dict(p) for p in pts]
        log(f"confirm: {len(grid)} runs")
    elif args.search == "explore":
        # Absolute Latin-hypercube points generated by search.lhs_points and saved
        # under config "search.explore" (the web UI writes them); resume-safe.
        pts = (((saved or {}).get("search") or {}).get("explore") or {}).get("points")
        if not pts:
            log("FATAL: no explore plan saved (config.json search.explore.points)")
            return 5
        grid = [dict(p) for p in pts]
        log(f"explore pass: {len(grid)} spread-out configs")
    elif args.from_config:
        if not saved:
            log("FATAL: no saved setup; run `python -m tuner --setup` first")
            return 5
        grid = _setup.grid_from_plan(saved)
        if not grid:
            log("FATAL: the saved setup has no enabled knobs")
            return 5
        log(f"grid from state/config.json: {len(grid)} combination(s)")
    else:
        if args.core_vals.strip():
            core_vals = _ints(args.core_vals)
        else:
            step = max(1, args.max_core // max(1, args.cores - 1))
            core_vals = [i * step for i in range(args.cores)]
        grid = build_grid(core_vals, _ints(args.mems), _floats(args.caps),
                          _ints(args.rels) or [0])

    # ---- baseline: an existing mVolt+ profile the user already runs -----
    if args.baseline_profile:
        from . import profiles as _profiles
        if args.baseline_profile == "active":
            prof = _profiles.active_profile()
        else:
            prof = next((p for p in _profiles.read_profiles()
                         if p["id"] == args.baseline_profile
                         or p["name"] == args.baseline_profile), None)
        if not prof:
            log(f"FATAL: baseline profile '{args.baseline_profile}' not found")
            return 8
        base = {k: v for k, v in (prof.get("params") or {}).items() if v}
        log(f"baseline profile '{prof['name']}' ({prof.get('source', 'named')}): "
            f"{base or 'all offsets zero'} - grid is applied on top of it")
        unknown = prof.get("unmodelled_controls") or []
        if unknown:
            log(f"WARNING: profile enables controls the knob registry does not model: "
                f"{', '.join(unknown)} - these will not be swept or reported")
        grid = apply_base(grid, base)
        link_ref = knobs.link_reference(prof.get("params") or {})

    try:
        check_grid(grid)
    except ValueError as exc:
        log(f"FATAL: {exc}")
        return 4

    if args.list:
        if edge_plan is not None:
            from . import edge as _edge
            print("edge search: next config is chosen from results; at most "
                  f"~{_edge.estimate_runs(edge_plan, state.Journal().scored_trials())} runs. Holding", json.dumps(edge_plan["base"]))
        for i, p in enumerate(grid, 1):
            print(i, p)
        return 0

    if not args.simulate and not mvolt.is_admin():
        log("FATAL: not elevated. mVolt+ rejects all tuning writes without admin.")
        return 2

    # Dry runs get their own journal: their fake results must never mix with
    # real measurements (a dry run once added 17 fake trials to the real one).
    _DRY["on"] = bool(args.simulate)
    tool = config.bench_tool()
    real_db, dry_db = config.journal_db(tool), config.journal_db(tool, dry=True)
    log(f"benchmark: {config.BENCH_TOOLS[tool]} (journal {real_db.name})")
    if args.simulate and not dry_db.exists() and real_db.exists():
        # Start dry runs from a COPY of the real results, so the edge search's
        # model and the crash guard behave as they would for real. Nothing is
        # ever written back. Delete state/journal-dryrun.db to start over.
        import shutil
        shutil.copy2(real_db, dry_db)
        log(f"dry run: started from a copy of your real journal (state/{dry_db.name})")
    journal = state.Journal(dry_db if args.simulate else real_db)
    # Hard freezes and driver resets are hardware events: the other
    # benchmarks' journals count too (read once; they do not change mid-run).
    other_trials = state.hardware_trials(exclude=real_db)
    boot = state.boot_id()
    orphans = journal.reclaim_orphans(boot)
    from . import crashes
    for o in orphans:
        log(f"reclaimed orphaned trial {o['id']} (was {o['was']}, {o['cause']})")
        if o["cause"] == "host reboot":
            # The PC went down mid-trial: a hard freeze. Record what the driver
            # logged before it, and which settings were beyond anything clean.
            n = kombustor.driver_errors_between(o["started_utc"], boot) if boot != "unknown" else None
            if n is not None:
                journal.append_note(o["id"], f"; {n} NVIDIA driver error(s) logged before the reboot")
            kind = "on apply" if o["was"] == "applying" else "under load"
            cul = crashes.implicated(o["params"], crashes.clean_extremes(journal.all_trials()))
            log(f"HARD FREEZE {kind} in trial {o['id']}: likely "
                + (", ".join(f"{k} {c['value']:+g} (highest clean "
                             f"{'none' if c['highest_clean'] is None else format(c['highest_clean'], '+g')})"
                             for k, c in cul.items()) or "no single setting stands out"))
    guard_rules = [] if args.retry_failed else (
        crashes.rules(journal.all_trials()) + crashes.rules(other_trials))
    for r in guard_rules:
        log("hard-crash guard: skipping configs where " + crashes.describe_rule(r))

    st0: dict[str, Any] | None = None
    wmap: dict[str, bool | None] | None = None
    xoc = False
    limits = _setup.load_limits()
    if not args.simulate:
        st0 = mvolt.status()
        if limits is None:
            # First launch: the limits are the only ceilings, so ask before any write.
            if not sys.stdin.isatty():
                log("FATAL: no limits set yet. Run interactively once, or set them with "
                    "--max-watts N|none --max-mv N|none")
                return 9
            limits = _setup.ask_limits(st0, print)
            _setup.save_limits(limits)
        log(f"limits: {_fmt_limits(limits)}")
        try:
            check_grid(grid, st0)  # the driver's own ranges, now that we have them
        except ValueError as exc:
            log(f"FATAL: {exc}")
            return 4
        wmap = capability.writable_map(args.reprobe, status=st0)
        dropped = sorted({k for p in grid for k in p if wmap.get(k) is False})
        if dropped:
            for p in grid:
                for k in dropped:
                    p.pop(k, None)
            log(f"not writable on this GPU, removed from the grid: {', '.join(dropped)}")
        if args.search is None:
            # Manual grid: move ALT with REL (explore/edge points come pre-linked).
            if not link_ref:
                link_ref = knobs.link_reference(knobs.snapshot(st0, ["rel", "msvdd_rel"]))
            for p in grid:
                knobs.link_rails(p, link_ref)
        fill_companions(grid, knobs.companions_from_status(st0))
        log("power limit: " + enforce_power_limit(grid, limits, st0, wmap))
        xoc = needs_xoc(limits, st0)
        if xoc:
            log("your voltage limits are above 1.15 V: applying in mVolt+ XOC range")

    if args.simulate and limits:
        log("power limit (dry run): " + enforce_power_limit(grid, limits, None, None))

    # The user's settings from BEFORE the study started. Persisted so a run that
    # resumes after a reboot or driver reset still restores the original state,
    # not the stock values the reset left behind.
    restore_point: dict[str, Any] | None = None
    touched = {k for p in grid for k in p} | set(SAFE)
    if edge_plan is not None:
        touched |= set(edge_plan["base"]) | {"core", "rel", "cap"}
    if vd_plan is not None:
        touched |= ({k for b in vd_plan["bases"] for k in b["params"]}
                    | {vd_plan.get("key") or "core_vdemand"})
    raw = journal.get_meta("restore_point")
    if raw:
        restore_point = json.loads(raw)
        # Captured by an earlier stage, which may have touched fewer controls:
        # anything this stage adds goes back to neutral, not left applied.
        added = extend_restore_point(restore_point, touched)
        if added:
            log(f"restore point: also resetting {', '.join(added)} at the end")
            journal.set_meta("restore_point", json.dumps(restore_point))
    elif st0 is not None:
        restore_point = capture_restore_point(st0, touched)
        journal.set_meta("restore_point", json.dumps(restore_point))
    if restore_point:
        log(f"will restore at the end: "
            f"{restore_point.get('profile') or restore_point.get('params')}")

    swept_now = sorted({k for p in grid for k in p if len({q.get(k) for q in grid}) > 1}
                       | ({"core", "rel", "cap"} if edge_plan is not None else set()))
    if swept_now:
        prev = {k for k in (journal.get_meta("swept_knobs", "") or "").split(",") if k}
        merged = sorted(prev | set(swept_now))
        journal.set_meta("swept_knobs", ",".join(merged))
        log(f"swept knobs: {','.join(merged)}")

    stalled = state.note_boot(boot, journal.finished_count())
    done_keys = journal.finished_param_keys(limits)
    failures = [] if args.retry_failed else journal.unstable_params()
    log(f"boot={boot}  completed={journal.completed_count()}  "
        f"known unstable configs={len(failures)}  stalled boots={stalled.get('stalled_boots')}")

    hardmode = args.recovery == "hardmode"
    if hardmode and state.boot_stalled(stalled):
        log("FATAL: too many consecutive boots without progress. Stopping for manual review.")
        journal.event("abort_boot_loop", {"stalled_boots": stalled.get("stalled_boots")})
        return 3

    if not args.simulate:
        # Keep run-args in sync so the logon task resumes THIS run, not an old one.
        try:
            (config.DATA / "run-args.json").write_text(json.dumps(raw_argv), encoding="utf-8")
        except Exception as exc:
            log(f"could not write run-args.json: {exc}")

    if hardmode:
        state.arm()
        log("hardmode: armed (external watchdog may reboot on stale heartbeat)")
    else:
        log("recovery=auto: relying on the driver/mVolt+ to recover after a crash")

    journal.event("run_start", {"boot": boot, "trials": args.trials, "grid": grid,
                                "recovery": args.recovery})

    state.clear_stop()
    hb = state.Heartbeat()
    hb.start()
    rc = 0
    clean_finish = False
    try:
        if args.chaos_core and not journal.get_meta("chaos_done"):
            journal.set_meta("chaos_done", state.utcnow())
            journal.event("chaos_applied", {})
            log("CHAOS: applying core +1000 - expect a hang or driver reset")
            try:
                mvolt.apply(["--core", "1000"])
                mvolt.apply(["--core", "0"])
            except Exception as exc:
                log(f"CHAOS: apply did not complete: {exc}")

        def revert() -> None:
            restore(restore_point, args.simulate)

        # Session anchor: scores are only comparable within a Windows session
        # (stock moved 37.8k -> 49.3k between boots on an RTX 5080), so every
        # boot gets one stock run before anything else.
        rows = [{**r, "params": r["params"]} for r in journal.all_trials()]
        if not state.has_anchor(rows, boot):
            prev = [r["params"] for r in rows if r.get("status") == "ok"
                    and state.is_stock(r.get("params") or {})]
            anchor = {k: v for k, v in (prev[-1] if prev else SAFE).items() if k != "cap"}
            anchor.update({k: 0 for k in ("core", "mem", "rel", "vmin", "alt",
                                          "xbar_offset", "sys_offset")})
            anchor.pop("boost_lock", None)
            enforce_power_limit([anchor], limits, st0, wmap)
            log("session anchor: measuring stock once for this boot "
                "(scores are compared against it)")
            run_trial(journal, hb, anchor, boot, args.simulate, bench,
                      limits, revert, reference=True, xoc=xoc)

        if args.baseline:
            # Your own settings, measured under the same power budget as the grid.
            base_point = dict((restore_point or {}).get("params") or SAFE)
            enforce_power_limit([base_point], limits, st0, wmap)
            if not state.is_stock(base_point) or state.has_anchor(journal.all_trials(), boot) is False:
                run_trial(journal, hb, base_point, boot, args.simulate, bench,
                          limits, revert, reference=True, xoc=xoc)

        done = 0
        consecutive_failures = 0
        skipped: set[str] = set()
        comps = knobs.companions_from_status(st0) if st0 is not None else {}

        def points():
            """Grid / explore / confirm: the fixed list. Edge: the next probe of
            the lane with the widest gap, rebuilt from the journal each time."""
            if vd_plan is not None:
                from . import vdemand as _vd
                misses = 0
                while misses < 25:
                    p = _vd.next_point(journal.scored_trials(), vd_plan, skipped)
                    if p is None:
                        log("sweep: every lane is done")
                        return
                    enforce_power_limit([p], limits, st0, wmap)
                    if st0 is not None:
                        fill_companions([p], comps)
                        try:
                            check_grid([p], st0)
                        except ValueError as exc:
                            skipped.add(state.param_key(p))
                            misses += 1
                            log(f"sweep: skipping {p}: {exc}")
                            continue
                    misses = 0
                    yield p
                return
            if edge_plan is None:
                yield from grid
                return
            from . import edge as _edge
            misses = 0
            while misses < 25:
                p = _edge.next_point(journal.scored_trials(), edge_plan, done_keys | skipped)
                if p is None:
                    log("edge search: every lane has found its edge")
                    return
                enforce_power_limit([p], limits, st0, wmap)
                if st0 is not None:
                    fill_companions([p], comps)
                    try:
                        check_grid([p], st0)
                    except ValueError as exc:
                        skipped.add(state.param_key(p))
                        misses += 1
                        log(f"edge search: skipping {p}: {exc}")
                        continue
                misses = 0
                yield p

        # How many results each config already has: confirm runs the same config
        # several times, and run k of a config is done once k results exist.
        have = journal.finished_counts(limits)
        occurrence: dict[str, int] = {}

        def resets_this_boot() -> int:
            return sum(1 for t in journal.all_trials() + other_trials
                       if t.get("boot_id") == boot and t.get("status") in UNSTABLE)

        if not args.simulate and resets_this_boot() >= config.MAX_RESETS_PER_BOOT:
            log(f"the driver already reset {resets_this_boot()} times since this boot - "
                "restart Windows first (repeated resets have preceded hard freezes), then press Start")
            raise RuntimeError("driver reset budget for this boot is used up - restart Windows, then Start again")

        for i, params in enumerate(points()):
            if done >= args.trials:
                break
            if state.stop_requested():
                log("stop requested - finishing")
                raise KeyboardInterrupt("stop requested")
            key = state.param_key(params)
            occurrence[key] = occurrence.get(key, 0) + 1
            # The vdemand planner counts repeats from the journal itself.
            if vd_plan is None and args.search != "ab" and have.get(key, 0) >= occurrence[key]:
                if edge_plan is not None:
                    skipped.add(key)
                continue
            # A config that matches a past hard freeze is never applied: a freeze
            # needs a manual reset and stops the whole run.
            hard = crashes.guard(params, guard_rules)
            if hard is not None:
                skipped.add(key)
                log(f"skip {params}: hard-crash guard - {crashes.describe_rule(hard)}")
                continue
            # The edge search applies the same monotone logic per lane itself.
            why = None if (edge_plan is not None or vd_plan is not None)                 else ruled_out(params, failures)
            if why is not None:
                skipped.add(key)
                log(f"skip {params}: at least as aggressive as unstable {why}")
                continue
            try:
                result = run_trial(journal, hb, params, boot, args.simulate, bench,
                                   limits, revert, xoc=xoc)
                done += 1
                done_keys.add(key)
                have[key] = have.get(key, 0) + 1
                consecutive_failures = 0
                journal.set_meta("next_index", str(i + 1))
                if result["status"] in UNSTABLE:
                    failures.append(params)
                    n = resets_this_boot()
                    if not args.simulate and n >= config.MAX_RESETS_PER_BOOT:
                        log(f"STOPPING: {n} driver resets since this boot. Hard freezes on this card "
                            "came after repeated resets, so the run stops here. Restart Windows, "
                            "then press Start - the search resumes where it left off.")
                        break
                    if not args.simulate:
                        log(f"driver reset {n} of {config.MAX_RESETS_PER_BOOT} allowed this boot - "
                            f"idling {config.RESET_COOLDOWN_S} s before the next config")
                        time.sleep(config.RESET_COOLDOWN_S)
            except KeyboardInterrupt:
                raise
            except (mvolt.DriverIgnoresRails, kombustor.GPUBusy) as exc:
                # Every config would fail the same way: stop now.
                log(f"STOPPING: {exc}")
                break
            except Exception as exc:
                # An apply the driver rejected is not evidence of instability:
                # the point stays un-done and is retried on the next run.
                consecutive_failures += 1
                log(f"grid point {i} failed ({consecutive_failures} in a row): {exc}")
                if consecutive_failures >= 3:
                    log("three consecutive trial failures - stopping")
                    break
        if vd_plan is not None:
            from . import vdemand as _vd
            clean_finish = _vd.next_point(journal.scored_trials(), vd_plan) is None
        elif edge_plan is not None:
            # Done when every lane has found its edge - possibly from earlier
            # runs alone, with no new run needed.
            from . import edge as _edge_done
            clean_finish = (_edge_done.next_point(journal.scored_trials(), edge_plan) is None
                            or done >= args.trials)
        else:
            clean_finish = all(state.param_key(p) in done_keys or ruled_out(p, failures)
                               for p in grid)
    except KeyboardInterrupt:
        log("interrupted by user")
        rc = 130
    except Exception as exc:
        log(f"FATAL: {exc}")
        rc = 1
    finally:
        hb.enter("shutting_down", 120)
        restore(restore_point, args.simulate)
        hb.stop()
        if hardmode:
            state.disarm()
        if clean_finish and rc == 0:
            # Study complete: the next study captures a fresh restore point.
            journal.delete_meta("restore_point")
        try:
            journal.write_live(_live_path(), running=False)
        except Exception:
            pass
        try:
            rep = write_importance(journal)
            clear = [k for k in rep["analysis"]["effects"].get("knobs") or []
                     if (k.get("score") or {}).get("clear")]
            if clear:
                log("clear effects on score: " + ", ".join(
                    f"{k['knob']} {k['score']['per_unit']:+g} pts per {k['per']:g} {k['unit']}"
                    for k in clear[:4]) + "  (python -m tuner --importance)")
            if rep["analysis"]["crash"].get("tradeoff"):
                log("stability: " + rep["analysis"]["crash"]["tradeoff"])
        except Exception as exc:
            log(f"importance report skipped: {exc}")
        journal.event("run_end", {"rc": rc})
        log(f"{'disarmed' if hardmode else 'done'}; completed={journal.completed_count()}")

    # Run queue: only a clean finish moves on (never after Stop, the reset
    # stop, or a failure). The next stage is planned with this code and
    # started as a new process, which keeps this one's elevation.
    if clean_finish and rc == 0 and not state.stop_requested():
        try:
            from . import pipeline
            pipeline.run_next(log)
        except Exception as exc:
            log(f"run queue: could not start the next stage: {exc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
