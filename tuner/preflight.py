"""Preflight: verify the machine before any tuning happens.

A GitHub release cannot assume any particular install layout. This module
locates the two external programs, reports their versions, and produces an
actionable error when something is missing - instead of a stack trace deep in
a subprocess call.
"""
from __future__ import annotations

import ctypes
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import config


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    hint: str = ""


@dataclass
class PreflightReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def render(self) -> str:
        lines = []
        for c in self.checks:
            mark = "OK  " if c.ok else "FAIL"
            lines.append(f"  [{mark}] {c.name}: {c.detail}")
            if not c.ok and c.hint:
                for ln in c.hint.splitlines():
                    lines.append(f"         {ln}")
        return "\n".join(lines)

    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _version_of(exe: Path) -> str:
    try:
        out = subprocess.run(
            [str(exe), "--version"], capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
        )
        txt = (out.stdout or out.stderr or "").strip().splitlines()
        return txt[0].strip() if txt else "unknown"
    except Exception:
        return "unknown"


def _nvml_present() -> bool:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15,
        )
        return out.returncode == 0 and bool(out.stdout.strip())
    except Exception:
        return False


def run_checks() -> PreflightReport:
    rep = PreflightReport()

    rep.checks.append(Check(
        "Administrator",
        is_admin(),
        "elevated" if is_admin() else "not elevated",
        "Tuning writes are rejected without an elevated token.\n"
        "Re-run from an Administrator terminal.",
    ))

    try:
        mv = config.mvolt_path()
        rep.checks.append(Check("mVolt+", True, f"{mv}  ({_version_of(mv)})"))
    except config.DependencyMissing as exc:
        rep.checks.append(Check("mVolt+", False, "not found", str(exc)))

    # Only the selected benchmark has to be installed.
    tool = config.bench_tool()
    name = "GravityMark" if tool == "gravitymark" else "MSI Kombustor"
    try:
        exe = config.gravitymark_exe() if tool == "gravitymark" else config.kombustor_exe()
        rep.checks.append(Check(name, True, f"{exe}"))
    except config.DependencyMissing as exc:
        rep.checks.append(Check(name, False, "not found", str(exc)))

    rep.checks.append(Check(
        "nvidia-smi",
        _nvml_present(),
        "available" if _nvml_present() else "not available",
        "nvidia-smi supplies power/clocks/temperature. Without it the tuner\n"
        "cannot record the wattage axis; install the NVIDIA driver tools.",
    ))

    free_gb = shutil.disk_usage(config.DATA).free / (1024 ** 3)
    rep.checks.append(Check(
        "Disk space", free_gb > 0.2, f"{free_gb:.1f} GB free at {config.ROOT}",
        "At least a few hundred MB is needed for the journal and logs.",
    ))

    return rep


def require_ok() -> None:
    """Raise a single readable error if any preflight check fails."""
    rep = run_checks()
    if rep.ok:
        return
    bad = rep.failures()
    raise SystemExit(
        "Preflight failed:\n" + rep.render() +
        f"\n\n{len(bad)} problem(s) must be fixed before tuning."
    )
