"""Paths and constants.

Nothing here may be machine-specific: the location of mVolt+ and Kombustor is
discovered at runtime by `preflight`, and every downstream module asks for it
through the functions in this file.

Environment overrides (checked first):
    MVOLT_EXE        full path to mVolt+.exe
    KOMBUSTOR_EXE    full path to MSI-Kombustor-x64.exe
    KOMBUSTOR_DIR    directory containing MSI-Kombustor-x64.exe
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATE = ROOT / "state"
DATA.mkdir(parents=True, exist_ok=True)
STATE.mkdir(parents=True, exist_ok=True)

CONFIG_FILE = STATE / "config.json"
HEARTBEAT = DATA / "heartbeat.json"
ARMED = DATA / "armed.flag"
BOOT_STATE = DATA / "boot-state.json"
JOURNAL_DB = STATE / "journal.db"
EVENTS = STATE / "events.jsonl"
LIVE_JSON = DATA / "live.json"
RECOMMENDATION_JSON = DATA / "recommendation.json"

# Trial protocol (overridable from config.json)
BENCH_ARGS = [
    "-width=1920", "-height=1080", "-gpu_index=0",
    "-glfurrymsi", "-benchmark", "-fullscreen",
]
BENCH_TIMEOUT_S = 240
HEARTBEAT_INTERVAL_S = 2.0
MAX_CONSECUTIVE_BOOTS_WITHOUT_PROGRESS = 3

# Hard ceiling on automatically applied offsets. Evidence from the reference
# machine (RTX 2060 mobile): +1000 wedged the driver and OS, +300 hung once,
# +250 hung during apply, +200 completed, +150 is the optimum.
MAX_AUTO_CORE_MHZ = 200
VERIFIED_CORE_MHZ = 150
MAX_AUTO_MEM_MHZ = 1200


class DependencyMissing(RuntimeError):
    """Raised when a required external program cannot be located."""


def _env_path(*names: str) -> Path | None:
    for n in names:
        raw = os.environ.get(n)
        if not raw:
            continue
        p = Path(raw.strip('"'))
        if p.is_dir():
            return p
        if p.exists():
            return p
    return None


def mvolt_path() -> Path:
    """Locate mVolt+.exe or raise with instructions."""
    env = _env_path("MVOLT_EXE", "MVOLT_PATH")
    if env:
        return env if env.suffix.lower() == ".exe" else env / "mVolt+.exe"

    candidates: list[Path] = [
        ROOT / "mVolt+.exe",              # dropped into the project folder
        ROOT.parent / "mVolt+.exe",       # kept alongside the project folder
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "mVolt+" / "mVolt+.exe",
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "mVolt+" / "mVolt+.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "mVolt+" / "mVolt+.exe",
    ]
    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:
            pass

    raise DependencyMissing(
        "mVolt+.exe was not found.\n"
        "  Download: https://github.com/b00nz/mVolt/releases/latest\n"
        "  Then either place mVolt+.exe next to this project, install it, or set\n"
        "  MVOLT_EXE=C:\\path\\to\\mVolt+.exe"
    )


def kombustor_dir() -> Path:
    """Locate the MSI Kombustor install directory."""
    env = _env_path("KOMBUSTOR_DIR")
    if env:
        return env
    env = _env_path("KOMBUSTOR_EXE")
    if env:
        return env.parent if env.suffix.lower() == ".exe" else env

    roots = [
        Path(os.environ.get("ProgramFiles", "C:/Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")),
    ]
    for base in roots:
        gee = base / "Geeks3D"
        if not gee.is_dir():
            continue
        for d in sorted(gee.glob("MSI Kombustor*")):
            if (d / "MSI-Kombustor-x64.exe").is_file():
                return d

    raise DependencyMissing(
        "MSI Kombustor was not found.\n"
        "  Download: https://www.geeks3d.com/furmark/kombustor/\n"
        "  Install it, or set KOMBUSTOR_EXE=C:\\path\\to\\MSI-Kombustor-x64.exe"
    )


def kombustor_exe() -> Path:
    p = kombustor_dir() / "MSI-Kombustor-x64.exe"
    if not p.is_file():
        raise DependencyMissing(f"MSI-Kombustor-x64.exe missing from {p.parent}")
    return p


def kombustor_log() -> Path:
    return kombustor_dir() / "_kombustor_log.txt"


def kombustor_logs_dir() -> Path:
    return kombustor_dir() / "logs"
