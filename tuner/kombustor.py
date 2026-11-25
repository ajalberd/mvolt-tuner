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
    over_limit: float | None = None   # rolling-average watts that broke the user's limit

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

    # Watt limit: judged on a rolling average so a single transient spike does
    # not stop a run, and only after warm-up (load ramps and shader compiles).
    WINDOW = 6          # samples (~3 s at 0.5 s)
    WARMUP_S = 5.0

    def __init__(self, interval_s: float = 0.5, gpu_index: int = 0,
                 watt_limit: float | None = None):
        self.interval_s = interval_s
        self.gpu_index = gpu_index
        self.watt_limit = watt_limit
        self.over_limit: float | None = None   # rolling average that broke the limit
        self.samples: list[tuple[float, float, float, float]] = []  # t, W, clk, temp
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def check_limit(self) -> float | None:
        """Rolling-average watts if it is over the limit, else None."""
        if self.watt_limit is None:
            return None
        recent = [s for s in self.samples if s[0] >= self.WARMUP_S][-self.WINDOW:]
        if len(recent) < self.WINDOW:
            return None
        avg = sum(s[1] for s in recent) / len(recent)
        return round(avg, 1) if avg > self.watt_limit else None

    @staticmethod
    def parse(stdout: str) -> tuple[float, float, float] | None:
        """One `power,clock,temp` line; None for '[N/A]' or malformed output."""
        line = (stdout or "").strip().splitlines()[:1]
        if not line:
            return None
        parts = [x.strip() for x in line[0].split(",")]
        if len(parts) < 3:
            return None
        try:
            return float(parts[0]), float(parts[1]), float(parts[2])
        except ValueError:
            return None

    def _loop(self) -> None:
        t0 = time.time()
        while not self._stop.is_set():
            try:
                # -i pins the query to one GPU: on a multi-GPU box the unpinned
                # query returns one line per GPU and never parsed.
                p = subprocess.run(
                    ["nvidia-smi", "-i", str(self.gpu_index),
                     "--query-gpu=power.draw,clocks.sm,temperature.gpu",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5,
                )
                got = self.parse(p.stdout) if p.returncode == 0 else None
                if got:
                    self.samples.append((time.time() - t0, *got))
                    if self.over_limit is None:
                        self.over_limit = self.check_limit()
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


class GPUBusy(RuntimeError):
    """Something else is using the GPU before the benchmark starts (a game, a
    video encode). Its result would be meaningless, so the run stops."""


BUSY_UTIL = 30.0      # % GPU utilisation that counts as "in use" at idle


def gpu_utilisation(gpu_index: int = 0, samples: int = 4, interval_s: float = 0.5) -> float | None:
    """Average GPU utilisation over a couple of seconds, or None if unreadable."""
    vals = []
    for i in range(samples):
        try:
            p = subprocess.run(["nvidia-smi", "-i", str(gpu_index),
                                "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=10)
            vals.append(float(p.stdout.strip().splitlines()[0]))
        except Exception:
            pass
        if i < samples - 1:
            time.sleep(interval_s)
    return sum(vals) / len(vals) if vals else None


def require_idle_gpu(gpu_index: int = 0) -> None:
    util = gpu_utilisation(gpu_index)
    if util is not None and util > BUSY_UTIL:
        raise GPUBusy(f"the GPU is already {util:.0f}% busy before the benchmark - another "
                      "app (a game?) is using it, so scores would be meaningless. Close it, "
                      "then press Start; the study resumes where it stopped.")


def driver_resets_since(since_epoch: float) -> int | None:
    """Count NVIDIA driver resets (TDR / Xid) in the System log since a moment.

    Event 4101 from 'Display' is Windows' "display driver stopped responding and
    has recovered"; nvlddmkm 13/14/153 are Xid errors. None means the log could
    not be read, which is not the same as "no reset".
    """
    from datetime import datetime, timezone
    iso = datetime.fromtimestamp(since_epoch - 2, tz=timezone.utc).isoformat()
    cmd = (
        f"$t=[datetime]::Parse('{iso}').ToLocalTime(); "
        "@(Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=$t; "
        "ProviderName='Display','nvlddmkm'} -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Id -in 4101,13,14,153 }).Count"
    )
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, text=True, timeout=30)
        return int(p.stdout.strip().splitlines()[-1])
    except Exception:
        return None


def driver_errors_between(start_iso: str, end_iso: str) -> int | None:
    """NVIDIA driver errors (Xid / TDR) logged in a time window - used after a
    reboot to record what the driver reported before the PC went down."""
    cmd = (
        f"$a=[datetime]::Parse('{start_iso}').ToLocalTime(); "
        f"$b=[datetime]::Parse('{end_iso}').ToLocalTime(); "
        "@(Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=$a; EndTime=$b; "
        "ProviderName='Display','nvlddmkm'} -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Id -in 4101,13,14,153 }).Count"
    )
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, text=True, timeout=30)
        return int(p.stdout.strip().splitlines()[-1])
    except Exception:
        return None


def run_benchmark(timeout_s: int = BENCH_TIMEOUT_S, bench_args: list[str] | None = None,
                  gpu_index: int = 0, watt_limit: float | None = None,
                  stop_check=None) -> tuple[BenchResult, dict[str, float | None]]:
    """Run one benchmark with power sampling. Returns (result, power stats).

    With `watt_limit`, the run is stopped as soon as the rolling average board
    power goes over it; the result then has `over_limit` set and no score.
    """
    require_idle_gpu(gpu_index)
    config.kombustor_log().unlink(missing_ok=True)
    args = list(bench_args) if bench_args else list(BENCH_ARGS)

    started = time.time()
    over: float | None = None
    with PowerSampler(gpu_index=gpu_index, watt_limit=watt_limit) as sampler:
        proc = subprocess.Popen([str(config.kombustor_exe()), *args])
        timed_out = False
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
    time.sleep(2.0)  # let the log flush

    res = BenchResult(exit_code=proc.returncode, elapsed_s=elapsed, timed_out=timed_out)
    if over is not None:
        res.over_limit = over
        res.note = f"stopped: {over} W average > limit {watt_limit:g} W"
        return res, sampler.stats()

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
