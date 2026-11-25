"""Unit tests for the logic that does not need a GPU.

Run:  python -m unittest discover -s tests -v     (or: python -m pytest)
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tuner import (capability, config, kombustor, knobs, optuna_report,  # noqa: E402
                   orchestrator, profiles, serve, setup, state)

# Isolate from the real machine: no real mVolt+ profiles (they raise the safety
# ceilings), no real state/config.json, no writes to the real state/ folder.
_SANDBOX = Path(tempfile.mkdtemp())
os.environ["LOCALAPPDATA"] = str(_SANDBOX / "localappdata")
config.CONFIG_FILE = _SANDBOX / "config.json"
state.EVENTS = _SANDBOX / "events.jsonl"

# --status from an RTX 5080 on mVolt+ v0.50.11 (trimmed of thermal/fan detail).
STATUS_5080 = {
    "gpu": "NVIDIA GeForce RTX 5080", "vbios": "98.03.3b.00.a6",
    "adapter_id": "pci-2c0210de-1432196e-00002c02-b01-s00", "active_profile": "Custom",
    "power_limits": {"requested_mw": 360000, "enforced_mw": 360000, "default_mw": 360000},
    "nvvdd_offsets_uv": {"vmin": 0, "rel": 0, "alt": 0, "ov": 0},
    "msvdd_offsets_uv": {"vmin": 0, "rel": -50000, "alt": 0, "ov": 0},
    "core_offset_mhz": 0, "core_offset_mhz_limit_min": -1000, "core_offset_mhz_limit_max": 1000,
    "memory_offset_mhz": 0, "memory_offset_mhz_limit_min": -1000,
    "memory_offset_mhz_limit_max": 3000,
    "power_limit_percent": 100, "power_limit_percent_limit_min": 69,
    "power_limit_percent_limit_max": 110, "voltage_boost_percent": 0,
    "xbar_offset_mhz": 0, "xbar_offset_mhz_limit_min": -1000, "xbar_offset_mhz_limit_max": 1000,
    "msvdd_clock_ratio": 0.89990,
    "sys_offset_mhz": 0, "sys_offset_mhz_limit_min": -1000, "sys_offset_mhz_limit_max": 1000,
    "video_offset_mhz": 0, "video_offset_mhz_limit_min": -1000,
    "video_offset_mhz_limit_max": 1000,
    "boost_lock": False,
    "voltage_demand_mv": {"Core": 0, "Xbar": 0, "SYS": 0, "Video": 0},
    "power_channel_a": {"NVVDD OCP": {"current": 300, "default": 300, "limit_min": 0,
                                      "limit_max": 5001},
                        "MSVDD OCP": {"current": 120, "default": 120, "limit_min": 0,
                                      "limit_max": 5001}},
    "clock_range_mhz": {"locked": False, "uncertain": False, "min": 0, "max": 3090,
                        "limit_min": 0, "limit_max": 3090},
}
# ... plus the voltage-rail and power-client fields of the same --status
STATUS_5080_FULL = dict(
    STATUS_5080, xoc=False,
    nvvdd_min_mv=800, nvvdd_max_mv=1050, msvdd_min_mv=800, msvdd_max_mv=1000,
    nvvdd_device_range_uv={"minimum": 445000, "maximum": 1280000, "step": 5000},
    msvdd_device_range_uv={"minimum": 445000, "maximum": 1280000, "step": 5000},
    power_client={"fe_mw": 360000, "maximum_mw": 396000, "managed_cap": False},
)

STATUS = {
    "core_offset_mhz": 50, "core_offset_mhz_limit_min": -1000, "core_offset_mhz_limit_max": 1000,
    "memory_offset_mhz": 0, "memory_offset_mhz_limit_min": -1000,
    "memory_offset_mhz_limit_max": 3000,
    "nvvdd_offsets_uv": {"vmin": -10000, "rel": -25000, "alt": 5000},
    "voltage_boost_percent": 0,
    "boost_lock": "off",
    "power_limit_percent": 100,
    "power_limits": {"requested_mw": 80000, "enforced_mw": 80000, "default_mw": 80000},
    "clock_range_mhz": {"min": 0, "max": 2100, "limit_min": 0, "limit_max": 2100,
                        "locked": False},
    "active_profile": "Daily",
}


def quiet(*_a, **_k):
    pass


class KnobTests(unittest.TestCase):
    def test_domain_knobs_detected_with_either_spelling(self):
        for st in ({"xbar_offset_mhz": 0, "xbar_offset_mhz_limit_min": -500,
                    "xbar_offset_mhz_limit_max": 500},
                   {"xbar_offset": 0, "xbar_offset_limit_min": -500,
                    "xbar_offset_limit_max": 500}):
            row = {r.key: r for r in knobs.survey(st)}["xbar_offset"]
            self.assertTrue(row.available, row.reason)

    def test_cap_readback_is_none_when_unavailable(self):
        self.assertIsNone(knobs.KNOBS["cap"].readback({"power_limits": {}}))
        self.assertEqual(knobs.KNOBS["cap"].readback(STATUS), 80.0)

    def test_boost_lock_string_readback(self):
        self.assertEqual(knobs.verify({"boost_lock": False}, {"boost_lock": "off"}), [])
        self.assertTrue(knobs.verify({"boost_lock": True}, {"boost_lock": "off"}))

    def test_power_and_cap_are_exclusive(self):
        with self.assertRaises(ValueError):
            knobs.build_args({"power": 100, "cap": 80})

    def test_verify_checks_rel_companions(self):
        p = {"rel": -25, "vmin": 0, "alt": 5}
        self.assertTrue(any("vmin" in x for x in knobs.verify(p, STATUS)))
        p["vmin"] = -10
        self.assertEqual(knobs.verify(p, STATUS), [])

    def test_snapshot_includes_companions(self):
        snap = knobs.snapshot(STATUS, ["core", "rel"])
        self.assertEqual(snap, {"core": 50, "rel": -25.0, "vmin": -10.0, "alt": 5.0})

    def test_aggression(self):
        failed = {"core": 200, "mem": 0}
        self.assertTrue(knobs.at_least_as_aggressive({"core": 250, "mem": 0}, failed))
        self.assertTrue(knobs.at_least_as_aggressive({"core": 200, "mem": 200}, failed))
        self.assertFalse(knobs.at_least_as_aggressive({"core": 150, "mem": 400}, failed))
        # MORE voltage at the same clock is riskier (measured on the RTX 5080)
        self.assertTrue(knobs.at_least_as_aggressive({"core": 200, "rel": 0},
                                                     {"core": 200, "rel": -25}))
        self.assertFalse(knobs.at_least_as_aggressive({"core": 200, "rel": -50},
                                                      {"core": 200, "rel": -25}))
        # unknown direction must match exactly
        self.assertFalse(knobs.at_least_as_aggressive({"core": 200, "cap": 70},
                                                      {"core": 200, "cap": 80}))

    def test_validate_params_has_no_offset_ceilings(self):
        # no tuner-imposed caps: only the driver's own range applies
        self.assertEqual(knobs.validate_params({"core": 1000, "mem": 3000}), [])
        self.assertEqual(knobs.validate_params({"core": 1000}, STATUS), [])
        self.assertTrue(knobs.validate_params({"core": 1001}, STATUS))  # driver range
        self.assertTrue(knobs.validate_params({"bogus": 1}))
        self.assertTrue(knobs.validate_params({"core": "x"}))
        self.assertTrue(knobs.validate_params({"core": 150}, {"core_offset_mhz": 0,
                                                            "core_offset_mhz_limit_min": -100,
                                                            "core_offset_mhz_limit_max": 100}))


class CapabilityTests(unittest.TestCase):
    def test_probes_rewrite_current_values(self):
        args = capability.probe_args(STATUS)
        self.assertEqual(args["core"], [["--core", "50"]])
        self.assertEqual(args["rel"], [["--nvvdd-offsets", "-10,-25,5"]])
        self.assertEqual(args["power"], [["--power", "100"]])
        self.assertEqual(args["cap"], [["--power-cap-watts", "80"], ["--return-to-percentage"]])
        self.assertEqual(args["boost_lock"], [["--boost-lock", "off"]])
        self.assertIn("clock_max", args)
        self.assertNotIn("core_vdemand", args)  # write-only: current value unknown

    def test_held_clock_lock_is_not_probed(self):
        st = dict(STATUS, clock_range_mhz={"max": 1800, "locked": True})
        self.assertNotIn("clock_max", capability.probe_args(st))

    def test_not_elevated_probe_writes_and_caches_nothing(self):
        with mock.patch("tuner.mvolt.is_admin", return_value=False), \
             mock.patch("tuner.mvolt.apply") as ap, \
             mock.patch.object(capability, "CACHE", Path(tempfile.mkdtemp()) / "w.json"):
            out = capability.probe(status=STATUS)
            ap.assert_not_called()
            self.assertFalse(capability.CACHE.exists())
            self.assertEqual(out["knobs"], {})


class SetupTests(unittest.TestCase):
    ST = {"core_offset_mhz": 0, "core_offset_mhz_limit_min": -1000,
          "core_offset_mhz_limit_max": 1000}

    def test_yes_never_prompts(self):
        asked = []
        setup.plan(self.ST, assume_yes=True, log=quiet,
                   ask=lambda p, d: asked.append(p) or d)
        self.assertEqual(asked, [])

    def test_limits_asked_first_and_offsets_clamped_only_to_driver_range(self):
        # limits: 300 W, none; then core: include, 0..1500 step 250
        answers = iter(["300", "none", "none", "y", "0", "1500", "250"])
        p = setup.plan(self.ST, log=quiet, ask=lambda pr, d: next(answers, d))
        self.assertEqual(p["limits"], {"max_watts": 300.0, "max_core_mv": None,
                                       "max_fabric_mv": None})
        self.assertEqual(p["knobs"]["core"]["max"], 1000)   # driver max, not a tuner cap
        self.assertEqual(max(g["core"] for g in setup.grid_from_plan(p)), 1000)

    def test_yes_sets_no_limits(self):
        p = setup.plan(self.ST, assume_yes=True, log=quiet)
        self.assertEqual(p["limits"], {"max_watts": None, "max_core_mv": None,
                                       "max_fabric_mv": None})

    def test_non_numeric_input_retries(self):
        answers = iter(["none", "none", "none", "y", "abc", "0", "100", "50"])
        p = setup.plan(self.ST, log=quiet, ask=lambda pr, d: next(answers, d))
        self.assertEqual(p["knobs"]["core"]["min"], 0)

    def test_grid_rel_has_no_mem_and_fixed_values_apply(self):
        plan = {"knobs": {"rel": {"enabled": True, "min": -50, "max": -25, "step": 12.5},
                          "clock_max": {"enabled": True, "value": 1800}}}
        grid = setup.grid_from_plan(plan)
        self.assertEqual(grid, [{"clock_max": 1800, "rel": -50},
                                {"clock_max": 1800, "rel": -37.5},
                                {"clock_max": 1800, "rel": -25}])


class OrchestratorTests(unittest.TestCase):
    def test_default_cli_grid_is_accepted(self):
        with redirect_stdout(io.StringIO()) as out:
            rc = orchestrator.main(["--simulate", "--list"])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertNotIn("cap", out.getvalue())  # no power cap unless asked

    def test_check_grid_only_rejects_malformed_or_out_of_driver_range(self):
        orchestrator.check_grid([{"core": 0}, {"core": 1000}])  # no ceiling
        with self.assertRaises(ValueError):
            orchestrator.check_grid([{"core": 0}, {"core": 1200}], STATUS)
        with self.assertRaises(ValueError):
            orchestrator.check_grid([{"core": "fast"}])

    def _run(self, st_applied, stats, res, limits):
        j = state.Journal(Path(tempfile.mkdtemp()) / "j.db")
        reverted = []
        with mock.patch("tuner.mvolt.apply_params", return_value=st_applied), \
             mock.patch("tuner.kombustor.run_benchmark", return_value=(res, stats)) as rb, \
             mock.patch("tuner.mvolt.status", return_value=dict(STATUS, core_offset_mhz=300)), \
             mock.patch("tuner.kombustor.driver_resets_since", return_value=0), \
             mock.patch.object(orchestrator, "log", quiet), \
             mock.patch.object(config, "LIVE_JSON", Path(tempfile.mkdtemp()) / "live.json"):
            out = orchestrator.run_trial(j, mock.Mock(), {"core": 300}, "boot", False,
                                         limits=limits, revert=lambda: reverted.append(1))
        return out, reverted, rb

    def test_voltage_limit_reverts_before_any_load(self):
        res = kombustor.BenchResult(exit_code=0, elapsed_s=62, score=6100)
        out, reverted, rb = self._run({"nvvdd_max_mv": 1100}, {}, res,
                                      {"max_core_mv": 1050, "max_watts": None})
        self.assertEqual(out["status"], "over_voltage_limit")
        self.assertEqual(reverted, [1])
        rb.assert_not_called()
        out, _, rb = self._run({"nvvdd_max_mv": 1000}, {"watts_avg": 300}, res,
                               {"max_core_mv": 1050, "max_watts": None})
        self.assertEqual(out["status"], "ok")
        self.assertEqual(rb.call_args.kwargs["watt_limit"], None)

    def test_power_limit(self):
        stopped = kombustor.BenchResult(exit_code=1, elapsed_s=20, over_limit=371.2,
                                        note="stopped")
        out, _, rb = self._run({}, {"watts_avg": 365}, stopped, {"max_watts": 350})
        self.assertEqual(out["status"], "over_power_limit")
        self.assertEqual(rb.call_args.kwargs["watt_limit"], 350)
        done = kombustor.BenchResult(exit_code=0, elapsed_s=62, score=6100)
        out, _, _ = self._run({}, {"watts_avg": 351.0}, done, {"max_watts": 350})
        self.assertEqual(out["status"], "over_power_limit")   # whole-run average
        self.assertEqual(out["score"], 6100)                   # kept as a measurement

    def test_fabric_voltage_limit_and_xoc(self):
        st = {"nvvdd_max_mv": 1000, "msvdd_max_mv": 1040}
        self.assertIsNone(orchestrator.voltage_over_limit(st, {"max_core_mv": 1000}))
        self.assertIn("fabric", orchestrator.voltage_over_limit(
            st, {"max_core_mv": 1000, "max_fabric_mv": 1000}))
        self.assertIn("unreadable", orchestrator.voltage_over_limit({}, {"max_fabric_mv": 1000}))
        self.assertFalse(orchestrator.needs_xoc({"max_core_mv": 1150}, STATUS_5080_FULL))
        self.assertTrue(orchestrator.needs_xoc({"max_core_mv": 1200}, STATUS_5080_FULL))
        calls = []
        with mock.patch("tuner.mvolt.apply", side_effect=lambda a: calls.append(a) or {}), \
             mock.patch("tuner.mvolt.status", return_value={"core_offset_mhz": 10}), \
             mock.patch("time.sleep"):
            from tuner import mvolt
            mvolt.apply_params({"core": 10}, xoc=True)
        self.assertEqual(calls[0][-1], "--xoc")

    def test_watt_limit_becomes_power_cap(self):
        grid = [{"core": 350}, {"core": 400, "cap": 250}, {"core": 400, "power": 110}]
        how = orchestrator.enforce_power_limit(grid, {"max_watts": 300}, STATUS_5080,
                                               {"cap": True})
        self.assertIn("watt cap 300", how)
        self.assertEqual([g["cap"] for g in grid], [300, 250, 300])
        self.assertNotIn("power", grid[2])
        # cap not writable -> power percent instead
        grid = [{"core": 350}]
        orchestrator.enforce_power_limit(grid, {"max_watts": 300}, STATUS_5080,
                                         {"cap": False, "power": True})
        self.assertEqual(grid[0]["power"], 83)
        self.assertEqual(orchestrator.enforce_power_limit(grid, {"max_watts": None}, None, {}),
                         "no watt limit")

    def test_capped_run_tolerates_small_overshoot(self):
        res = kombustor.BenchResult(exit_code=0, elapsed_s=62, score=6100)
        out, _, rb = self._run({}, {"watts_avg": 304.0}, res, {"max_watts": 300})
        self.assertEqual(out["status"], "over_power_limit")      # uncapped: exact
        j = state.Journal(Path(tempfile.mkdtemp()) / "j.db")
        with mock.patch("tuner.mvolt.apply_params", return_value={}), \
             mock.patch("tuner.kombustor.run_benchmark",
                        return_value=(res, {"watts_avg": 304.0})) as rb, \
             mock.patch("tuner.mvolt.status", return_value=dict(STATUS, core_offset_mhz=300,
                        power_limits={"requested_mw": 300000})), \
             mock.patch("tuner.kombustor.driver_resets_since", return_value=0), \
             mock.patch.object(orchestrator, "log", quiet), \
             mock.patch.object(config, "LIVE_JSON", Path(tempfile.mkdtemp()) / "live.json"):
            out = orchestrator.run_trial(j, mock.Mock(), {"core": 300, "cap": 300}, "b", False,
                                         limits={"max_watts": 300})
        self.assertEqual(out["status"], "ok")                     # capped: within +2%
        self.assertAlmostEqual(rb.call_args.kwargs["watt_limit"], 315.0)

    def test_baseline_reference_skips_voltage_check(self):
        j = state.Journal(Path(tempfile.mkdtemp()) / "j.db")
        res = kombustor.BenchResult(exit_code=0, elapsed_s=62, score=6000)
        with mock.patch("tuner.mvolt.apply_params", return_value={"nvvdd_max_mv": 1050}), \
             mock.patch("tuner.kombustor.run_benchmark", return_value=(res, {"watts_avg": 290})), \
             mock.patch("tuner.mvolt.status", return_value=dict(STATUS, core_offset_mhz=0)), \
             mock.patch("tuner.kombustor.driver_resets_since", return_value=0), \
             mock.patch.object(orchestrator, "log", quiet), \
             mock.patch.object(config, "LIVE_JSON", Path(tempfile.mkdtemp()) / "live.json"):
            out = orchestrator.run_trial(j, mock.Mock(), {"core": 0}, "b", False,
                                         limits={"max_core_mv": 1000}, reference=True)
        self.assertEqual(out["status"], "ok")

    def test_restore_releases_cap_it_set(self):
        rp = orchestrator.capture_restore_point(STATUS_5080, {"core", "cap"})
        self.assertTrue(rp["release_cap"])
        self.assertNotIn("cap", rp["params"])
        calls = []
        with mock.patch("tuner.mvolt.apply", side_effect=lambda a: calls.append(a)), \
             mock.patch.object(orchestrator, "log", quiet):
            orchestrator.restore(rp, simulate=False)
        self.assertIn(["--return-to-percentage"], calls)
        # It keeps the last cap as the percentage, so the user's % is written after.
        self.assertEqual(calls[calls.index(["--return-to-percentage"]) + 1], ["--power", "100"])
        rp = orchestrator.capture_restore_point(dict(STATUS_5080, power_limit_percent=95), {"cap"})
        calls.clear()
        with mock.patch("tuner.mvolt.apply", side_effect=lambda a: calls.append(a)), \
             mock.patch.object(orchestrator, "log", quiet):
            orchestrator.restore(rp, simulate=False)
        self.assertIn(["--power", "95"], calls)
        calls.clear()
        with mock.patch("tuner.mvolt.apply", side_effect=lambda a: calls.append(a)), \
             mock.patch.object(orchestrator, "log", quiet):    # older point: no power_pct
            orchestrator.restore({"params": {}, "release_cap": True}, simulate=False)
        self.assertEqual(calls[:2], [["--return-to-percentage"], ["--power", "100"]])

    def test_driver_ignoring_rail_offsets_is_named(self):
        from tuner import mvolt
        st = dict(STATUS_5080, nvvdd_offsets_uv={"vmin": 0, "rel": 0, "alt": 0, "ov": 0})
        with mock.patch("tuner.mvolt.apply", return_value={"success": True,
                                                           "rail_offsets_ignored_by_driver": True}), \
             mock.patch("tuner.mvolt.status", return_value=st), mock.patch("time.sleep"):
            with self.assertRaises(mvolt.DriverIgnoresRails):
                mvolt.apply_params({"rel": -50, "alt": -70, "vmin": 0}, log=quiet)
        with mock.patch("tuner.mvolt.apply", return_value={"success": True}), \
             mock.patch("tuner.mvolt.status", return_value=st), mock.patch("time.sleep"):
            with self.assertRaises(mvolt.MVoltError) as cm:         # plain "did not stick"
                mvolt.apply_params({"rel": -50, "alt": -70, "vmin": 0}, log=quiet)
            self.assertNotIsInstance(cm.exception, mvolt.DriverIgnoresRails)

    def test_sampler_limit_uses_rolling_average_after_warmup(self):
        s = kombustor.PowerSampler(watt_limit=350)
        s.samples = [(t * 0.5, 400.0, 0, 0) for t in range(10)]   # spike during warm-up
        self.assertIsNone(s.check_limit())
        s.samples += [(5 + t * 0.5, 340.0, 0, 0) for t in range(6)]
        self.assertIsNone(s.check_limit())
        s.samples += [(8 + t * 0.5, 360.0, 0, 0) for t in range(6)]
        self.assertEqual(s.check_limit(), 360.0)

    def test_negative_value_lists_parse(self):
        glued = orchestrator._glue_negative_lists(
            ["--core-vals", "-50,0,25", "--rels", "0,-25", "--mems", "-500", "--trials", "3"])
        self.assertEqual(glued, ["--core-vals=-50,0,25", "--rels", "0,-25",
                                 "--mems=-500", "--trials", "3"])
        with redirect_stdout(io.StringIO()) as out:
            rc = orchestrator.main(["--simulate", "--list", "--core-vals", "-50,0"])
        self.assertEqual(rc, 0)
        self.assertIn("'core': -50", out.getvalue())

    def test_apply_base_keeps_profile_controls(self):
        grid = orchestrator.apply_base([{"core": 0}, {"core": 50}], {"core": 150, "mem": 500})
        self.assertEqual(grid, [{"core": 150, "mem": 500}, {"core": 200, "mem": 500}])

    def test_fill_companions(self):
        grid = [{"core": 0, "rel": -25}, {"core": 0}]
        orchestrator.fill_companions(grid, {"vmin": -10.0, "alt": 5.0})
        self.assertEqual(grid[0], {"core": 0, "rel": -25, "vmin": -10.0, "alt": 5.0})
        self.assertEqual(grid[1], {"core": 0})

    def test_restore_point_prefers_named_profile(self):
        rp = orchestrator.capture_restore_point(STATUS, {"core", "mem", "rel"})
        self.assertEqual(rp["profile"], "Daily")
        self.assertEqual(rp["params"]["core"], 50)
        rp = orchestrator.capture_restore_point(dict(STATUS, active_profile="Custom"), {"core"})
        self.assertIsNone(rp["profile"])

    def test_driver_reset_is_detected_after_benchmark(self):
        j = state.Journal(Path(tempfile.mkdtemp()) / "j.db")
        hb = mock.Mock()
        res = kombustor.BenchResult(exit_code=0, elapsed_s=62, score=6100)
        stock = dict(STATUS, core_offset_mhz=0)
        with mock.patch("tuner.mvolt.apply_params"), \
             mock.patch("tuner.kombustor.run_benchmark", return_value=(res, {"watts_avg": 70})), \
             mock.patch("tuner.mvolt.status", return_value=stock), \
             mock.patch("tuner.kombustor.driver_resets_since", return_value=0), \
             mock.patch.object(orchestrator, "log", quiet), \
             mock.patch.object(config, "LIVE_JSON", Path(tempfile.mkdtemp()) / "live.json"):
            out = orchestrator.run_trial(j, hb, {"core": 150}, "boot", simulate=False)
        self.assertEqual(out["status"], "driver_reset")
        self.assertIsNone(out["score"])
        self.assertEqual(j.unstable_params(), [{"core": 150}])


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.j = state.Journal(Path(tempfile.mkdtemp()) / "j.db")

    def _trial(self, params, status, boot="b1"):
        tid = self.j.begin_trial(params, boot)
        if status not in ("applying", "running"):
            self.j.finish_trial(tid, {"status": status})
        return tid

    def test_resume_by_identity_not_index(self):
        self._trial({"core": 0, "vmin": -10}, "ok")
        self._trial({"core": 50}, "aborted")
        keys = self.j.finished_param_keys()
        self.assertIn(state.param_key({"core": 0}), keys)       # companions ignored
        self.assertNotIn(state.param_key({"core": 50}), keys)   # aborted gets retried

    def test_finished_counts_support_repeats(self):
        for _ in range(2):
            tid = self.j.begin_trial({"core": 450, "vmin": 0}, "b")
            self.j.finish_trial(tid, {"status": "ok"})
        self.assertEqual(self.j.finished_counts()[state.param_key({"core": 450})], 2)

    def test_over_limit_is_done_only_under_the_same_limits(self):
        lim = {"max_watts": 300, "max_core_mv": 1000}
        tid = self.j.begin_trial({"core": 400}, "b", lim)
        self.j.finish_trial(tid, {"status": "over_power_limit"})
        self.assertIn(state.param_key({"core": 400}), self.j.finished_param_keys(lim))
        self.assertNotIn(state.param_key({"core": 400}),
                         self.j.finished_param_keys({"max_watts": 330, "max_core_mv": 1000}))

    def test_reclaim_distinguishes_reboot_from_process_death(self):
        self._trial({"core": 250}, "applying", boot="old")
        self._trial({"core": 100}, "running", boot="new")
        causes = {o["params"]["core"]: o["cause"] for o in self.j.reclaim_orphans("new")}
        self.assertEqual(causes, {250: "host reboot", 100: "process death"})
        self.assertEqual(self.j.unstable_params(), [{"core": 250}])


class ProfileTests(unittest.TestCase):
    def test_picks_aggregate_and_skip_unstable(self):
        trials = [
            {"params": {"core": 0}, "score": 6000, "watts": 77, "status": "ok"},
            {"params": {"core": 0}, "score": 6020, "watts": 77, "status": "ok"},
            {"params": {"core": 150}, "score": 6150, "watts": 77.5, "status": "ok"},
            {"params": {"core": 100}, "score": 6140, "watts": 76.0, "status": "ok"},
            {"params": {"core": 200}, "score": 6300, "watts": 78, "status": "ok"},
            {"params": {"core": 200}, "score": None, "watts": None, "status": "driver_reset"},
            {"params": {"core": 50}, "score": 6100, "watts": None, "status": "ok"},
        ]
        trials.append({"params": {"core": 450}, "score": 6400, "watts": 380,
                       "status": "over_power_limit"})
        picks = profiles.pick_configs(trials)
        labels = {label: params for label, params, _ in picks}
        self.assertEqual(labels["best-performance"], {"core": 150})
        # core 50 has no power reading, so it can never win efficiency
        self.assertEqual(labels["best-efficiency"], {"core": 100})
        self.assertEqual(labels["stock-baseline"], {})
        base = next(s for label, _, s in picks if label == "stock-baseline")
        self.assertEqual(base["score"], 6010.0)

    def test_unsavable_params_are_refused_not_dropped(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.build_controls({"core": 100, "boost_lock": True})
        self.assertEqual(profiles.build_controls({"cap": 300})["power_cap"]["milliwatts"], 300000)
        c = profiles.build_controls({"core": 100, "xbar_offset": 50, "rel": -12.5,
                                     "vmin": -10, "alt": 5})
        self.assertTrue(c["xbar"]["enabled"])
        self.assertEqual(c["nvvdd"]["rel_offset_uv"], -12500)
        self.assertEqual(c["nvvdd"]["vmin_offset_uv"], -10000)

    def test_write_profile_picks_live_document_and_is_atomic(self):
        tmp = Path(tempfile.mkdtemp())
        pdir = tmp / "mVolt+" / "profiles"
        pdir.mkdir(parents=True)
        live = pdir / "90.06.39-ADAPTER1.json"
        live.write_text(json.dumps({"revision": 4, "profiles": [
            {"id": "Daily", "name": "Daily", "mobile_rel_only": False,
             "controls": {"nvvdd": {"enabled": True, "rel_offset_uv": -12500}}}]}))
        backup = pdir / "90.06.39-ADAPTER1.before-power-cap.json"
        backup.write_text("{}")
        os.utime(backup, (live.stat().st_mtime + 100,) * 2)  # newer than the live doc
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(tmp)}), \
             mock.patch.object(profiles, "app_running", return_value=False):
            self.assertEqual(profiles.profile_document(), live)
            info = profiles.write_profile("tuner-best-1", {"core": 150},
                                          backup_dir=tmp / "bk")
            doc = json.loads(live.read_text())
            self.assertEqual(doc["revision"], 5)
            self.assertFalse(doc["profiles"][-1]["mobile_rel_only"])
            self.assertEqual(info["enabled"], ["core"])
            self.assertEqual(profiles.read_profiles()[0]["params"],
                             {"rel": -12.5, "vmin": 0, "alt": 0})
            with self.assertRaises(profiles.ProfileError):
                profiles.write_profile("Daily", {"core": 100}, backup_dir=tmp / "bk")
            with self.assertRaises(profiles.ProfileError):
                profiles.write_profile("tuner-x", {}, backup_dir=tmp / "bk")


class RTX5080Tests(unittest.TestCase):
    """Real --status and profile layouts from an RTX 5080 / mVolt+ v0.50.11."""

    TEST1 = {"id": "6E227E68", "name": "test1", "mobile_rel_only": False, "controls": {
        "core": {"enabled": True, "offset_mhz": 400},
        "memory": {"enabled": True, "offset_mhz": 2000},
        "xbar": {"enabled": True, "offset_mhz": 400},
        "sys_clock": {"enabled": True, "offset_mhz": 300},
        "nvvdd": {"enabled": True, "restore_default": False, "vmin_offset_uv": 0,
                  "rel_offset_uv": -50000, "alt_offset_uv": -70000},
        "msvdd_clock_ratio": {"enabled": True, "raw_ratio": 65536},
        "fan": {"enabled": False, "automatic": True, "master_pct": 30},
        "power_cap": {"enabled": False, "milliwatts": 360000, "absent": False},
    }}

    def test_every_control_is_detected(self):
        rows = {r.key: r for r in knobs.survey(STATUS_5080)}
        self.assertEqual([k for k, r in rows.items() if not r.available], [])
        self.assertEqual(rows["core_vdemand"].current, 0)
        self.assertEqual(rows["nvvdd_ocp"].current, 300)
        self.assertEqual(rows["power"].bounds, (69.0, 110.0))
        self.assertAlmostEqual(rows["msvdd_clock_ratio"].current, 0.8999)

    def test_ocp_only_bounded_by_driver_range(self):
        self.assertEqual(knobs.validate_params({"nvvdd_ocp": 400}, STATUS_5080), [])
        self.assertTrue(knobs.validate_params({"nvvdd_ocp": 6000}, STATUS_5080))

    def test_ratio_tolerance_is_tight(self):
        self.assertTrue(knobs.verify({"msvdd_clock_ratio": 1.0}, STATUS_5080))

    def _with_doc(self):
        tmp = Path(tempfile.mkdtemp())
        pdir = tmp / "mVolt+" / "profiles"
        pdir.mkdir(parents=True)
        doc = pdir / "98.03.3b.00.a6-pci-2c0210de.json"
        doc.write_text(json.dumps({"revision": 1, "profiles": [self.TEST1]}))
        return tmp, doc

    def test_test1_is_read_and_sweeping_above_it_is_allowed(self):
        tmp, _doc = self._with_doc()
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(tmp)}):
            p = profiles.read_profiles()[0]["params"]
            self.assertEqual(p, {"core": 400, "mem": 2000, "xbar_offset": 400,
                                 "sys_offset": 300, "rel": -50, "vmin": 0, "alt": -70,
                                 "msvdd_clock_ratio": 1.0})
            grid = orchestrator.apply_base([{"core": 0}, {"core": 50}, {"core": 100}],
                                           {k: v for k, v in p.items() if v})
            orchestrator.check_grid(grid, STATUS_5080)   # +500 core: no tuner ceiling
            self.assertEqual([g["core"] for g in grid], [400, 450, 500])

    def test_stability_edge_depends_on_voltage(self):
        def t(core, rel, status):
            return {"params": {"core": core, "rel": rel}, "status": status,
                    "score": 1 if status == "ok" else None, "watts": 300}
        # as measured: deeper undervolt holds a HIGHER core offset
        trials = [t(400, 0, "ok"), t(425, 0, "driver_reset"),
                  t(425, -50, "ok"), t(475, -50, "ok"), t(500, -50, "crash")]
        edges = {e["setting"]["rel"]: e for e in optuna_report.stability_edge(trials)}
        self.assertEqual(edges[0]["edge"], "between 400 and 425")
        self.assertEqual(edges[-50]["edge"], "between 475 and 500")
        # a crash at 0 mV does not rule out 425 with a deeper undervolt...
        self.assertIsNone(orchestrator.ruled_out({"core": 425, "rel": -50},
                                                 [{"core": 425, "rel": 0}]))
        # ...but does rule out more core at more voltage
        self.assertIsNotNone(orchestrator.ruled_out({"core": 450, "rel": 25},
                                                    [{"core": 425, "rel": 0}]))

    def test_written_profile_keeps_mvolt_layout(self):
        tmp, doc = self._with_doc()
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(tmp)}), \
             mock.patch.object(profiles, "app_running", return_value=False):
            profiles.write_profile("tuner-best-1", {"core": 425, "rel": -60, "vmin": 0,
                                                    "alt": -70, "msvdd_clock_ratio": 1.0},
                                   backup_dir=tmp / "bk")
            c = json.loads(doc.read_text())["profiles"][-1]["controls"]
            self.assertIn("fan", c)                     # copied from test1, disabled
            self.assertFalse(c["fan"]["enabled"])
            self.assertFalse(c["memory"]["enabled"])    # test1's mem not inherited
            self.assertEqual(c["nvvdd"]["rel_offset_uv"], -60000)
            self.assertEqual(c["msvdd_clock_ratio"], {"enabled": True, "raw_ratio": 65536})


class OptunaReportTests(unittest.TestCase):
    @staticmethod
    def synthetic(n=40, seed=1):
        """core drives score, mem drives watts, deep undervolt + high core crashes."""
        import random
        rnd = random.Random(seed)
        out = []
        for _ in range(n):
            core = rnd.choice([0, 100, 200, 300, 400])
            mem = rnd.choice([0, 1000, 2000])
            rel = rnd.choice([0, -50, -100])
            crash = core >= 300 and rel <= -100
            out.append({
                "params": {"core": core, "mem": mem, "rel": rel, "vmin": 0, "alt": 0},
                "status": "driver_reset" if crash else "ok",
                "score": None if crash else 10000 + 3 * core + rnd.gauss(0, 15),
                "watts": None if crash else 300 + 0.02 * mem + rnd.gauss(0, 1),
            })
        return out

    def test_finds_the_planted_effects(self):
        rep = optuna_report.report(self.synthetic())
        self.assertTrue(rep["available"])
        t = rep["targets"]
        self.assertEqual(t["score"]["knobs"][0]["knob"], "core")
        self.assertGreater(t["score"]["knobs"][0]["trend_r"], 0.8)
        self.assertEqual(t["watts"]["knobs"][0]["knob"], "mem")
        crash_top = {k["knob"] for k in t["instability"]["knobs"][:2]}
        self.assertEqual(crash_top, {"core", "rel"})
        self.assertNotIn("vmin", [k["knob"] for k in t["score"]["knobs"]])  # companion
        self.assertIsNotNone(t["score"]["knobs"][0]["pedanova"])  # >= 20 trials
        self.assertIn("SCORE", optuna_report.render(rep))

    def test_falls_back_to_pedanova_without_sklearn(self):
        real = optuna_report._importances

        def no_fanova(study, knobs_, n):
            with mock.patch("optuna.importance.FanovaImportanceEvaluator",
                            side_effect=ImportError("No module named 'sklearn'")):
                return real(study, knobs_, n)

        with mock.patch.object(optuna_report, "_importances", no_fanova):
            t = optuna_report.report(self.synthetic())["targets"]["score"]
        self.assertEqual(t["evaluator"], "pedanova")
        self.assertEqual(t["knobs"][0]["knob"], "core")
        self.assertIn("sklearn", t["fanova_error"])

    def test_small_or_flat_data_is_reported_not_crashed(self):
        rep = optuna_report.report(self.synthetic(n=3))
        self.assertIn("needs at least", rep["targets"]["score"]["note"])
        flat = [{"params": {"core": 0}, "status": "ok", "score": 1, "watts": 1}] * 6
        rep = optuna_report.report(flat)
        self.assertEqual(rep["not_varied"], ["core"])
        self.assertEqual(rep["targets"]["score"]["knobs"], [])


class WebApiTests(unittest.TestCase):
    def setUp(self):
        from tuner import webapi
        # never call the real mVolt+ from tests
        p = mock.patch.object(webapi, "_status", return_value=(STATUS_5080_FULL, None))
        p.start()
        self.addCleanup(p.stop)

    def test_limits_validation_allows_above_stock_up_to_card_max(self):
        from tuner import webapi
        out = webapi.save_limits({"max_watts": "380", "max_core_mv": "", "max_fabric_mv": "1200"})
        self.assertEqual(out["limits"], {"max_watts": 380.0, "max_core_mv": None,
                                         "max_fabric_mv": 1200.0})
        self.assertEqual(setup.load_limits(), out["limits"])
        for bad in ({"max_watts": "-5"}, {"max_watts": "lots"}, {"max_watts": "400"},
                    {"max_core_mv": "1300"}):
            with self.assertRaises(ValueError):
                webapi.save_limits(bad)

    def test_state_has_defaults_and_ranges(self):
        from tuner import webapi
        with mock.patch.object(profiles, "app_running", return_value=True):
            s = webapi.gpu_state()
        d = {k["key"]: k["default"] for k in s["knobs"]}
        self.assertEqual((d["core"], d["power"], d["cap"], d["nvvdd_ocp"], d["msvdd_rel"]),
                         (0, 100, 360.0, 300, -50.0))
        self.assertIsNone(d["msvdd_clock_ratio"])
        self.assertEqual((s["power"]["default_w"], s["power"]["max_w"]), (360.0, 396.0))
        self.assertEqual((s["rails"]["core"]["stock_mv"], s["rails"]["fabric"]["stock_mv"]),
                         (1050.0, 1000.0))   # the driver's -50 mV MSVDD is stock
        self.assertTrue(s["app_running"])

    def test_plan_relative_to_profile(self):
        from tuner import webapi
        with mock.patch.object(profiles, "read_profiles", return_value=[
                {"id": "x", "name": "test1", "params": {"core": 400, "rel": -50, "alt": -70}}]):
            p = webapi.build_plan({"baseline_profile": "test1", "knobs": {
                "core": {"mode": "sweep", "min": "-50", "max": "50", "step": "50"},
                "rel": {"mode": "fixed", "value": "-25"},
                "mem": {"mode": "off"}}}, STATUS_5080)
        self.assertEqual([g["core"] for g in p["grid"]], [350, 400, 450])
        self.assertTrue(all(g["rel"] == -75 and g["alt"] == -70 for g in p["grid"]))
        for bad in ({"knobs": {}}, {"knobs": {"bogus": {"mode": "sweep"}}},
                    {"knobs": {"core": {"mode": "sweep", "min": 0, "max": 9, "step": 0}}},
                    {"knobs": {"core": {"mode": "sweep", "min": 0, "max": 2000, "step": 500}}}):
            with self.assertRaises(ValueError):
                webapi.build_plan(bad, STATUS_5080)

    def test_search_plan_and_proposal(self):
        from tuner import search, webapi
        setup.save_limits({"max_watts": 300.0, "max_core_mv": 1000.0, "max_fabric_mv": None})
        test1 = [{"id": "x", "name": "test1", "params": {"core": 400, "mem": 2000, "rel": -50,
                                                          "vmin": 0, "alt": -70}}]
        with mock.patch.object(profiles, "read_profiles", return_value=test1):
            d = webapi.search_defaults("test1")
            self.assertTrue(d["selection"]["core"]["on"])
            self.assertEqual(d["selection"]["cap"]["hi"], 300.0)
            plan = webapi.search_plan({"profile": "test1", "selection": d["selection"],
                                       "runs": 24})
        self.assertEqual(plan["count"], 24)
        self.assertEqual(plan["space"]["dims"]["rel"]["hi"], -50)    # voltage limit
        self.assertIn("rel capped", " ".join(plan["notes"]))
        # ALT moves with REL, keeping test1's -20 mV gap (both limits at the same voltage)
        self.assertTrue(all(p["alt"] == p["rel"] - 20 for p in plan["points"]))
        with self.assertRaises(ValueError):
            webapi.search_plan({"profile": "test1", "selection": {"bogus": {"on": True}}})

        # pretend the explore pass ran, then analyse it and plan the edge search
        db = Path(tempfile.mkdtemp()) / "j.db"
        j = state.Journal(db)
        for p in plan["points"]:
            tid = j.begin_trial(p, "b")
            crash = p["core"] - 2 * p["rel"] > 600
            j.finish_trial(tid, {"status": "crash" if crash else "ok",
                                 "score": None if crash else 9000 + 3 * p["core"],
                                 "watts_avg": None if crash else p["cap"] - 1})
        cfg = setup.load()
        cfg["search"] = {"explore": {"space": plan["space"], "points": plan["points"],
                                     "start": plan["start"]}}
        setup.save(cfg)
        real_journal = state.Journal
        with mock.patch.object(state, "Journal", lambda *a, **k: real_journal(db)):
            res = webapi.analyse()
            st = webapi.search_status()
            ep = webapi.edge_plan_from({"budgets": "300, 260", "rels": "-50,-75",
                                        "core": {"lo": 300, "hi": 600, "step": 15},
                                        "held": {"mem": 1800}})
            with self.assertRaises(ValueError):    # above the 300 W limit
                webapi.edge_plan_from({"budgets": "320", "rels": "-50",
                                       "core": {"lo": 300, "hi": 600}})
        eff = {k["knob"]: k for k in res["analysis"]["effects"]["knobs"]}
        self.assertTrue(eff["core"]["score"]["clear"])
        self.assertAlmostEqual(eff["core"]["score"]["per_unit"], 300, delta=15)
        # three budgets from the 300 W limit down to the card minimum (248.4 W)
        self.assertEqual(res["suggested_edge"]["budgets"], [300.0, 275.0, 250.0])
        self.assertEqual(res["suggested_edge"]["rels"][0], -50.0)   # within the voltage limit
        self.assertEqual(len(res["suggested_edge"]["rels"]), 4)
        self.assertEqual(st["explore"]["done"], 24)
        self.assertEqual(ep["base"]["mem"], 1800.0)
        self.assertEqual(ep["base"]["alt"], -70)

    def test_run_refuses_without_limits_or_elevation(self):
        from tuner import webapi
        body = {"knobs": {"core": {"mode": "sweep", "min": 0, "max": 50, "step": 50}}}
        with mock.patch.object(setup, "load_limits", return_value=None):
            with self.assertRaises(RuntimeError):
                webapi.start_run(body)
        with mock.patch.object(setup, "load_limits", return_value={"max_watts": None}), \
             mock.patch("tuner.mvolt.is_admin", return_value=False), \
             mock.patch.object(webapi, "_external_run", return_value=None):
            with self.assertRaises(PermissionError):
                webapi.start_run(body)


class SearchTests(unittest.TestCase):
    START = {"core": 400, "mem": 2000, "rel": -50, "alt": -70, "xbar_offset": 400,
             "sys_offset": 300}

    def test_space_respects_driver_and_voltage_limits(self):
        from tuner import search
        sel = search.default_selection(STATUS_5080_FULL, self.START, {"max_watts": 300})
        sp = search.build_space(STATUS_5080_FULL, self.START, sel,
                                {"max_watts": 300, "max_core_mv": 1000})
        d = sp["dims"]
        self.assertEqual((d["core"]["lo"], d["core"]["hi"]), (200, 590))
        self.assertEqual(d["rel"]["hi"], -50)          # 1000 mV limit on a 1050 mV stock
        self.assertEqual((d["cap"]["lo"], d["cap"]["hi"]), (250, 300))   # card min 248.4 W
        self.assertEqual(d["mem"]["hi"], 3000)         # driver max
        pts = search.lhs_points(sp, 20, seed=3)
        self.assertEqual(len(pts), 20)
        self.assertTrue(all(sp["dims"]["core"]["lo"] <= p["core"] <= sp["dims"]["core"]["hi"]
                            and p["alt"] == p["rel"] - 20 for p in pts))
        self.assertGreater(len({p["core"] for p in pts}), 12)   # evenly spread

    def test_whole_front_with_raised_limits(self):
        """Suggested 5080 limits: 396 W, core 1100 mV - REL may now go above stock."""
        from tuner import edge, search
        specs = {s["key"]: s for s in setup.limit_specs(STATUS_5080_FULL)}
        self.assertEqual(specs["max_watts"]["suggested"], 396.0)
        self.assertEqual(specs["max_core_mv"]["suggested"], 1100.0)
        self.assertEqual(specs["max_fabric_mv"]["suggested"], 1000.0)     # fabric at stock
        lim = {"max_watts": 396.0, "max_core_mv": 1100.0, "max_fabric_mv": 1000.0}
        sel = search.default_selection(STATUS_5080_FULL, self.START, lim)
        sp = search.build_space(STATUS_5080_FULL, self.START, sel, lim)
        self.assertEqual((sp["dims"]["rel"]["lo"], sp["dims"]["rel"]["hi"]), (-150, 50))
        self.assertEqual((sp["dims"]["cap"]["lo"], sp["dims"]["cap"]["hi"]), (250, 395))  # 5 W grid
        plan = edge.suggest_plan(STATUS_5080_FULL, self.START, lim)
        self.assertEqual(plan["budgets"], [395.0, 335.0, 275.0])
        # undervolt is where efficiency and core headroom are: start +25 down to -90
        self.assertEqual(plan["rels"], [-25.0, -65.0, -100.0, -140.0])
        p = edge.point(plan, 395.0, 50.0, 500.0)
        self.assertEqual((p["rel"], p["alt"]), (50.0, 30))                 # ALT linked
        # no voltage limit at all: stop at mVolt+'s 1.15 V standard ceiling (REL +100)
        sp = search.build_space(STATUS_5080_FULL, self.START, sel, {"max_watts": 396.0})
        self.assertEqual(sp["dims"]["rel"]["hi"], 100)

    def test_explore_runs_mild_first_and_a_crash_prunes_the_rest(self):
        from tuner import orchestrator, search
        sp = {"dims": {"core": {"lo": 300, "hi": 990, "step": 15},
                       "rel": {"lo": -150, "hi": 100, "step": 5},
                       "cap": {"lo": 250, "hi": 395, "step": 5}}, "fixed": {}}
        pts = search.lhs_points(sp, 40, seed=11)
        risks = [search.risk(p, sp) for p in pts]
        self.assertEqual(risks, sorted(risks))
        self.assertLess(risks[0], 1.0)          # first run is from the mild corner
        # the crash that took the real PC down: core +615, REL -100, 290 W
        crash = {"core": 615, "rel": -100, "cap": 290}
        # riskier in every way (more core, more voltage, bigger budget) -> skipped
        self.assertIsNotNone(orchestrator.ruled_out({"core": 630, "rel": -90, "cap": 300}, [crash]))
        # a deeper undervolt or a smaller budget is safer -> still allowed
        self.assertIsNone(orchestrator.ruled_out({"core": 630, "rel": -110, "cap": 300}, [crash]))
        self.assertIsNone(orchestrator.ruled_out({"core": 630, "rel": -90, "cap": 280}, [crash]))
        later = [p for p in pts if orchestrator.ruled_out(p, [crash])]
        self.assertTrue(later)                  # a crash now prunes queued configs

    def test_manual_grid_links_alt(self):
        from tuner import knobs
        ref = knobs.link_reference({"rel": -50, "alt": -70})
        self.assertEqual(knobs.link_rails({"rel": 0}, ref), {"rel": 0, "alt": -20})
        self.assertEqual(knobs.link_rails({"core": 1}, ref), {"core": 1})

    @staticmethod
    def toy(p, rnd):
        """Stable core edge = 400 - REL (deeper undervolt holds more core, as
        measured on the RTX 5080); score from core and mem; watts from REL."""
        if p["core"] + p["rel"] > 400:
            return {"params": p, "status": "driver_reset", "score": None, "watts": None}
        return {"params": p, "status": "ok", "watts": 240 + 0.9 * p["rel"] + 0.012 * p.get("mem", 0),
                "score": 8000 + 2.4 * p["core"] + 0.35 * p.get("mem", 0) + rnd.uniform(-25, 25)}

    def test_effects_and_crash_tradeoff_recover_the_model(self):
        from tuner import analysis, search
        sp = {"dims": {"core": {"lo": 200, "hi": 590, "step": 15},
                       "rel": {"lo": -150, "hi": -50, "step": 5},
                       "mem": {"lo": 1000, "hi": 3000, "step": 100},
                       "xbar_offset": {"lo": 100, "hi": 700, "step": 50}}, "fixed": {"alt": -70}}
        rnd = __import__("random").Random(1)
        trials = [self.toy(p, rnd) for p in search.lhs_points(sp, 40, seed=4)]
        e = {k["knob"]: k for k in analysis.effects(trials)["knobs"]}
        self.assertAlmostEqual(e["core"]["score"]["per_unit"], 240, delta=25)   # per 100 MHz
        self.assertAlmostEqual(e["mem"]["score"]["per_unit"], 175, delta=25)    # per 500 MHz
        self.assertAlmostEqual(e["rel"]["watts"]["per_unit"], 9, delta=0.5)     # per 10 mV
        # XBAR has (almost) no effect: tiny next to core. The 'clear' flag is a
        # 2-standard-error test, so it can trip by chance ~1 in 20 - check size.
        self.assertLess(abs(e["xbar_offset"]["score"]["per_unit"]), 15)
        # the edge model (core, REL, budget) recovers the trade-off: the true
        # edge is core = 400 - REL, i.e. +10 MHz of core per 10 mV of undervolt
        m = analysis.edge_model(trials)
        self.assertAlmostEqual(10 * m["coef"]["rel"] / m["coef"]["core"], 10, delta=4)
        self.assertAlmostEqual(m["core_at"](0.5, -100), 500, delta=40)

    def test_edge_search_finds_each_lane_edge_and_confirms(self):
        from tuner import edge
        rnd = __import__("random").Random(3)
        plan = edge.make_plan({"core": 400, "rel": -50, "alt": -70, "mem": 2000},
                              [300, 260], [-50, -75, -100], 300, 600)
        trials, runs = [], 0
        while (p := edge.next_point(trials, plan)) is not None:
            runs += 1
            trials.append(self.toy(p, rnd))
            self.assertLess(runs, 60)
        self.assertLessEqual(runs, 40)
        self.assertGreater(edge.estimate_runs(plan, trials), 0)
        got = {(s["cap"], s["rel"]): s["edge"] for s in edge.lanes(trials, plan)}
        # true edges 450 / 475 / 500 (deeper undervolt holds more core),
        # on the 15 MHz grid starting at 300
        self.assertEqual(got[(300.0, -50.0)], 450)
        self.assertEqual(got[(300.0, -75.0)], 465)
        self.assertEqual(got[(260.0, -100.0)], 495)
        # never probed far past an edge: no crash more than 2 steps above it
        for t in trials:
            if t["status"] != "ok":
                edge_true = 400 - t["params"]["rel"]
                self.assertLessEqual(t["params"]["core"], edge_true + 30)
        cands = edge.candidates(trials, plan)
        ref = [c for c in cands if c["pick"].startswith("reference")]
        self.assertEqual([(c["cap"], c["rel"], c["core"]) for c in ref], [(300.0, -50.0, 400.0)])
        # the toy card draws < 260 W at REL -100, so 300 W would be the same point
        self.assertEqual([(c["cap"], c["rel"]) for c in cands if c not in ref], [(260.0, -100.0)])
        for c in cands:
            if c in ref:
                continue
            # edge - margin, pulled back by the model by at most 2 steps
            self.assertLessEqual(c["core"], c["edge"] - plan["margin"])
            self.assertGreaterEqual(c["core"], c["edge"] - plan["margin"] - 2 * 15)
        pts = edge.confirm_points(cands, 3)
        self.assertEqual(len(pts), 6)
        self.assertNotEqual(pts[0]["cap"], pts[1]["cap"])       # interleaved
        confirm = [self.toy(p, rnd) for p in pts]
        confirm[0] = dict(confirm[0], status="driver_reset", score=None)
        res = edge.confirm_results(trials + confirm, cands)
        self.assertIn("failed", res[0]["verdict"])
        self.assertEqual(res[1]["verdict"], "confirmed")
        # runs of the same config during the edge search count towards confirmation
        self.assertGreaterEqual(res[1]["runs"], 3)

    def test_edge_crash_counts_at_every_budget(self):
        from tuner import edge
        plan = edge.make_plan({"core": 0, "rel": 0}, [395, 335, 275], [-25], 300, 600)
        tr = [{"params": edge.point(plan, 395, -25, 435), "status": "ok", "score": 1},
              {"params": edge.point(plan, 395, -25, 450), "status": "driver_reset"}]
        for s in edge.lanes(tr, plan):
            self.assertEqual(s["edge"], 435, s)   # 335 W and 275 W never re-probe +450
        self.assertIsNone(edge.next_point(tr, plan))

    def test_core_culprit_judged_at_its_voltage(self):
        from tuner import crashes
        tr = [{"id": 1, "status": "ok", "params": {"core": 470, "rel": -145}},
              {"id": 2, "status": "ok", "params": {"core": 435, "rel": -25}},
              {"id": 3, "status": "interrupted", "note": "orphaned in state 'running' (host reboot)",
               "params": {"core": 465, "rel": -25, "cap": 275}}]
        r = crashes.rules(tr)
        self.assertEqual(r[0]["at_or_beyond"], {"core": 465.0, "rel": -25.0})
        self.assertIsNotNone(crashes.guard({"core": 465, "rel": 0, "cap": 395}, r))
        self.assertIsNone(crashes.guard({"core": 465, "rel": -65, "cap": 395}, r))

    def test_anchor_scores_per_boot(self):
        from tuner import state
        stock = {"core": 0, "rel": 0, "cap": 352}
        oc = {"core": 400, "rel": -50, "cap": 352}
        rows = [{"id": 1, "status": "ok", "score": 37800, "boot_id": "a", "params": stock},
                {"id": 2, "status": "ok", "score": 41000, "boot_id": "a", "params": oc},
                {"id": 3, "status": "ok", "score": 49300, "boot_id": "b", "params": stock},
                {"id": 4, "status": "ok", "score": 53470, "boot_id": "b", "params": oc},
                {"id": 5, "status": "ok", "score": 50000, "boot_id": "c", "params": oc}]
        a = state.anchor_scores(rows)
        # canon = median of the per-boot stock refs = 43550; same OC -> same anchored score
        self.assertAlmostEqual(a[2] / a[1], 41000 / 37800, places=4)
        self.assertAlmostEqual(a[2], a[4], delta=5)
        self.assertIsNone(a[5])                 # boot with no stock run: not comparable
        self.assertTrue(state.has_anchor(rows, "a"))
        self.assertFalse(state.has_anchor(rows, "c"))
        # no stock run anywhere -> unchanged
        self.assertEqual(state.anchor_scores(rows[1:2])[2], 41000)

    def test_candidate_pulled_back_by_model_after_lucky_pass(self):
        from tuner import edge
        plan = edge.make_plan({"core": 0, "rel": 0}, [300], [0], 300, 600)
        mk = lambda c, st, rel=0: {"params": edge.point(plan, 300, rel, c), "status": st,
                                   "score": 1000 + c if st == "ok" else None}
        # history with slightly more voltage says crashes start around +420
        # (a crash with MORE voltage says nothing directly about this lane) ...
        trials = [mk(c, "ok", 5) for c in (300, 330, 360, 390)] * 2 +                  [mk(c, "crash", 5) for c in (420, 435, 450, 465)]
        # ... then the 300 W lane gets one lucky pass at +480 and crashes at +495
        trials += [mk(c, "ok") for c in (330, 390, 480)] + [mk(495, "crash")]
        c = edge.candidates(trials, plan)[0]
        self.assertEqual(c["edge"], 480)
        self.assertEqual(c["core"], 435)       # 480 - 15 margin - 2 steps (the most it may pull)

    def test_edge_lane_sharing_and_dead_lane(self):
        from tuner import edge
        plan = edge.make_plan({"core": 0, "rel": 0}, [300], [0, -50], 100, 400)
        crash = lambda c, r: {"params": edge.point(plan, 300, r, c), "status": "crash"}
        ok = lambda c, r: {"params": edge.point(plan, 300, r, c), "status": "ok", "score": 1}
        # a crash at 250 with LESS voltage (REL -50) bounds the REL 0 lane too
        s = edge.lane_state(edge.lane_observations([ok(100, 0), crash(250, -50)], plan),
                            plan, 300, 0)
        self.assertEqual((s["lo"], s["hi"]), (100, 250))
        self.assertLessEqual(s["next"], 100 + 2 * 15)       # creeps, never jumps
        # a pass with MORE voltage counts for the -50 lane
        s = edge.lane_state(edge.lane_observations([ok(220, 0)], plan), plan, 300, -50)
        self.assertEqual(s["lo"], 220)
        # a lane that crashes at the bottom of the range is reported, not looped on
        s = edge.lane_state(edge.lane_observations([crash(100, 0)], plan), plan, 300, 0)
        self.assertTrue(s["done"])
        self.assertIn("unstable even", s["note"])


class CrashLogTests(unittest.TestCase):
    """Modelled on the real RTX 5080 run: two hard freezes on apply."""

    def rows(self):
        def t(i, status, p, note=None, boot="b2"):
            return {"id": i, "status": status, "params": p, "note": note, "boot_id": boot,
                    "started_utc": "2026-11-11T02:00:00+00:00"}
        # the most aggressive values that ran clean in the real study (REL +95 ran clean)
        clean = dict(core=425, mem=2900, rel=95, xbar_offset=550, sys_offset=450, cap=396)
        return [
            t(5, "interrupted", dict(core=615, mem=2400, rel=-100, xbar_offset=400,
                                     sys_offset=750, cap=290),
              "orphaned in state 'applying' (host reboot); 6 NVIDIA driver error(s) "
              "logged before the reboot", boot="b1"),
            t(12, "ok", clean),
            t(14, "driver_reset", dict(core=560, mem=1800, rel=10, xbar_offset=150,
                                       sys_offset=50, cap=320)),
            t(21, "applying", dict(core=275, mem=2700, rel=65, xbar_offset=700,
                                   sys_offset=250, cap=270), boot="b2"),
            t(22, "cancelled", dict(core=1), "orphaned in state 'running' (process death)"),
        ]

    def test_classification_and_culprits(self):
        from tuner import crashes
        lg = crashes.log(self.rows(), current_boot="b3")
        kinds = {e["id"]: e["kind"] for e in lg["entries"]}
        self.assertEqual(kinds, {5: "hard_apply", 14: "driver_reset", 21: "hard_apply"})
        e5 = next(e for e in lg["entries"] if e["id"] == 5)
        self.assertEqual(set(e5["culprits"]), {"core", "sys_offset"})   # xbar 400, mem 2400 ran clean
        self.assertEqual(e5["driver_errors"], 6)
        e21 = next(e for e in lg["entries"] if e["id"] == 21)
        self.assertEqual(set(e21["culprits"]), {"xbar_offset"})        # core 275 is mild
        self.assertEqual(e21["culprits"]["xbar_offset"]["highest_clean"], 550)
        # the current boot's own in-flight trial is not a crash
        self.assertNotIn(21, {e["id"] for e in crashes.log(self.rows(), "b2")["entries"]})

    def test_guard_blocks_only_the_culprit_combination(self):
        from tuner import crashes
        rules = crashes.rules(self.rows(), current_boot="b3")
        self.assertEqual(len(rules), 2)
        block = lambda **p: crashes.guard(p, rules)
        self.assertIsNotNone(block(core=200, xbar_offset=700))        # XBAR alone did it
        self.assertIsNotNone(block(core=300, xbar_offset=750))
        self.assertIsNone(block(core=300, xbar_offset=650))
        # both culprits, at the frozen config's voltage and budget (or more)
        self.assertIsNotNone(block(core=615, sys_offset=750, rel=-100, cap=290))
        self.assertIsNone(block(core=615, sys_offset=500, rel=-100, cap=290))  # one culprit alone
        # a core culprit is voltage-dependent: a deeper undervolt is not blocked
        self.assertIsNone(block(core=615, sys_offset=750, rel=-120, cap=290))
        self.assertIn("xbar_offset >= +700", " ".join(crashes.describe_rule(r) for r in rules))


class ServeTests(unittest.TestCase):
    def test_static_whitelist(self):
        ok = serve.Handler.static_allowed
        self.assertTrue(ok("/web/index.html"))
        self.assertTrue(ok("/data/live.json?x=1"))
        self.assertFalse(ok("/state/journal.db"))
        self.assertFalse(ok("/web/../state/journal.db"))
        self.assertFalse(ok("/web/%2e%2e/state/config.json"))
        self.assertFalse(ok("/.git/config"))

    def test_profile_request_validation(self):
        self.assertEqual(serve.validate_profile_request(
            {"name": "tuner-best-1", "params": {"core": 150}}), ("tuner-best-1", {"core": 150}))
        # no tuner-imposed offset ceilings
        self.assertEqual(serve.validate_profile_request(
            {"name": "x", "params": {"core": 1000, "mem": 3000}})[1], {"core": 1000, "mem": 3000})
        for bad in ({"name": "x", "params": {"mem": "lots"}},
                    {"name": "x", "params": {"bogus": 1}},
                    {"name": "x", "params": {"boost_lock": True}},
                    {"name": "x", "params": {}},
                    {"name": "<script>", "params": {"core": 1}},
                    []):
            with self.assertRaises(ValueError):
                serve.validate_profile_request(bad)


class SamplerTests(unittest.TestCase):
    def test_parse(self):
        P = kombustor.PowerSampler.parse
        self.assertEqual(P("77.12, 1650, 64\n55.0, 300, 40\n"), (77.12, 1650.0, 64.0))
        self.assertIsNone(P("[N/A], 1650, 64"))
        self.assertIsNone(P(""))


class VdemandTests(unittest.TestCase):
    BASE = {"cap": 335.0, "rel": -100.0, "core": 465.0}

    def _plan(self):
        from tuner import vdemand
        return vdemand.make_plan([{"params": self.BASE, "label": "A"},
                                  {"params": dict(self.BASE, cap=275.0), "label": "B"}],
                                 [-10, -20, -30], repeats=2)

    def _t(self, params, status="ok", score=40000, watts=300):
        return {"params": params, "status": status, "score": score, "watts": watts, "clk_avg": 2700}

    def test_walks_from_zero_interleaved_and_stops_at_crash(self):
        from tuner import vdemand
        plan = self._plan()
        self.assertEqual(plan["values"], [0.0, -10.0, -20.0, -30.0])
        a = lambda v: vdemand.point(plan["bases"][0], v)
        b = lambda v: vdemand.point(plan["bases"][1], v)
        self.assertEqual(vdemand.next_point([], plan), a(0))
        tr = [self._t(a(0))]
        self.assertEqual(vdemand.next_point(tr, plan), a(0))      # second repeat
        tr.append(self._t(a(0), watts=300))
        self.assertEqual(vdemand.next_point(tr, plan), b(0))      # interleave: B catches up
        tr += [self._t(b(0)), self._t(b(0))]
        self.assertEqual(vdemand.next_point(tr, plan), a(-10))
        tr += [self._t(a(-10), watts=294), self._t(a(-10), watts=294)]
        tr.append(self._t(b(-10), status="driver_reset", score=None, watts=None))
        lanes = vdemand.lanes(tr, plan)
        self.assertTrue(lanes[1]["done"])
        self.assertEqual(lanes[1]["crashed_at"], -10.0)
        self.assertEqual(lanes[1]["edge"], 0.0)
        self.assertEqual(lanes[0]["rows"][1]["d_watts"], -6.0)
        self.assertEqual(vdemand.next_point(tr, plan), a(-20))
        tr += [self._t(a(-20)), self._t(a(-20)), self._t(a(-30)), self._t(a(-30))]
        self.assertIsNone(vdemand.next_point(tr, plan))
        self.assertEqual(vdemand.lanes(tr, plan)[0]["edge"], -30.0)

    def test_memory_sweep_steps_up_from_the_configs_own_offset(self):
        from tuner import vdemand
        base = dict(self.BASE, mem=2000.0)
        plan = vdemand.make_plan([{"params": base, "label": "A"}], [2600, 2300], repeats=1, key="mem")
        self.assertEqual(plan["values"], [2000.0, 2300.0, 2600.0])
        self.assertEqual(vdemand.next_point([], plan), base)          # reference first
        tr = [self._t(base, watts=300), self._t(dict(base, mem=2300.0), watts=302, score=40300)]
        self.assertEqual(vdemand.next_point(tr, plan), dict(base, mem=2600.0))
        self.assertNotIn("core_vdemand", vdemand.next_point(tr, plan))
        tr.append(self._t(dict(base, mem=2600.0), status="driver_reset", score=None, watts=None))
        lane = vdemand.lanes(tr, plan)[0]
        self.assertEqual((lane["edge"], lane["crashed_at"]), (2300.0, 2600.0))
        self.assertEqual(lane["rows"][1]["d_score"], 300)
        self.assertIsNone(vdemand.next_point(tr, plan))
        with self.assertRaises(ValueError):                           # below the base's own
            vdemand.make_plan([{"params": base}], [1500], key="mem")
        with self.assertRaises(ValueError):                           # bases disagree
            vdemand.make_plan([{"params": base}, {"params": dict(base, mem=1000.0)}], [2500], key="mem")

    def test_plan_rejects_positive_and_empty(self):
        from tuner import vdemand
        with self.assertRaises(ValueError):
            vdemand.make_plan([{"params": self.BASE}], [10])
        with self.assertRaises(ValueError):
            vdemand.make_plan([], [-10])

    def test_deeper_demand_is_riskier_for_guards(self):
        from tuner import crashes
        failed = dict(self.BASE, core_vdemand=-30.0)
        self.assertTrue(knobs.at_least_as_aggressive(dict(self.BASE, core_vdemand=-40.0), failed))
        self.assertFalse(knobs.at_least_as_aggressive(dict(self.BASE, core_vdemand=-20.0), failed))
        self.assertFalse(knobs.at_least_as_aggressive(dict(self.BASE), failed))   # no key = 0
        rule = {"at_or_beyond": {"core_vdemand": -30.0}}
        self.assertIsNotNone(crashes.guard(dict(self.BASE, core_vdemand=-35.0), [rule]))
        self.assertIsNone(crashes.guard(dict(self.BASE, core_vdemand=-25.0), [rule]))

    def test_vdemand_runs_stay_out_of_edge_lanes_and_model(self):
        from tuner import analysis, edge
        plan = edge.make_plan({"core": 400, "rel": -100, "cap": 335}, [335], [-100], 315, 615)
        t = self._t(edge.point(plan, 335, -100, 465), status="driver_reset")
        t["params"]["core_vdemand"] = -30.0
        self.assertEqual(edge.lane_observations([t], plan), {})
        clean = [self._t({"core": c, "rel": -100, "cap": 335}) for c in range(300, 450, 15)]
        bad = [self._t({"core": c, "rel": -100, "cap": 335}, status="crash") for c in (500, 520, 540)]
        m1 = analysis.edge_model(clean + bad)
        vd = [dict(self._t({"core": 400, "rel": -100, "cap": 335, "core_vdemand": -60.0},
                           status="crash")) for _ in range(5)]
        m2 = analysis.edge_model(clean + bad + vd)
        self.assertAlmostEqual(m1["core_at"](0.5, -100, 335), m2["core_at"](0.5, -100, 335))

    def test_stale_restore_point_resets_new_controls(self):
        from tuner import orchestrator
        rp = {"profile": None, "params": {"core": 0, "mem": 0, "rel": 0.0}, "release_cap": False}
        added = orchestrator.extend_restore_point(
            rp, {"core", "rel", "cap", "xbar_offset", "core_vdemand", "msvdd_clock_ratio"})
        self.assertEqual(rp["params"]["core_vdemand"], 0)
        self.assertEqual(rp["params"]["xbar_offset"], 0)
        self.assertTrue(rp["release_cap"])
        self.assertNotIn("msvdd_clock_ratio", rp["params"])
        self.assertEqual(orchestrator.extend_restore_point(rp, {"core_vdemand", "cap"}), [])

    def test_new_sweep_ignores_runs_before_it(self):
        from tuner import vdemand
        plan = vdemand.make_plan([{"params": self.BASE}], [-10], repeats=1, since_id=5)
        old = dict(self._t(vdemand.point(plan["bases"][0], 0)), id=5)
        self.assertEqual(vdemand.next_point([old], plan), vdemand.point(plan["bases"][0], 0))
        new = dict(old, id=6)
        self.assertEqual(vdemand.next_point([old, new], plan), vdemand.point(plan["bases"][0], -10))

    def test_odd_high_score_state_is_not_compared(self):
        from tuner import state
        rows = [{"id": i, "status": "ok", "score": 40000 + i, "clk_avg": 2750, "boot_id": "b",
                 "params": {"core": 400}} for i in range(8)]
        rows.append({"id": 99, "status": "ok", "score": 48000, "clk_avg": 2400, "boot_id": "b",
                     "params": {"core": 400}})
        self.assertEqual(state.odd_state_ids(rows), {99})
        self.assertIsNone(state.anchor_scores(rows)[99])
        self.assertIsNotNone(state.anchor_scores(rows)[3])



class GravityMarkTests(unittest.TestCase):
    CLOSED = (Path(__file__).parent / "gravitymark_closed_early.txt").read_text(encoding="utf-8")

    def test_closed_early_log_has_no_score_but_is_done(self):
        from tuner import gravitymark
        got = gravitymark.parse_log(self.CLOSED)
        self.assertTrue(got["started"] and got["done"])
        self.assertIsNone(got["score"])           # "-fps 1"/"Count: 1" are setup, not results
        self.assertAlmostEqual(got["bench_s"], 25.7, places=1)

    def test_score_after_start_is_read(self):
        from tuner import gravitymark
        text = self.CLOSED.replace("M:  27.476 s: Clearing Scene",
                                   "M:  27.400 s: Score: 41,523 FPS: 248.6\n"
                                   "M:  27.476 s: Clearing Scene")
        got = gravitymark.parse_log(text)
        self.assertEqual((got["score"], got["avg_fps"]), (41523, 248.6))

    def test_finished_pass(self):
        from tuner import gravitymark
        got = gravitymark.parse_log((Path(__file__).parent / "gravitymark_finished.txt")
                                    .read_text(encoding="utf-8"))
        self.assertEqual((got["score"], got["avg_fps"], got["done"]), (19394, 116.1, True))
        self.assertAlmostEqual(got["bench_s"], 167.1, places=1)   # "2:48.679" timestamps

    def test_frame_times(self):
        from tuner import gravitymark
        lines = ["      0:     1.615730  0.000000  0"] + [
            f"{i:7d}: {1.6 + i * 0.004:12.6f}  {0.020 if i == 100 else 0.004:.6f}  0"
            for i in range(1, 101)]
        ft = gravitymark.parse_times("\n".join(lines))
        self.assertEqual(ft["frames"], 100)                      # frame 0 skipped
        self.assertAlmostEqual(ft["avg_fps"], 1000 * 100 / (99 * 4 + 20), places=1)
        self.assertEqual(ft["low1_fps"], 50.0)
        real = gravitymark.parse_times((Path(__file__).parent / "gravitymark_times_sample.txt")
                                       .read_text(encoding="utf-8"))
        self.assertEqual(real["frames"], 299)
        self.assertGreater(real["avg_fps"], 50)

    def test_no_score_aborts_the_run_instead_of_counting_a_crash(self):
        from tuner import gravitymark
        j = state.Journal(Path(tempfile.mkdtemp()) / "j.db")
        with mock.patch("tuner.mvolt.apply_params", return_value={}),              mock.patch("tuner.gravitymark.run_benchmark", side_effect=gravitymark.NoScore("closed")),              mock.patch("tuner.kombustor.driver_resets_since", return_value=0),              mock.patch.object(orchestrator, "log", quiet),              mock.patch.object(config, "LIVE_JSON", Path(tempfile.mkdtemp()) / "live.json"):
            with self.assertRaises(gravitymark.NoScore):
                orchestrator.run_trial(j, mock.Mock(), {"core": 300}, "boot", False,
                                       bench={"tool": "gravitymark"}, limits={})
        self.assertEqual(j.all_trials()[-1]["status"], "aborted")
        from tuner import crashes
        self.assertIsNone(crashes.classify(j.all_trials()[-1]))

    def test_journal_per_benchmark(self):
        self.assertEqual(config.journal_db("kombustor").name, "journal.db")
        self.assertEqual(config.journal_db("kombustor", dry=True).name, "journal-dryrun.db")
        self.assertEqual(config.journal_db("gravitymark").name, "journal-gravitymark.db")

    def test_switch_stashes_and_restores_each_study(self):
        from tuner import webapi
        d = Path(tempfile.mkdtemp())
        setup.save({"version": 3, "limits": {"max_watts": 330},
                    "search": {"explore": {"profile": "test1"}},
                    "bench": config.bench_defaults("kombustor")})
        with mock.patch.object(config, "STATE", d),              mock.patch.object(config, "LIVE_JSON", d / "live.json"),              mock.patch.object(webapi, "run_status", return_value={"running": False}):
            webapi.switch_bench("gravitymark")
            saved = setup.load()
            self.assertEqual(config.bench_tool(), "gravitymark")
            self.assertEqual(saved["search"], {})
            self.assertEqual(saved["bench"]["args"][0], "-d3d11")
            self.assertEqual(saved["limits"], {"max_watts": 330})        # limits are shared
            self.assertTrue((d / "journal-gravitymark.db").exists())
            webapi.switch_bench("kombustor")
            saved = setup.load()
            self.assertEqual(saved["search"], {"explore": {"profile": "test1"}})
            self.assertEqual(saved["studies"]["gravitymark"]["bench"]["tool"], "gravitymark")
            with self.assertRaises(ValueError):
                webapi.switch_bench("furmark")
        config.CONFIG_FILE.unlink()

    def test_pareto_front_keeps_clean_configs_only(self):
        from tuner import seed
        t = lambda i, p, w, s, st="ok": {"id": i, "params": p, "watts": w, "score": s, "status": st}
        a, b, c, d = {"core": 300.0}, {"core": 400.0}, {"core": 350.0}, {"core": 500.0}
        trials = [t(1, a, 250, 35000), t(2, b, 300, 40000), t(3, b, 302, 40400),
                  t(4, c, 310, 39000),                        # dominated: more watts, less score
                  t(5, d, 330, 43000), t(6, d, None, None, "driver_reset"),   # crashed once
                  t(7, {"core": 450.0}, 320, None)]           # odd-state run: no comparable score
        front = seed.pareto_front(trials)
        self.assertEqual([f["params"] for f in front], [a, b])
        self.assertEqual(front[1]["score"], 40200)
        ref = {"params": {"core": 200.0}, "pick": "reference (test1)"}
        cands = seed.candidates(trials, "Kombustor", ref)
        self.assertEqual(cands[0]["pick"], "reference (test1)")
        self.assertEqual([c["params"] for c in cands[1:]], [a, b])
        self.assertTrue(cands[1]["pick"].startswith("Kombustor front"))

    def test_probe_marks_ignored_rails_unwritable(self):
        flagged = {"success": True, "rail_offsets_ignored_by_driver": True}
        with mock.patch("tuner.mvolt.is_admin", return_value=True), \
             mock.patch("tuner.mvolt.apply", return_value=flagged), \
             mock.patch.object(capability, "CACHE", Path(tempfile.mkdtemp()) / "w.json"):
            w = capability.writable_map(force=True, status=STATUS_5080)
        self.assertIs(w["rel"], False)
        self.assertIs(w["core"], True)          # the flag only concerns rail offsets

    def test_dead_rel_is_kept_out_of_explore_edge_and_seed(self):
        from tuner import webapi
        prof = [{"id": "p1", "name": "test1", "params": {"core": 400.0, "rel": -50.0, "alt": -70.0,
                                                          "vmin": 0.0, "mem": 2000.0}}]
        with mock.patch.object(webapi, "_cached_writable", return_value={"rel": False, "core": True}), \
             mock.patch.object(webapi, "_status", return_value=(STATUS_5080, None)), \
             mock.patch("tuner.profiles.read_profiles", return_value=prof), \
             mock.patch.object(config, "bench_tool", return_value="gravitymark"):
            start = webapi._start_params("test1", STATUS_5080)
            self.assertEqual(start, {"core": 400.0, "mem": 2000.0})
            d = webapi.search_defaults("test1")
            self.assertNotIn("rel", d["selection"])
            self.assertEqual(d["dead_rails"], ["rel"])
            with self.assertRaises(ValueError):
                webapi.search_plan({"profile": "test1", "selection": {"rel": {"on": True, "lo": -50,
                                                                              "hi": 0, "step": 5}}})
            body = {"budgets": "300", "core": {"lo": 300, "hi": 500}, "profile": "test1"}
            with self.assertRaises(ValueError):
                webapi.edge_plan_from(dict(body, rels="-50"))
            plan = webapi.edge_plan_from(dict(body, rels="0"))
            self.assertEqual(plan["rels"], [0.0])
            self.assertIsNone(webapi.seed_candidates())
        from tuner import edge
        sp = edge.suggest_plan(STATUS_5080, {"core": 400.0}, {"max_watts": 330}, [], fixed_rels=[0])
        self.assertEqual(sp["rels"], [0.0])

    def test_validate_takes_the_other_studys_confirmed_configs(self):
        from tuner import webapi
        d = Path(tempfile.mkdtemp())
        good = {"core": 365.0, "cap": 330.0, "rel": 0.0}
        bad = {"core": 380.0, "cap": 330.0, "rel": 0.0}
        cands = [{"params": good, "cap": 330.0, "rel": 0.0, "core": 365.0, "pick": "efficiency"},
                 {"params": bad, "cap": 330.0, "rel": 0.0, "core": 380.0, "pick": "x"}]
        setup.save({"version": 3, "bench": config.bench_defaults("kombustor"),
                    "studies": {"gravitymark": {"search": {"confirm": {"candidates": cands}}}}})
        with mock.patch.object(config, "STATE", d):
            j = state.Journal(config.journal_db("gravitymark"))
            for p, st in ((good, "ok"), (good, "ok"), (bad, "ok"), (bad, "driver_reset")):
                tid = j.begin_trial(p, "b", None)
                j.finish_trial(tid, {"status": st, "score": 52000 if st == "ok" else None,
                                     "watts_avg": 230.0 if st == "ok" else None})
            j.close()
            out = webapi.validate_candidates()
        config.CONFIG_FILE.unlink()
        self.assertEqual([c["params"] for c in out], [good])      # only the confirmed one
        self.assertTrue(out[0]["pick"].startswith("GravityMark confirmed"))
        self.assertEqual(out[0]["src_watts"], 230.0)

    def test_busy_gpu_stops_before_the_benchmark_and_is_not_a_crash(self):
        from tuner import gravitymark
        with mock.patch("tuner.kombustor.gpu_utilisation", return_value=95.0):
            with self.assertRaises(kombustor.GPUBusy):
                gravitymark.run_benchmark()
            with self.assertRaises(kombustor.GPUBusy):
                kombustor.run_benchmark()
        j = state.Journal(Path(tempfile.mkdtemp()) / "j.db")
        with mock.patch("tuner.mvolt.apply_params", return_value={}), \
             mock.patch("tuner.gravitymark.run_benchmark", side_effect=kombustor.GPUBusy("busy")), \
             mock.patch.object(orchestrator, "log", quiet), \
             mock.patch.object(config, "LIVE_JSON", Path(tempfile.mkdtemp()) / "live.json"):
            with self.assertRaises(kombustor.GPUBusy):
                orchestrator.run_trial(j, mock.Mock(), {"core": 300}, "boot", False,
                                       bench={"tool": "gravitymark"}, limits={})
        self.assertEqual(j.all_trials()[-1]["status"], "aborted")
        with mock.patch("tuner.kombustor.gpu_utilisation", return_value=4.0):
            kombustor.require_idle_gpu()                 # idle desktop: fine

    def test_under_loaded_run_is_not_a_result(self):
        # Real numbers: stock 47,218 @ 2870 MHz; clean +365 53,015 @ 3179; spoiled 22,645 @ 3227.
        ref = 47218 / 2870
        self.assertIsNone(state.underloaded(53015, 3179, ref))
        self.assertEqual(state.underloaded(22645, 3227, ref), 0.43)
        self.assertIsNotNone(state.underloaded(45126, 3196, ref))
        self.assertIsNone(state.underloaded(22645, 3227, None))          # no anchor yet
        j = state.Journal(Path(tempfile.mkdtemp()) / "j.db")
        tid = j.begin_trial({"core": 0}, "boot", None)
        j.finish_trial(tid, {"status": "ok", "score": 47218, "clk_avg": 2870, "watts_avg": 220})
        res = kombustor.BenchResult(exit_code=0, elapsed_s=170, score=22645)
        with mock.patch("tuner.mvolt.apply_params", return_value={}), \
             mock.patch("tuner.kombustor.run_benchmark",
                        return_value=(res, {"watts_avg": 130.4, "clk_avg": 3227})), \
             mock.patch("tuner.mvolt.status", return_value=dict(STATUS, core_offset_mhz=365)), \
             mock.patch("tuner.kombustor.driver_resets_since", return_value=0), \
             mock.patch.object(orchestrator, "log", quiet), \
             mock.patch.object(config, "LIVE_JSON", Path(tempfile.mkdtemp()) / "live.json"):
            with self.assertRaises(kombustor.GPUBusy):
                orchestrator.run_trial(j, mock.Mock(), {"core": 365}, "boot", False, limits={})
        last = j.all_trials()[-1]
        self.assertEqual(last["status"], "aborted")
        self.assertIsNone(last["score"])

    def test_zero_rail_offset_is_the_same_config_as_none(self):
        a = {"core": 365.0, "cap": 330.0, "rel": 0.0}
        self.assertEqual(state.param_key(a), state.param_key({"core": 365.0, "cap": 330.0}))
        self.assertNotEqual(state.param_key(dict(a, rel=-50.0)), state.param_key(a))
        self.assertEqual(state.param_key(dict(a, msvdd_rel=0)), state.param_key(a))

    def test_validate_stage_plan(self):
        from tuner import webapi
        cand = {"params": {"core": 365.0, "cap": 330.0, "rel": 0.0}, "cap": 330.0, "rel": 0.0,
                "core": 365.0, "edge": None, "pick": "GravityMark confirmed · efficiency"}
        own = {"candidates": [{"params": {"core": 1.0}}], "points": [{"core": 1.0}]}
        setup.save({"version": 3, "bench": config.bench_defaults("kombustor"),
                    "search": {"confirm": own}})
        with mock.patch.object(webapi, "validate_candidates", return_value=[cand]):
            args = webapi.prepare_run({"mode": "validate", "repeats": 2, "simulate": True}, None)
        saved = setup.load()
        config.CONFIG_FILE.unlink()
        self.assertEqual(args[-5:], ["--search", "confirm", "--trials", "2", "--simulate"])
        self.assertEqual(saved["search"]["confirm"]["points"], [cand["params"]] * 2)
        self.assertEqual(saved["search"]["confirm"]["source"], "validate-gravitymark")
        self.assertEqual(saved["search"]["confirm_prev"], own)     # Kombustor's own plan kept

    def test_run_queue_switches_then_plans_the_next_stage(self):
        from tuner import pipeline, webapi
        setup.save({"version": 3, "bench": config.bench_defaults("gravitymark"),
                    "queue": [{"switch": "kombustor"}, {"mode": "validate", "repeats": 2},
                              {"switch": "gravitymark"}]})
        seen = []
        with mock.patch.object(webapi, "_switch_tool", side_effect=seen.append), \
             mock.patch.object(webapi, "prepare_run", return_value=["tuner", "--search", "confirm"]) as pr, \
             mock.patch("tuner.mvolt.status", return_value=STATUS_5080):
            args = pipeline.run_next(quiet, launch=False)
            self.assertEqual(args, ["tuner", "--search", "confirm"])
            self.assertEqual(seen, ["kombustor"])
            self.assertEqual(pr.call_args.args[0]["mode"], "validate")
            self.assertEqual(pipeline.pending(), [{"switch": "gravitymark"}])
            self.assertIsNone(pipeline.run_next(quiet, launch=False))   # only a switch left
            self.assertEqual(seen, ["kombustor", "gravitymark"])
            self.assertEqual(pipeline.pending(), [])
            # A step that cannot start ends the queue but still applies its switches.
            setup.save({"version": 3, "queue": [{"switch": "kombustor"}, {"mode": "validate"},
                                                {"mode": "confirm"}, {"switch": "gravitymark"}]})
            seen.clear()
            pr.side_effect = ValueError("nothing confirmed")
            self.assertIsNone(pipeline.run_next(quiet, launch=False))
            self.assertEqual(seen, ["kombustor", "gravitymark"])
            self.assertEqual(pipeline.pending(), [])
        config.CONFIG_FILE.unlink()

    def test_hard_freezes_guard_across_benchmarks(self):
        d = Path(tempfile.mkdtemp())
        with mock.patch.object(config, "STATE", d):
            k = state.Journal(config.journal_db("kombustor"))
            tid = k.begin_trial({"xbar_offset": 700.0}, "boot-a", None)
            k.finish_trial(tid, {"status": "interrupted",
                                 "note": "host reboot: was 'applying'"})
            ok = k.begin_trial({"xbar_offset": 400.0}, "boot-a", None)
            k.finish_trial(ok, {"status": "ok", "score": 40000})
            k.close()
            other = state.hardware_trials(exclude=config.journal_db("gravitymark"))
        from tuner import crashes
        rules = crashes.rules(other)
        self.assertTrue(rules)
        self.assertIsNotNone(crashes.guard({"xbar_offset": 750.0}, rules))
        self.assertIsNone(crashes.guard({"xbar_offset": 450.0}, rules))


if __name__ == "__main__":
    unittest.main()
