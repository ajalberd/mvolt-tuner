"""Journal, heartbeat, and boot bookkeeping.

Design notes:
  * SQLite in WAL mode so an unclean shutdown cannot lose a committed trial.
  * Every trial is recorded *before* settings are applied (status 'applying'),
    so a machine that dies mid-write is identifiable on the next boot.
  * A JSONL event log mirrors state changes for human debugging.
  * The heartbeat is a separate file read by scripts/watchdog.ps1.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import (
    ARMED, BOOT_STATE, EVENTS, HEARTBEAT, HEARTBEAT_INTERVAL_S,
    JOURNAL_DB, MAX_CONSECUTIVE_BOOTS_WITHOUT_PROGRESS,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS trials (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_utc  TEXT NOT NULL,
    finished_utc TEXT,
    status       TEXT NOT NULL,          -- applying|running|ok|crash|driver_reset|
                                         -- interrupted|aborted|cancelled|
                                         -- over_power_limit|over_voltage_limit
    params_json  TEXT NOT NULL,
    score        INTEGER,
    avg_fps      INTEGER,
    watts_avg    REAL,
    watts_max    REAL,
    clk_avg      REAL,
    clk_max      REAL,
    temp_max     REAL,
    elapsed_s    REAL,
    note         TEXT,
    boot_id      TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# REL's VMIN/ALT companions are filled from the live GPU and are not part of a
# grid point's identity.
_COMPANION_KEYS = {"vmin", "alt", "msvdd_vmin", "msvdd_alt"}

# Trials stopped or refused because they broke one of the user's limits.
OVER_LIMIT = ("over_power_limit", "over_voltage_limit")

# Outcomes that finish a grid point (it is not re-run on resume).
FINISHED = ("ok", "crash", "driver_reset", "interrupted") + OVER_LIMIT


def param_key(params: dict[str, Any]) -> str:
    """Stable identity of a grid point, independent of grid order or index."""
    core = {k: v for k, v in params.items() if k not in _COMPANION_KEYS}
    # A zero rail offset is the same config as none: the orchestrator drops
    # rail keys a driver ignores (NVIDIA 621.24), so a planned 'rel': 0 runs
    # without it and must still match its own results.
    for k in ("rel", "msvdd_rel"):
        if k in core and isinstance(core[k], (int, float)) and float(core[k]) == 0.0:
            del core[k]
    norm = {k: (float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
            for k, v in core.items()}
    return json.dumps(norm, sort_keys=True)


# ---------------------------------------------------------------------------
# per-session anchoring
# ---------------------------------------------------------------------------
# The benchmark's absolute score moves between Windows sessions far more than
# tuning does: on an RTX 5080, stock scored 37,800 in two sessions and 49,300
# in a later one, at the same settings and power. So every session (boot) gets
# a stock reference run, and scores are compared as a ratio to their own
# session's stock, rescaled to the median stock score so the numbers stay in
# familiar units. A session with no stock reference keeps its pass/crash
# result but its score is not used.

_OFFSET_KEYS = ("core", "mem", "rel", "xbar_offset", "sys_offset", "video_offset",
                "boost", "msvdd_rel", "core_vdemand", "xbar_vdemand", "sys_vdemand",
                "video_vdemand")


def is_stock(params: dict[str, Any]) -> bool:
    """No offsets applied (the power budget may differ)."""
    return all(not float(params.get(k, 0) or 0) for k in _OFFSET_KEYS)


def anchor_scores(rows: list[dict[str, Any]]) -> dict[Any, float | None]:
    """trial id -> score on a common scale, or None if its session has no anchor.

    `rows` need id, status, score, boot_id, params (and clk_avg to catch the odd
    state, see odd_state_ids). With no stock reference anywhere, scores are
    returned unchanged (nothing to anchor to). Odd-state runs get None."""
    odd = odd_state_ids(rows)
    rows = [r for r in rows if r.get("id") not in odd]
    refs: dict[str, list[float]] = {}
    for r in rows:
        if r.get("status") == "ok" and r.get("score") and is_stock(r.get("params") or {}):
            refs.setdefault(r.get("boot_id") or "", []).append(float(r["score"]))
    if not refs:
        out0: dict[Any, float | None] = {r.get("id"): r.get("score") for r in rows}
        out0.update({i: None for i in odd})
        return out0
    per_boot = {b: sum(v) / len(v) for b, v in refs.items()}
    vals = sorted(per_boot.values())
    canon = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2
    out: dict[Any, float | None] = {}
    for r in rows:
        ref = per_boot.get(r.get("boot_id") or "")
        sc = r.get("score")
        out[r.get("id")] = round(float(sc) * canon / ref, 1) if (sc and ref) else None
    out.update({i: None for i in odd})
    return out


ODD_STATE_RATIO = 1.1


def odd_state_ids(rows: list[dict[str, Any]]) -> set[Any]:
    """Runs measured in the unexplained high-score state: score per MHz of SM
    clock far above the norm. On an RTX 5080 normal runs sit at ~14-15 points
    per MHz; twice (after the 03:42 reboot, and mid-boot at trial 126) the
    card switched to ~20 - ~18% more score at ~400 MHz LOWER clock, with the
    same settings. Those scores are not comparable with anything else, and a
    same-boot stock anchor cannot catch a switch inside a boot."""
    ratios = {}
    for r in rows:
        clk = r.get("clk_avg")
        if r.get("status") == "ok" and r.get("score") and clk:
            ratios[r.get("id")] = float(r["score"]) / float(clk)
    if len(ratios) < 5:
        return set()
    vals = sorted(ratios.values())
    med = vals[len(vals) // 2]
    return {i for i, v in ratios.items() if v > med * ODD_STATE_RATIO}


def _safe_json(s: str | None) -> dict[str, Any]:
    try:
        return json.loads(s or "{}")
    except Exception:
        return {}


UNDERLOAD_RATIO = 0.9


def stock_ratio(rows: list[dict[str, Any]], boot: str) -> float | None:
    """Score per MHz of this boot's stock runs (the session anchor)."""
    r = [float(t["score"]) / float(t["clk_avg"]) for t in rows
         if t.get("status") == "ok" and t.get("score") and t.get("clk_avg")
         and t.get("boot_id") == boot and is_stock(t.get("params") or {})]
    return sum(r) / len(r) if r else None


