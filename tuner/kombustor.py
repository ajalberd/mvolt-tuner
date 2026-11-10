"""Kombustor runner, score harvesting, and nvidia-smi power sampling."""
from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import config
from .config import BENCH_ARGS, BENCH_TIMEOUT_S

SCORE_RE = re.compile(r"score\s*=>\s*(\d+)\s*points")
AVG_RE = re.compile(r"average framerate\s*=>\s*(\d+)\s*FPS")
MINMAX_RE = re.compile(r"AVG:(\d+)FPS,\s*MIN:(\d+)FPS,\s*MAX:(\d+)FPS")


@dataclass
class BenchResult:
    exit_code: int | None
    elapsed_s: float
    score: int | None = None
    avg_fps: int | None = None
    min_fps: int | None = None
    max_fps: int | None = None
    timed_out: bool = False
    crash: bool = False
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "elapsed_s": round(self.elapsed_s, 1),
            "score": self.score,
            "avg_fps": self.avg_fps,
            "min_fps": self.min_fps,
            "max_fps": self.max_fps,
            "timed_out": self.timed_out,
            "crash": self.crash,
            "note": self.note,
        }


def harvest_score() -> dict[str, int | None]:
    """Read the score block Kombustor writes at the end of a benchmark run."""
    out: dict[str, int | None] = {
        "score": None, "avg_fps": None, "min_fps": None, "max_fps": None,
    }
    try:
        text = config.kombustor_log().read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return out

    m = SCORE_RE.search(text)
    if m:
        out["score"] = int(m.group(1))
    m = AVG_RE.search(text)
    if m:
        out["avg_fps"] = int(m.group(1))
    m = MINMAX_RE.search(text)
    if m:
        out["avg_fps"] = out["avg_fps"] or int(m.group(1))
        out["min_fps"] = int(m.group(2))
        out["max_fps"] = int(m.group(3))
    return out


class PowerSampler:
    """Polls nvidia-smi on a thread for the duration of a run."""

    def __init__(self, interval_s: float = 0.5):
        self.interval_s = interval_s
        self.samples: list[tuple[float, float, float, float]] = []  # t, W, clk, temp
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        t0 = time.time()
        while not self._stop.is_set():
            try:
                p = subprocess.run(
                    ["nvidia-smi", "--query-gpu=power.draw,clocks.sm,temperature.gpu",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5,
                )
                if p.returncode == 0 and p.stdout.strip():
                    parts = [x.strip() for x in p.stdout.strip().split(",")]
                    if len(parts) >= 3:
                        self.samples.append((
                            time.time() - t0,
                            float(parts[0]), float(parts[1]), float(parts[2]),
                        ))
            except Exception:
                pass
            self._stop.wait(self.interval_s)

    def __enter__(self) -> "PowerSampler":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=3)

    def stats(self) -> dict[str, float | None]:
        if not self.samples:
            return {"watts_avg": None, "watts_max": None, "clk_avg": None,
                    "clk_max": None, "temp_max": None, "samples": 0}
        # drop the first 20% (spin-up) for averages
        n = len(self.samples)
        steady = self.samples[int(n * 0.2):] or self.samples
        return {
            "watts_avg": round(sum(s[1] for s in steady) / len(steady), 1),
            "watts_max": round(max(s[1] for s in self.samples), 1),
            "clk_avg": round(sum(s[2] for s in steady) / len(steady)),
            "clk_max": round(max(s[2] for s in self.samples)),
            "temp_max": round(max(s[3] for s in self.samples)),
            "samples": n,
        }


def run_benchmark(timeout_s: int = BENCH_TIMEOUT_S) -> tuple[BenchResult, dict[str, float | None]]:
    """Run one benchmark with power sampling. Returns (result, power stats)."""
    config.kombustor_log().unlink(missing_ok=True)

    started = time.time()
    with PowerSampler() as sampler:
        proc = subprocess.Popen([str(config.kombustor_exe()), *BENCH_ARGS])
        timed_out = False
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            proc.wait(timeout=10)
    elapsed = time.time() - started
    time.sleep(2.0)  # let the log flush

    res = BenchResult(exit_code=proc.returncode, elapsed_s=elapsed, timed_out=timed_out)
    got = harvest_score()
    res.score = got["score"]
    res.avg_fps = got["avg_fps"]
    res.min_fps = got["min_fps"]
    res.max_fps = got["max_fps"]

    if timed_out:
        res.crash = True
        res.note = "timeout"
    elif res.score is None:
        res.crash = True
        res.note = "no score in log"
    elif proc.returncode not in (0, None):
        res.crash = True
        res.note = f"exit {proc.returncode}"

    return res, sampler.stats()
