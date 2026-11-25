"""Run queue: stages that start one after another without anyone at the PC.

`state/config.json` "queue" is a list of steps, run in order:

    {"switch": "kombustor"}                       select a benchmark (no run)
    {"mode": "confirm", "repeats": 3}             any Start-button body
    {"mode": "validate", "repeats": 2}

The orchestrator calls `run_next` after a run that finished CLEANLY - never
after Stop, the driver-reset stop or a failure, so the queue waits for you
then. Each stage is planned by the same code as the Start button
(webapi.prepare_run) and launched as a new process; it inherits the finishing
run's elevation, so no admin prompt is needed between stages.
"""
from __future__ import annotations

import subprocess
from typing import Any

from . import config, setup


def pending() -> list[dict[str, Any]]:
    return list((setup.load() or {}).get("queue") or [])


def _save_queue(q: list[dict[str, Any]]) -> None:
    saved = setup.load() or {}
    saved["queue"] = q
    setup.save(saved)


def run_next(log=print, launch=True) -> list[str] | None:
    """Apply switch steps, then plan and start the next run step. Returns the
    command started, or None when the queue is empty."""
    from . import mvolt, webapi
    q = pending()
    while q:
        step = q.pop(0)
        _save_queue(q)
        if "switch" in step:
            webapi._switch_tool(str(step["switch"]))
            log(f"run queue: benchmark switched to {step['switch']}")
            continue
        body = dict(step)
        st = None if body.get("simulate") else mvolt.status()
        try:
            args = webapi.prepare_run(body, st)
        except Exception as exc:
            # Nothing more runs; put the benchmark back where the queue would
            # have left it, so the UI is not stranded on a half-done step.
            log(f"run queue: {body.get('mode')} cannot start ({exc}) - stopping the queue")
            for rest in q:
                if "switch" in rest:
                    webapi._switch_tool(str(rest["switch"]))
                    log(f"run queue: benchmark switched to {rest['switch']}")
            _save_queue([])
            return None
        log(f"run queue: starting {body.get('mode')} ({len(q)} step(s) left after it)")
        if launch:
            out = open(config.RUN_LOG, "a", encoding="utf-8")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(args, cwd=str(config.ROOT), stdout=out, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, creationflags=flags)
        return args
    return None