def underloaded(score: float | None, clk: float | None, ref: float | None) -> float | None:
    """This run's score per MHz as a share of stock's, if it is too low to be a
    full-load run (the benchmark lost focus or shared the GPU), else None.
    Clean tuned runs sit at ~1.0 of stock; spoiled ones at 0.43-0.86."""
    if not score or not clk or not ref:
        return None
    share = float(score) / float(clk) / ref
    return round(share, 2) if share < UNDERLOAD_RATIO else None


def has_anchor(rows: list[dict[str, Any]], boot: str) -> bool:
    return any(r.get("status") == "ok" and r.get("score") and r.get("boot_id") == boot
               and is_stock(r.get("params") or {}) for r in rows)


def limits_key(limits: dict[str, Any] | None) -> str | None:
    if not limits:
        return None
    return json.dumps({k: limits.get(k) for k in ("max_watts", "max_core_mv", "max_fabric_mv")},
                      sort_keys=True)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def boot_id() -> str:
    """A stable identifier for the current OS boot."""
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')"],
            capture_output=True, text=True, timeout=15,
        )
        s = out.stdout.strip()
        if s:
            return s
    except Exception:
        pass
    return "unknown"


class Journal:
    def __init__(self, path: Path | None = None):
        # Default: the journal of the selected benchmark (config.bench_tool()).
        from . import config as _config
        path = Path(path) if path is not None else _config.journal_db()
        self.path = path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)
        # Added later: the user's limits at the time of each trial, so an
        # over-limit result only counts as "done" while the limits are the same.
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(trials)")}
        if "limits_json" not in cols:
            self.conn.execute("ALTER TABLE trials ADD COLUMN limits_json TEXT")
        self.conn.commit()

    # ---- trials ----
    def begin_trial(self, params: dict[str, Any], boot: str,
                    limits: dict[str, Any] | None = None) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO trials (started_utc, status, params_json, boot_id, limits_json) "
                "VALUES (?,?,?,?,?)",
                (utcnow(), "applying", json.dumps(params, sort_keys=True), boot,
                 limits_key(limits)),
            )
            self.conn.commit()
            tid = int(cur.lastrowid)
        self.event("trial_begin", {"trial": tid, "params": params})
        return tid

    def mark_running(self, tid: int) -> None:
        self._update(tid, status="running")

    def finish_trial(self, tid: int, result: dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                """UPDATE trials SET finished_utc=?, status=?, score=?, avg_fps=?,
                   watts_avg=?, watts_max=?, clk_avg=?, clk_max=?, temp_max=?,
                   elapsed_s=?, note=? WHERE id=?""",
                (
                    utcnow(), result["status"], result.get("score"), result.get("avg_fps"),
                    result.get("watts_avg"), result.get("watts_max"), result.get("clk_avg"),
                    result.get("clk_max"), result.get("temp_max"), result.get("elapsed_s"),
                    result.get("note"), tid,
                ),
            )
            self.conn.commit()
        self.event("trial_end", {"trial": tid, **result})

    def _update(self, tid: int, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(f"UPDATE trials SET {sets} WHERE id=?", (*fields.values(), tid))
            self.conn.commit()

    # ---- recovery ----
    def reclaim_orphans(self, current_boot: str) -> list[dict[str, Any]]:
        """Trials left mid-flight are marked interrupted on the next start.

        Only one orchestrator may run at a time, so any row still in
        'applying'/'running' when we start is an orphan - whether the process
        died or the whole host rebooted. The boot_id tells us which.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, params_json, status, boot_id, started_utc FROM trials "
                "WHERE status IN ('applying','running')"
            ).fetchall()
            out = []
            for tid, params_json, status, prior_boot, started in rows:
                rebooted = prior_boot != current_boot and current_boot != "unknown"
                cause = "host reboot" if rebooted else "process death"
                # A host that rebooted mid-trial is evidence against the config.
                # A dead process (closed window, kill) is not: let it be re-run.
                new_status = "interrupted" if rebooted else "cancelled"
                self.conn.execute(
                    "UPDATE trials SET status=?, finished_utc=?, note=? WHERE id=?",
                    (new_status, utcnow(), f"orphaned in state '{status}' ({cause})", tid),
                )
                out.append({"id": tid, "params": json.loads(params_json), "was": status,
                            "boot": prior_boot, "cause": cause, "started_utc": started})
            self.conn.commit()
        for r in out:
            self.event("trial_orphaned", {"trial": r["id"], "was": r["was"], "boot": r["boot"]})
        return out

    def trials(self) -> list[dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM trials ORDER BY id")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def finished_param_keys(self, limits: dict[str, Any] | None = None) -> set[str]:
        """Grid points that already have a finished outcome (not re-run on resume).

        An over-limit outcome only counts while the limits are unchanged: raise
        your watt or voltage limit and those configs get tried again.
        """
        q = (f"SELECT params_json, status, limits_json FROM trials "
             f"WHERE status IN ({','.join('?' * len(FINISHED))})")
        with self._lock:
            rows = self.conn.execute(q, FINISHED).fetchall()
        current = limits_key(limits)
        return {param_key(json.loads(p)) for p, s, lj in rows
                if s not in OVER_LIMIT or lj == current}

    def finished_counts(self, limits: dict[str, Any] | None = None) -> dict[str, int]:
        """Finished results per config (same rules as finished_param_keys)."""
        q = (f"SELECT params_json, status, limits_json FROM trials "
             f"WHERE status IN ({','.join('?' * len(FINISHED))})")
        with self._lock:
            rows = self.conn.execute(q, FINISHED).fetchall()
        current = limits_key(limits)
        out: dict[str, int] = {}
        for p, s, lj in rows:
            if s in OVER_LIMIT and lj != current:
                continue
            k = param_key(json.loads(p))
            out[k] = out.get(k, 0) + 1
        return out

    def unstable_params(self) -> list[dict[str, Any]]:
        """Configs that crashed, reset the driver, or took the host down."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT params_json FROM trials WHERE status IN "
                "('crash','driver_reset','interrupted') "
                # journals from older versions marked process deaths 'interrupted' too
                "AND COALESCE(note,'') NOT LIKE '%process death%'").fetchall()
        return [json.loads(r[0]) for r in rows]

    def finished_count(self) -> int:
        """Monotonic progress token: every finished trial, whatever its outcome."""
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM trials WHERE status NOT IN ('applying','running')"
        ).fetchone()[0])

    def all_trials(self) -> list[dict[str, Any]]:
        """Every row with params decoded (for the crash log)."""
        out = []
        for t in self.trials():
            try:
                params = json.loads(t["params_json"])
            except Exception:
                params = {}
            out.append({**{k: v for k, v in t.items() if k != "params_json"}, "params": params})
        return out

    def append_note(self, tid: int, text: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE trials SET note = COALESCE(note, '') || ? WHERE id=?", (text, tid))
            self.conn.commit()

    def scored_trials(self) -> list[dict[str, Any]]:
        """{params, score, watts, status, ...} for every trial, for picks,
        analysis and the search. `score` is anchored to its session's stock run
        (see anchor_scores); the measured value is in `score_raw`."""
        out = []
        for t in self.trials():
            try:
                params = json.loads(t["params_json"])
            except Exception:
                params = {}
            out.append({"id": t["id"], "params": params, "score_raw": t["score"],
                        "score": t["score"], "watts": t["watts_avg"], "clk_avg": t["clk_avg"],
                        "status": t["status"], "boot_id": t["boot_id"]})
        anchored = anchor_scores(out)
        for r in out:
            r["score"] = anchored.get(r["id"])
        return out

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def completed_count(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM trials WHERE status='ok'").fetchone()[0])

    def write_live(self, path: Path, running: bool = True) -> None:
        """Export a UI-friendly snapshot the web interface polls."""
        rows = self.trials()
        anchored = anchor_scores([{**t, "params": _safe_json(t["params_json"])} for t in rows])
        out_trials = []
        for t in rows:
            params = _safe_json(t["params_json"])
            out_trials.append({
                "id": t["id"],
                "params": params,
                "status": t["status"],
                # anchored to this session's stock run; raw value alongside
                "score": anchored.get(t["id"]),
                "score_raw": t["score"],
                "watts": t["watts_avg"],
                "clk": t["clk_avg"],
                "temp": t["temp_max"],
                "elapsed": t["elapsed_s"],
                "boot": t["boot_id"],
                "note": t["note"],
            })
        payload = {
            "generated_utc": utcnow(),
            "running": running,
            "completed": self.completed_count(),
            "next_index": self.get_meta("next_index", "0"),
            "trials": out_trials,
        }
        # atomic: the web UI polls this file and must never read half of it
        _atomic_write(path, json.dumps(payload, indent=2))

    # ---- meta ----
    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self.conn.commit()

    def delete_meta(self, key: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM meta WHERE key=?", (key,))
            self.conn.commit()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    # ---- events ----
    def event(self, kind: str, payload: dict[str, Any]) -> None:
        rec = {"utc": utcnow(), "kind": kind, **payload}
        try:
            with EVENTS.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
        except Exception:
            pass


class Heartbeat:
    """Writes data/heartbeat.json every few seconds while the run is making progress.

    A background thread alone would keep beating while the main thread is stuck
    in a hung apply or benchmark - exactly the case the watchdog exists for. So
    each phase declares how long it may take (`enter(phase, seconds)`), and the
    heartbeat stops refreshing once that deadline passes.
    """

    def __init__(self, interval_s: float = HEARTBEAT_INTERVAL_S):
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.phase = "starting"
        self.trial = 0
        self.deadline = time.time() + 600

    def enter(self, phase: str, max_seconds: float) -> None:
        self.phase = phase
        self.deadline = time.time() + max_seconds
        try:
            self._write()
        except Exception:
            pass

    def _write(self) -> None:
        _atomic_write(HEARTBEAT, json.dumps({
            "utc": datetime.now(timezone.utc).isoformat(),
            "phase": self.phase,
            "trial": self.trial,
            "deadline_utc": datetime.fromtimestamp(self.deadline, timezone.utc).isoformat(),
            "pid": __import__("os").getpid(),
        }))

    def _loop(self) -> None:
        while not self._stop.is_set():
            if time.time() <= self.deadline:
                try:
                    self._write()
                except Exception:
                    pass
            self._stop.wait(self.interval_s)

    def start(self) -> "Heartbeat":
        self._write()
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)

    def __enter__(self) -> "Heartbeat":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def stop_requested() -> bool:
    """True once the web UI's Stop button (or anyone) created data/stop.flag."""
    from .config import STOP_FLAG
    return STOP_FLAG.exists()


def clear_stop() -> None:
    from .config import STOP_FLAG
    STOP_FLAG.unlink(missing_ok=True)


def arm() -> None:
    ARMED.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")


def disarm() -> None:
    ARMED.unlink(missing_ok=True)


def note_boot(boot: str, progress_token: int) -> dict[str, Any]:
    """Track consecutive boots that produced no forward progress.

    `progress_token` should be a monotonically increasing marker of real work
    (e.g. the next grid index), NOT a completed-trial count - re-running the
    same config would otherwise look like progress and defeat the boot-loop
    guard.
    """
    state = {"boot_id": None, "token_at_last_boot": None, "stalled_boots": 0}
    if BOOT_STATE.exists():
        try:
            state = json.loads(BOOT_STATE.read_text(encoding="utf-8"))
        except Exception:
            pass

    if state.get("boot_id") == boot:
        return state

    first_boot = state.get("boot_id") is None
    progressed = first_boot or progress_token > int(state.get("token_at_last_boot") or 0)
    state["stalled_boots"] = 0 if progressed else int(state.get("stalled_boots") or 0) + 1
    state["boot_id"] = boot
    state["token_at_last_boot"] = progress_token
    BOOT_STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def boot_stalled(state: dict[str, Any]) -> bool:
    return int(state.get("stalled_boots") or 0) > MAX_CONSECUTIVE_BOOTS_WITHOUT_PROGRESS


def hardware_trials(exclude: Path | None = None) -> list[dict[str, Any]]:
    """Rows from the OTHER benchmarks' real journals. Hard freezes and driver
    resets are hardware events: they guard and count across benchmarks."""
    from . import config as _config
    out: list[dict[str, Any]] = []
    for tool in _config.BENCH_TOOLS:
        path = _config.journal_db(tool)
        if not path.exists() or (exclude is not None and path.resolve() == Path(exclude).resolve()):
            continue
        j = Journal(path)
        try:
            out.extend(j.all_trials())
        finally:
            j.close()
    return out
