"""Paths and constants.

Nothing here may be machine-specific: the location of mVolt+ and Kombustor is
discovered at runtime by `preflight`, and every downstream module asks for it
through the functions in this file.

Environment overrides (checked first):
    MVOLT_EXE        full path to mVolt+.exe
    KOMBUSTOR_EXE    full path to MSI-Kombustor-x64.exe
    KOMBUSTOR_DIR    directory containing MSI-Kombustor-x64.exe
    GRAVITYMARK_EXE  full path to GravityMark.exe
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
STOP_FLAG = DATA / "stop.flag"      # created by the web UI's Stop button
RUN_LOG = DATA / "run.log"          # output of a run started from the web UI
RECOMMENDATION_JSON = DATA / "recommendation.json"

# Trial protocol (overridable from config.json)
BENCH_ARGS = [
    "-width=1920", "-height=1080", "-gpu_index=0",
    "-glfurrymsi", "-benchmark", "-fullscreen",
]
BENCH_TIMEOUT_S = 240
HEARTBEAT_INTERVAL_S = 2.0
MAX_CONSECUTIVE_BOOTS_WITHOUT_PROGRESS = 3
# Driver resets pile up: on the reference RTX 5080 three of four hard freezes
# (black screen, manual reset) came right after 2+ driver resets in the same
# boot (23-26 NVIDIA driver errors logged first). After this many unstable runs
# since boot the run stops and asks for a Windows restart; Start resumes it.
MAX_RESETS_PER_BOOT = 2
RESET_COOLDOWN_S = 30   # idle time after a driver reset before the next apply

# Top of the default core sweep when no range is given (`--max-core`). This is a
# starting range, NOT a limit: the tuner imposes no ceilings of its own. The only
# bounds are the ranges the driver reports through mVolt+ (which mVolt+ enforces
# anyway), plus configs learned to be unstable from this machine's own crashes.
DEFAULT_SWEEP_CORE_MHZ = 200


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
        ROOT.parent.parent / "mVolt+.exe",
        Path(os.environ.get("USERPROFILE", "")) / "Downloads" / "mVolt+.exe",  # the download is a bare exe
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


# ---- which benchmark ----------------------------------------------------
# Each benchmark scores in its own units, so each keeps its own journal (its
# results, plans and restore point). Hard freezes and driver resets are
# hardware events, not benchmark ones: those are read across every journal.
BENCH_TOOLS = {
    "kombustor": "MSI Kombustor (furry donut, OpenGL)",
    "gravitymark": "GravityMark (Direct3D 11)",
}
DEFAULT_BENCH_TOOL = "kombustor"
GRAVITYMARK_ARGS = [
    "-d3d11", "-benchmark", "1", "-close", "1", "-fullscreen", "1", "-screen", "0",
    "-temporal", "1", "-fps", "1", "-info", "1", "-sensors", "1",
]
GRAVITYMARK_TIMEOUT_S = 330   # a pass renders ~167 s; load and exit add a few


def bench_tool() -> str:
    """The benchmark selected in state/config.json (bench.tool)."""
    import json
    try:
        saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        tool = ((saved or {}).get("bench") or {}).get("tool")
    except Exception:
        tool = None
    return tool if tool in BENCH_TOOLS else DEFAULT_BENCH_TOOL


def journal_db(tool: str | None = None, dry: bool = False) -> Path:
    """Kombustor keeps the original file names, so its history is unchanged."""
    tool = tool or bench_tool()
    stem = "journal" if tool == "kombustor" else f"journal-{tool}"
    return STATE / f"{stem}{'-dryrun' if dry else ''}.db"


def bench_defaults(tool: str) -> dict:
    if tool == "gravitymark":
        return {"tool": tool, "args": list(GRAVITYMARK_ARGS),
                "timeout_s": GRAVITYMARK_TIMEOUT_S, "nvsmi_index": 0}
    return {"tool": "kombustor", "args": list(BENCH_ARGS),
            "timeout_s": BENCH_TIMEOUT_S, "nvsmi_index": 0}


def gravitymark_exe() -> Path:
    env = _env_path("GRAVITYMARK_EXE")
    if env and env.is_file():
        return env
    for base in (os.environ.get("ProgramFiles", "C:/Program Files"),
                 os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")):
        p = Path(base) / "GravityMark" / "bin" / "GravityMark.exe"
        if p.is_file():
            return p
    raise DependencyMissing(
        "GravityMark was not found.\n"
        "  Download: https://gravitymark.tellusim.com/\n"
        "  Install it, or set GRAVITYMARK_EXE=C:\path\to\GravityMark\bin\GravityMark.exe"
    )
