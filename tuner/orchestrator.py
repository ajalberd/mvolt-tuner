"""Trial orchestrator: apply -> benchmark -> record, with crash recovery.

Run elevated:  python -m tuner --trials 20
Test safely:   python -m tuner --simulate --trials 5
"""
from __future__ import annotations

import argparse
import itertools
import random
import sys
import time
from typing import Any

from . import config, kombustor, mvolt, state

SAFE = {"core": 0, "mem": 0, "rel": 0, "alt": 0, "boost_lock": False}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def restore_safe(simulate: bool) -> None:
    if simulate:
        return
    try:
        mvolt.apply(mvolt.settings_args(SAFE))
        log("restored safe defaults")
    except Exception as exc:
        log(f"WARNING: restore failed: {exc}")


def build_grid(core_vals: list[int], mems: list[int], caps: list[int],
               rels: list[int] | None = None,
               allow_above_safe: bool = False) -> list[dict[str, Any]]:
    rels = rels or [0]
    top = max(core_vals) if core_vals else 0
    if top > config.MAX_AUTO_CORE_MHZ:
        if not allow_above_safe:
            raise ValueError(
                f"refusing core {top} MHz: above the safe ceiling "
                f"{config.MAX_AUTO_CORE_MHZ} MHz (use --allow-above-safe to override). "
                f"+1000 wedged the driver and OS; +250 and +300 have hung this machine."
            )
        log(f"WARNING: running above the safe ceiling ({top} > "
            f"{config.MAX_AUTO_CORE_MHZ} MHz) because --allow-above-safe was given")
    mems = [m for m in mems if abs(m) <= config.MAX_AUTO_MEM_MHZ]
    combos = []
    for core, mem, rel, cap in itertools.product(core_vals, mems, rels, caps):
        p: dict[str, Any] = {"core": core, "mem": mem, "cap": cap}
        if rel:
            p["rel"] = rel
        combos.append(p)
    return combos


def simulate_bench() -> tuple[kombustor.BenchResult, dict[str, Any]]:
    time.sleep(2.0)
    score = 5960 + random.randint(-40, 40)
    res = kombustor.BenchResult(exit_code=0, elapsed_s=62.0, score=score,
                                avg_fps=round(score / 60), min_fps=98, max_fps=103)
    stats = {"watts_avg": 77.0, "watts_max": 78.5, "clk_avg": 1030.0,
             "clk_max": 1665.0, "temp_max": 64.0, "samples": 120}
    return res, stats


