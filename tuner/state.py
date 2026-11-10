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
    status       TEXT NOT NULL,          -- applying|running|ok|crash|interrupted|aborted
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
    def __init__(self, path: Path = JOURNAL_DB):
        self.path = path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- trials ----
    def begin_trial(self, params: dict[str, Any], boot: str) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO trials (started_utc, status, params_json, boot_id) VALUES (?,?,?,?)",
                (utcnow(), "applying", json.dumps(params, sort_keys=True), boot),
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
                "SELECT id, params_json, status, boot_id FROM trials "
                "WHERE status IN ('applying','running')"
            ).fetchall()
            for tid, _params, status, prior_boot in rows:
                cause = "host reboot" if prior_boot != current_boot else "process death"
                self.conn.execute(
                    "UPDATE trials SET status='interrupted', finished_utc=?, note=? WHERE id=?",
                    (utcnow(), f"orphaned in state '{status}' ({cause})", tid),
                )
            self.conn.commit()
        for r in rows:
            self.event("trial_orphaned", {"trial": r[0], "was": r[2], "boot": r[3]})
        return [{"id": r[0], "params": json.loads(r[1]), "was": r[2], "boot": r[3]} for r in rows]

    def trials(self) -> list[dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM trials ORDER BY id")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def completed_count(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM trials WHERE status='ok'").fetchone()[0])

    def write_live(self, path: Path, running: bool = True) -> None:
        """Export a UI-friendly snapshot the web interface polls."""
        rows = self.trials()
        out_trials = []
        for t in rows:
            try:
                params = json.loads(t["params_json"])
            except Exception:
                params = {}
            out_trials.append({
                "id": t["id"],
                "params": params,
                "status": t["status"],
                "score": t["score"],
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
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # ---- meta ----
    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
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
    """Writes data/heartbeat.json every few seconds while the run is alive."""

    def __init__(self, interval_s: float = HEARTBEAT_INTERVAL_S):
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.phase = "starting"
        self.trial = 0

    def _write(self) -> None:
        HEARTBEAT.write_text(json.dumps({
            "utc": datetime.now(timezone.utc).isoformat(),
            "phase": self.phase,
            "trial": self.trial,
            "pid": __import__("os").getpid(),
        }), encoding="utf-8")

    def _loop(self) -> None:
        while not self._stop.is_set():
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

    progressed = progress_token > int(state.get("token_at_last_boot") or 0)
    state["stalled_boots"] = 0 if progressed else int(state.get("stalled_boots") or 0) + 1
    state["boot_id"] = boot
    state["token_at_last_boot"] = progress_token
    BOOT_STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def boot_stalled(state: dict[str, Any]) -> bool:
    return int(state.get("stalled_boots") or 0) > MAX_CONSECUTIVE_BOOTS_WITHOUT_PROGRESS
