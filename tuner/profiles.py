"""Create mVolt+ profiles from tuning results.

Design rules learned the hard way (see docs/FINDINGS.md trap 14):
  * Control keys must be EXACT - mVolt+ silently drops unknown ones and saves
    "controls": {}, which looks valid and does nothing.
  * mVolt+ rewrites the document from memory, so it must be closed to edit.
  * Always back up and bump `revision`.
  * Verify `controls` is non-empty before reporting success.

This module only WRITES profiles. It never applies anything to the GPU.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from . import config

DEFAULT_CONTROLS: dict[str, dict[str, Any]] = {
    "nvvdd": {"enabled": False, "restore_default": False,
              "vmin_offset_uv": 0, "rel_offset_uv": 0, "alt_offset_uv": 0},
    "core": {"enabled": False, "offset_mhz": 0},
    "memory": {"enabled": False, "offset_mhz": 0},
    "xbar": {"enabled": False, "offset_mhz": 0},
    "board_input_current": {"enabled": False, "milliamps": 9000},
    "voltage_boost": {"enabled": False, "percent": 0},
    "sys_clock": {"enabled": False, "offset_mhz": 0},
    "video_clock": {"enabled": False, "offset_mhz": 0},
    "nvvdd_ocp": {"enabled": False, "limit_a": 81},
    "clock_range": {"enabled": False, "locked": False, "min_mhz": 0, "max_mhz": 2100},
}


class ProfileError(RuntimeError):
    pass


def profiles_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "mVolt+" / "profiles"


def profile_document() -> Path:
    """The per-GPU profile document (newest non-backup .json)."""
    d = profiles_dir()
    if not d.is_dir():
        raise ProfileError(f"mVolt+ profile directory not found: {d}")
    cands = [p for p in d.glob("*.json") if not p.name.endswith(".bak")]
    if not cands:
        raise ProfileError(f"no profile document in {d} - open mVolt+ once first")
    return max(cands, key=lambda p: p.stat().st_mtime)


def app_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq mVolt+.exe"],
                             capture_output=True, text=True, timeout=15)
        return "mVolt+.exe" in (out.stdout or "")
    except Exception:
        return False


def close_app(wait_s: float = 12.0) -> bool:
    """Ask mVolt+ to close. Returns True once it is gone."""
    if not app_running():
        return True
    subprocess.run(["taskkill", "/IM", "mVolt+.exe"], capture_output=True, timeout=20)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if not app_running():
            return True
        time.sleep(0.5)
    subprocess.run(["taskkill", "/F", "/IM", "mVolt+.exe"], capture_output=True, timeout=20)
    time.sleep(1.5)
    return not app_running()


def build_controls(params: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map a trial's params onto the exact control schema mVolt+ expects."""
    c = copy.deepcopy(DEFAULT_CONTROLS)
    if params.get("core"):
        c["core"] = {"enabled": True, "offset_mhz": int(params["core"])}
    if params.get("mem"):
        c["memory"] = {"enabled": True, "offset_mhz": int(params["mem"])}
    if params.get("rel"):
        c["nvvdd"] = {"enabled": True, "restore_default": False,
                      "vmin_offset_uv": 0,
                      "rel_offset_uv": int(params["rel"]) * 1000,
                      "alt_offset_uv": 0}
    if params.get("boost"):
        c["voltage_boost"] = {"enabled": True, "percent": int(params["boost"])}
    return c


