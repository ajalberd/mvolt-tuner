"""GravityMark runner: one benchmark pass, its score, and power sampling.

GravityMark writes a timestamped log to stdout. A normal pass reads:

    M:   1.797 s: Starting 3440x1440 Direct3D11 Benchmark
    ...
    M:  27.476 s: Clearing Scene
    M:  27.582 s: GravityMark 1.89 Done

"Done" is printed however the pass ended (finished, or the window was closed),
so it does not prove a score exists. The score line is read from the same log.
A finished pass (GravityMark 1.89, RTX 5080, verified) ends with:

    M:  2:48.661: Benchmark Finished
    ...
    M:  2:48.671: Score: 19394
    M:  2:48.671: Time: 167.0 s
    M:  2:48.671: FPS: 116.1

The default pass takes ~167 s of rendering (six scenes). A pass that exits
normally with no score found (the window was closed) raises `NoScore` - a
tooling problem that stops the run, never a "crash" (a crash would wrongly feed
the stability guards).

With `-times FILE` GravityMark also writes per-frame times, used here for the
average and 1%-low FPS (secondary numbers; the score is GravityMark's own).
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any

from . import config
from .kombustor import BenchResult, PowerSampler

SCORE_RE = re.compile(r"\bScore\b\s*[:=]?\s*([\d][\d,]*)", re.I)
FPS_RE = re.compile(r"\b(?:Average\s+)?FPS\b\s*[:=]?\s*([\d]+(?:\.\d+)?)", re.I)
STARTED_RE = re.compile(r"Starting .*Benchmark")
DONE_RE = re.compile(r"GravityMark [\d.]+ Done")
# "M:   1.797 s:", "M: 611.93 ms:", and past a minute "M:  2:48.661:".
TIME_RE = re.compile(r"^\w:\s+(?:(\d+):)?([\d.]+)\s*(ms|s|us)?:")
# Frame-time file: "   1234:   12.345678  0.010200  3" = index, time (s),
# frame time (s), benchmark scene (0-5).
FRAME_RE = re.compile(r"^\s*\d+:\s+[\d.]+\s+([\d.]+)")


class NoScore(RuntimeError):
    """The pass ended normally but no score could be read (closed early, or an
    output format this parser does not know). Not a GPU failure."""


def out_path() -> Path:
    return config.DATA / "gravitymark-out.txt"


def times_path() -> Path:
    return config.DATA / "gravitymark-times.txt"


def parse_log(text: str) -> dict[str, Any]:
    """Score, FPS, and whether the pass started and finished, from stdout."""
    out: dict[str, Any] = {"score": None, "avg_fps": None, "started": False,
                           "done": False, "bench_s": None}
    t_start = t_end = None
    for line in text.splitlines():
        m = TIME_RE.match(line)
        t = None
        if m:
            mins, v, unit = m.group(1), float(m.group(2)), m.group(3)
            if mins is not None:
                t = int(mins) * 60 + v
            else:
                t = v if unit in ("s", None) else v / 1000 if unit == "ms" else v / 1e6
        if STARTED_RE.search(line):
            out["started"], t_start = True, t
        if "Clearing Scene" in line and t is not None:
            t_end = t
        if DONE_RE.search(line):
            out["done"] = True
        # Score lines come after the pass started; earlier lines are setup.
        if out["started"]:
            m = SCORE_RE.search(line)
            if m:
                out["score"] = int(m.group(1).replace(",", ""))
            m = FPS_RE.search(line)
            if m and out["avg_fps"] is None:
                out["avg_fps"] = float(m.group(1))
    if t_start is not None and t_end is not None:
        out["bench_s"] = round(t_end - t_start, 1)
    return out


def parse_times(text: str) -> dict[str, float | None]:
    """Average and 1%-low FPS from GravityMark's `-times` file (frame times in
    seconds, third column). Frame 0 has no frame time and is skipped."""
    ft = []
    for line in text.splitlines():
        m = FRAME_RE.match(line)
        if m:
            v = float(m.group(1)) * 1000.0      # ms
            if v > 0:
                ft.append(v)
    if len(ft) < 10:
        return {"avg_fps": None, "low1_fps": None, "frames": len(ft)}
    avg = 1000.0 * len(ft) / sum(ft)
    worst = sorted(ft, reverse=True)[:max(1, len(ft) // 100)]
    return {"avg_fps": round(avg, 1),
            "low1_fps": round(1000.0 * len(worst) / sum(worst), 1),
            "frames": len(ft)}


def run_benchmark(timeout_s: int = config.GRAVITYMARK_TIMEOUT_S,
                  bench_args: list[str] | None = None, gpu_index: int = 0,
                  watt_limit: float | None = None,
                  stop_check=None) -> tuple[BenchResult, dict[str, float | None]]:
    """One GravityMark pass with power sampling: (result, power stats).
    Same contract as kombustor.run_benchmark, plus NoScore (see module doc)."""
    from .kombustor import require_idle_gpu
    exe = config.gravitymark_exe()
    require_idle_gpu(gpu_index)
    for p in (out_path(), times_path()):
        p.unlink(missing_ok=True)
    args = list(bench_args) if bench_args else list(config.GRAVITYMARK_ARGS)
    args += ["-times", str(times_path())]

    started = time.time()
    over: float | None = None
    timed_out = False
    with open(out_path(), "wb") as fh, \
            PowerSampler(gpu_index=gpu_index, watt_limit=watt_limit) as sampler:
        # GravityMark resolves data.zip relative to its bin directory.
        proc = subprocess.Popen([str(exe), *args], cwd=str(exe.parent),
                                stdout=fh, stderr=subprocess.STDOUT)
        while proc.poll() is None:
            if stop_check is not None and stop_check():
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                raise KeyboardInterrupt("stop requested")
            if sampler.over_limit is not None:
                over = sampler.over_limit
                proc.kill()
                break
            if time.time() - started > timeout_s:
                timed_out = True
                proc.kill()
                break
            time.sleep(0.25)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    elapsed = time.time() - started

    res = BenchResult(exit_code=proc.returncode, elapsed_s=elapsed, timed_out=timed_out)
    if over is not None:
        res.over_limit = over
        res.note = f"stopped: {over} W average > limit {watt_limit:g} W"
        return res, sampler.stats()

    text = out_path().read_text(encoding="utf-8", errors="replace") if out_path().exists() else ""
    got = parse_log(text)
    try:
        ft = parse_times(times_path().read_text(encoding="utf-8", errors="replace"))
    except FileNotFoundError:
        ft = {"avg_fps": None, "low1_fps": None, "frames": 0}
    res.score = got["score"]
    fps = got["avg_fps"] if got["avg_fps"] is not None else ft["avg_fps"]
    res.avg_fps = round(fps) if fps is not None else None
    res.min_fps = round(ft["low1_fps"]) if ft.get("low1_fps") is not None else None

    if timed_out:
        res.crash, res.note = True, "timeout"
    elif proc.returncode not in (0, None):
        res.crash, res.note = True, f"exit {proc.returncode}"
    elif not got["done"]:
        res.crash, res.note = True, "GravityMark ended without finishing"
    elif res.score is None:
        raise NoScore(
            f"GravityMark finished (exit 0, benchmark {got['bench_s']} s) but no score was "
            f"found in {out_path().name} - the window was closed early, or the score line "
            "is in a format this parser does not know yet. Not counted as a crash.")
    return res, sampler.stats()