def run_trial(journal: state.Journal, hb: state.Heartbeat, params: dict[str, Any],
              boot: str, simulate: bool) -> dict[str, Any]:
    tid = journal.begin_trial(params, boot)
    hb.trial = tid
    hb.phase = "applying"
    log(f"trial {tid}: {params}")

    try:
        if not simulate:
            mvolt.apply_params(params)
        journal.mark_running(tid)
        hb.phase = "benchmark"

        if simulate:
            res, stats = simulate_bench()
        else:
            res, stats = kombustor.run_benchmark()

        status = "crash" if res.crash else "ok"
        payload = {
            "status": status,
            "score": res.score,
            "avg_fps": res.avg_fps,
            "elapsed_s": round(res.elapsed_s, 1),
            "note": res.note or None,
            **stats,
        }
        journal.finish_trial(tid, payload)
        journal.write_live(config.DATA / "live.json", running=True)
        log(f"trial {tid}: {status} score={res.score} W={stats.get('watts_avg')} "
            f"clk={stats.get('clk_avg')}")
        return payload
    except Exception as exc:
        journal.finish_trial(tid, {"status": "aborted", "note": str(exc)[:300]})
        log(f"trial {tid}: ABORTED {exc}")
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tuner")
    ap.add_argument("--trials", type=int, default=3, help="how many trials to run")
    ap.add_argument("--simulate", action="store_true", help="no GPU writes, fake results")
    ap.add_argument("--baseline", action="store_true", help="measure stock before the grid")
    ap.add_argument("--max-core", type=int, default=400)
    ap.add_argument("--cores", type=int, default=3, help="number of core offset steps (0..max)")
    ap.add_argument("--mems", type=str, default="0")
    ap.add_argument("--caps", type=str, default="80")
    ap.add_argument("--list", action="store_true", help="print the grid and exit")
    ap.add_argument("--recovery", choices=["auto", "hardmode"], default="auto",
                    help="auto (default): rely on mVolt+ reverting its settings after a "
                         "crash. hardmode: arm the external watchdog so a stale "
                         "heartbeat reboots the machine (needs scripts/watchdog.ps1)")
    ap.add_argument("--check", action="store_true",
                    help="verify the install and this GPU, then exit")
    ap.add_argument("--setup", action="store_true",
                    help="interactively choose which knobs to sweep, save to state/config.json")
    ap.add_argument("--from-config", action="store_true",
                    help="run the grid saved by --setup instead of the CLI ranges")
    ap.add_argument("--yes", action="store_true",
                    help="with --setup: accept every default without prompting")
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
    ap.add_argument("--allow-above-safe", action="store_true",
                    help="override the MAX_AUTO_CORE_MHZ ceiling (unsafe; hang risk)")
    ap.add_argument("--chaos-core", action="store_true",
                    help="TEST ONLY: apply core +1000 once to force a host hang "
                         "(recorded in meta so a resumed run does not repeat it)")
    args = ap.parse_args(argv)

    from . import capability, knobs as _knobs

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
        print("This GPU exposes:")
        print()
        print(_knobs.describe(st, capability.writable_map(args.reprobe)))
        print()
        print("sweepable now:",
              ", ".join(k.key for k in _knobs.survey(st, capability.writable_map())
                        if k.available))
        return 0

    if args.setup:
        from . import preflight, setup as _setup
        preflight.require_ok()
        p = _setup.plan(mvolt.status(), assume_yes=args.yes, log=print)
        _setup.save(p)
        n = len(_setup.grid_from_plan(p))
        enabled = [k for k, v in p["knobs"].items() if v.get("enabled")]
        print()
        print(f"saved {config.CONFIG_FILE}")
        print(f"  sweeping: {', '.join(enabled) if enabled else '(nothing)'}")
        print(f"  grid size: {n} combination(s)")
        return 0

    if args.serve:
        from . import serve as _serve
        _serve.serve(args.port)
        return 0

    if args.save_picks:
        import json as _json

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

        rows = state.Journal().trials()
        trials = []
        for t in rows:
            try:
                params = _json.loads(t["params_json"])
            except Exception:
                params = {}
            trials.append({"params": params, "score": t["score"],
                           "watts": t["watts_avg"]})

        picks = _profiles.pick_configs(trials, config.MAX_AUTO_CORE_MHZ)
        if not picks:
            log("no scored trials yet - run the tuner first")
            return 7

        for i, (label, params, stats) in enumerate(picks, 1):
            name = f"tuner-best-{i}"
            info = _profiles.write_profile(name, params)
            pretty = " ".join(f"{k}={v}" for k, v in params.items() if v)
            log(f"profile '{name}' ({label}): {pretty or 'stock'} "
                f"[score={stats.get('score')} W={stats.get('watts')}] "
                f"controls={info['controls']} rev={info['revision']}")
        log(f"wrote {len(picks)} profile(s) into {_profiles.profile_document()}")
        return 0

    if args.survey:
        st = mvolt.status()
        print(_knobs.describe(st, capability.writable_map(args.reprobe)))
        print()
        print("sweepable now:",
              ", ".join(k.key for k in _knobs.survey(st, capability.writable_map())
                        if k.available))
        return 0

    mems = [int(x) for x in args.mems.split(",") if x.strip()]
    caps = [int(x) for x in args.caps.split(",") if x.strip()]
    rels = [int(x) for x in args.rels.split(",") if x.strip()] or [0]
    if args.core_vals.strip():
        core_vals = [int(x) for x in args.core_vals.split(",") if x.strip()]
    else:
        step = max(1, args.max_core // max(1, args.cores - 1))
        core_vals = [i * step for i in range(args.cores)]
    if args.from_config:
        from . import setup as _setup
        saved = _setup.load()
        if not saved:
            log("FATAL: no saved setup; run `python -m tuner --setup` first")
            return 5
        grid = _setup.grid_from_plan(saved)
        if not grid:
            log("FATAL: the saved setup has no enabled knobs")
            return 5
        log(f"grid from state/config.json: {len(grid)} combination(s)")
    else:
        try:
            grid = build_grid(core_vals, mems, caps, rels, args.allow_above_safe)
        except ValueError as exc:
            log(f"FATAL: {exc}")
            return 4

    if not args.simulate:
        wmap = capability.writable_map(args.reprobe)
        if not wmap.get("cap", True):
            for p in grid:
                p.pop("cap", None)
            log("power cap is not writable on this GPU - removed from the grid")

    if args.list:
        for i, p in enumerate(grid, 1):
            print(i, p)
        return 0

    if not args.simulate and not mvolt.is_admin():
        log("FATAL: not elevated. mVolt+ rejects all tuning writes without admin.")
        return 2

    boot = state.boot_id()
    journal = state.Journal()
    before = journal.completed_count()
    start_idx = int(journal.get_meta("next_index", "0") or 0)
    stalled = state.note_boot(boot, start_idx)

    log(f"boot={boot}")
    log(f"completed trials so far: {before}")
    log(f"resuming grid at index {start_idx} of {len(grid)}")
    log(f"stalled boots: {stalled.get('stalled_boots')}")

    orphans = journal.reclaim_orphans(boot)
    for o in orphans:
        log(f"reclaimed orphaned trial {o['id']} (was {o['was']})")

    hardmode = args.recovery == "hardmode"

    if hardmode and state.boot_stalled(stalled):
        log("FATAL: too many consecutive boots without progress. Stopping for manual review.")
        journal.event("abort_boot_loop", {"stalled_boots": stalled.get("stalled_boots")})
        return 3

    # After an unclean shutdown mVolt+ adopts an orphaned clock-range lock and
    # then refuses every apply ("settings application safety marker"). Release
    # it unconditionally before touching anything else.
    if not args.simulate:
        from . import recovery
        try:
            if not recovery.force_release(log=log):
                log("WARNING: could not release the clock-range lease; applies may fail")
        except Exception as exc:
            log(f"lease recovery failed: {exc}")
        # Keep run-args in sync so the logon task resumes THIS run, not an old one.
        try:
            argv = " ".join(sys.argv[1:]).strip()
            if argv:
                (config.DATA / "run-args.txt").write_text(argv, encoding="utf-8")
                log(f"run-args refreshed: {argv}")
        except Exception as exc:
            log(f"could not write run-args.txt: {exc}")

    # Arming is what lets the external watchdog reboot the machine. mVolt+ already
    # reverts its own settings on a driver reset, so this is opt-in.
    if hardmode:
        state.arm()
        log("hardmode: armed (external watchdog may reboot on stale heartbeat)")
    else:
        log("recovery=auto: relying on mVolt+ to revert after a crash")

    journal.event("run_start", {"boot": boot, "trials": args.trials, "grid": grid,
                                "recovery": args.recovery})

    hb = state.Heartbeat()
    hb.start()
    rc = 0
    try:
        if args.chaos_core and not journal.get_meta("chaos_done"):
            # One-shot: recorded BEFORE the write so a resumed run never re-arms it.
            journal.set_meta("chaos_done", state.utcnow())
            journal.event("chaos_applied", {})
            log("CHAOS: applying core +1000 - expect a hard hang and a reboot")
            try:
                mvolt.apply(["--core", "1000"])
                log("CHAOS: apply returned (no hang). Resetting core to 0.")
                mvolt.apply(["--core", "0"])
            except Exception as exc:
                log(f"CHAOS: apply did not complete: {exc}")

        if args.baseline:
            run_trial(journal, hb, dict(SAFE), boot, args.simulate)

        done = 0
        consecutive_failures = 0
        for i, params in enumerate(grid):
            if i < start_idx:
                continue
            if done >= args.trials:
                break
            try:
                run_trial(journal, hb, params, boot, args.simulate)
                done += 1
                consecutive_failures = 0
                start_idx = i + 1
                journal.set_meta("next_index", str(start_idx))
            except Exception as exc:
                consecutive_failures += 1
                log(f"trial at grid index {i} failed ({consecutive_failures} in a row): {exc}")
                if consecutive_failures >= 3:
                    log("three consecutive trial failures - stopping")
                    break
                # do not advance next_index: this config gets retried
    except KeyboardInterrupt:
        log("interrupted by user")
        rc = 130
    except Exception as exc:
        log(f"FATAL: {exc}")
        rc = 1
    finally:
        hb.phase = "shutting_down"
        time.sleep(0.2)
        hb.stop()
        restore_safe(args.simulate)
        if hardmode:
            state.disarm()
        try:
            journal.write_live(config.DATA / "live.json", running=False)
        except Exception:
            pass
        journal.event("run_end", {"rc": rc})
        log(f"{'disarmed' if hardmode else 'done'}; completed={journal.completed_count()}")

    return rc


if __name__ == "__main__":
    sys.exit(main())