def write_profile(name: str, params: dict[str, Any],
                  backup_dir: Path | None = None) -> dict[str, Any]:
    """Add or replace one profile. Returns a verification summary."""
    if app_running():
        raise ProfileError(
            "mVolt+ is running and would overwrite the file. "
            "Close it first (or pass --close-app)."
        )

    doc_path = profile_document()
    doc = json.loads(doc_path.read_text(encoding="utf-8"))

    backup_dir = backup_dir or (config.DATA / "backup-profile")
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(doc_path, backup_dir / f"profile-before-{name}.json")

    controls = build_controls(params)
    if not controls:
        raise ProfileError("refusing to write a profile with empty controls")

    existing = [p for p in (doc.get("profiles") or []) if p.get("id") != name]
    clobbered = [p for p in (doc.get("profiles") or []) if p.get("id") == name]
    # Never silently overwrite a profile the user made themselves.
    if clobbered and not str(name).startswith("tuner-"):
        raise ProfileError(
            f"a profile named '{name}' already exists and was not created by this "
            f"tool; refusing to overwrite it"
        )
    existing.append({
        "id": name,
        "name": name,
        "xoc": False,
        "partial": False,
        "full_snapshot": False,
        "mobile_rel_only": True,
        "confirmed_high_memory": False,
        "controls": controls,
    })
    doc["profiles"] = existing
    doc["revision"] = int(doc.get("revision") or 0) + 1
    doc_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    # verify by re-reading and asserting the controls survived
    check = json.loads(doc_path.read_text(encoding="utf-8"))
    written = next((p for p in check["profiles"] if p.get("id") == name), None)
    if not written or not written.get("controls"):
        raise ProfileError(
            f"'{name}' was written but controls came back empty - "
            f"a control key was not recognised; restore {backup_dir}"
        )

    return {"id": name, "controls": len(written["controls"]),
            "revision": check["revision"], "path": str(doc_path)}


def read_profiles() -> list[dict[str, Any]]:
    """Read the profiles that already exist in mVolt+ (does not modify them)."""
    try:
        doc = json.loads(profile_document().read_text(encoding="utf-8"))
    except (ProfileError, OSError, ValueError):
        return []

    out: list[dict[str, Any]] = []
    for p in doc.get("profiles") or []:
        c = p.get("controls") or {}
        core, mem, nv = c.get("core") or {}, c.get("memory") or {}, c.get("nvvdd") or {}
        params: dict[str, Any] = {}
        if core.get("enabled"):
            params["core"] = core.get("offset_mhz", 0)
        if mem.get("enabled"):
            params["mem"] = mem.get("offset_mhz", 0)
        if nv.get("enabled"):
            params["rel"] = int(nv.get("rel_offset_uv", 0)) // 1000
        out.append({
            "id": p.get("id"),
            "name": p.get("name"),
            "params": params,
            "enabled_controls": [k for k, v in c.items()
                                 if isinstance(v, dict) and v.get("enabled")],
        })
    return out


def active_profile() -> dict[str, Any] | None:
    """The profile mVolt+ is currently running, if any.

    Falls back to the startup profile so a user who has configured one is
    treated as having a baseline even before they apply it this session.
    """
    try:
        doc = json.loads(profile_document().read_text(encoding="utf-8"))
    except (ProfileError, OSError, ValueError):
        return None
    pid = doc.get("last_applied_profile_id") or doc.get("startup_profile_id")
    if not pid:
        return None
    for p in doc.get("profiles") or []:
        if p.get("id") == pid:
            for rp in read_profiles():
                if rp["id"] == pid:
                    return {**rp, "source": "active" if doc.get("last_applied_profile_id") else "startup"}
    return None


def pick_configs(trials: list[dict[str, Any]], ceiling: int,
                 limit: int = 3) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Choose up to `limit` recommended configs from journalled trials.

    Returns [(label, params, stats)]. Never recommends above the safe ceiling.
    """
    scored = []
    for t in trials:
        if not t.get("score"):
            continue
        params = t.get("params") or {}
        if int(params.get("core") or 0) > ceiling:
            continue
        scored.append(t)
    if not scored:
        return []

    def watts(t: dict[str, Any]) -> float:
        return float(t.get("watts") or 0.0) or 1e-9

    base = [t for t in scored if not int((t.get("params") or {}).get("core") or 0)]
    base_score = (sum(t["score"] for t in base) / len(base)) if base else min(t["score"] for t in scored)

    picks: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    used: set[str] = set()

    def add(label: str, t: dict[str, Any]) -> None:
        key = json.dumps(t.get("params") or {}, sort_keys=True)
        if key in used or len(picks) >= limit:
            return
        used.add(key)
        picks.append((label, dict(t.get("params") or {}),
                      {"score": t["score"], "watts": t.get("watts")}))

    add("best-performance", max(scored, key=lambda t: t["score"]))
    add("best-efficiency", max(scored, key=lambda t: t["score"] / watts(t)))

    # When the data has fewer distinct winners than slots (common on a
    # power-limited card where every point lands in the same place), spend the
    # remaining slot on the stock baseline so the user has something to compare.
    if len(picks) < limit:
        picks.append(("stock-baseline", {},
                      {"score": round(base_score, 1), "watts": None}))
    return picks[:limit]
