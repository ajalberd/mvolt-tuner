"""Create mVolt+ profiles from tuning results.

Design rules learned the hard way (see docs/FINDINGS.md trap 14):
  * Control keys must be EXACT - mVolt+ silently drops unknown ones and saves
    "controls": {}, which looks valid and does nothing. So only keys from
    KNOWN_CONTROLS are ever written, and a params dict containing a knob that
    has no known profile key is refused instead of silently dropped.
  * mVolt+ rewrites the document from memory, so it must be closed to edit.
  * Always back up, write atomically and bump `revision`.

Profile documents live in %LOCALAPPDATA%\\mVolt+\\profiles\\<VBIOS>-<adapter>.json,
next to backups (.bak, .before-power-cap.json, .before-thermal-inputs.json) that
must never be mistaken for the live document.

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

# Fallback control set, used only when the document has no profile to copy the
# per-GPU layout from. Schema as written by mVolt+ v0.50.11 (RTX 5080).
DEFAULT_CONTROLS: dict[str, dict[str, Any]] = {
    "nvvdd": {"enabled": False, "restore_default": False,
              "vmin_offset_uv": 0, "rel_offset_uv": 0, "alt_offset_uv": 0},
    "msvdd": {"enabled": False, "restore_default": False,
              "vmin_offset_uv": 0, "rel_offset_uv": 0, "alt_offset_uv": 0},
    "core": {"enabled": False, "offset_mhz": 0},
    "memory": {"enabled": False, "offset_mhz": 0},
    "xbar": {"enabled": False, "offset_mhz": 0},
    "board_input_current": {"enabled": False, "milliamps": 9000},
    "voltage_boost": {"enabled": False, "percent": 0},
    "sys_clock": {"enabled": False, "offset_mhz": 0},
    "video_clock": {"enabled": False, "offset_mhz": 0},
    "core_voltage_demand": {"enabled": False, "offset_mv": 0},
    "xbar_voltage_demand": {"enabled": False, "offset_mv": 0},
    "sys_voltage_demand": {"enabled": False, "offset_mv": 0},
    "video_voltage_demand": {"enabled": False, "offset_mv": 0},
    "nvvdd_ocp": {"enabled": False, "limit_a": 81},
    "msvdd_ocp": {"enabled": False, "limit_a": 120},
    "msvdd_clock_ratio": {"enabled": False, "raw_ratio": 65536},
    "power": {"enabled": False, "percent": 100},
    "power_cap": {"enabled": False, "milliwatts": 0, "absent": False},
    "clock_range": {"enabled": False, "locked": False, "min_mhz": 0, "max_mhz": 2100},
}
KNOWN_CONTROLS = frozenset(DEFAULT_CONTROLS)

# params key -> (profile control, field). Everything else cannot be saved.
_SIMPLE = {
    "core": ("core", "offset_mhz"),
    "mem": ("memory", "offset_mhz"),
    "xbar_offset": ("xbar", "offset_mhz"),
    "sys_offset": ("sys_clock", "offset_mhz"),
    "video_offset": ("video_clock", "offset_mhz"),
    "boost": ("voltage_boost", "percent"),
    "core_vdemand": ("core_voltage_demand", "offset_mv"),
    "xbar_vdemand": ("xbar_voltage_demand", "offset_mv"),
    "sys_vdemand": ("sys_voltage_demand", "offset_mv"),
    "video_vdemand": ("video_voltage_demand", "offset_mv"),
    "nvvdd_ocp": ("nvvdd_ocp", "limit_a"),
    "msvdd_ocp": ("msvdd_ocp", "limit_a"),
    "power": ("power", "percent"),
}
_RAILS = {"nvvdd": ("rel", "vmin", "alt"), "msvdd": ("msvdd_rel", "msvdd_vmin", "msvdd_alt")}
_NVVDD_KEYS = {"rel", "vmin", "alt"}
_MSVDD_KEYS = {"msvdd_rel", "msvdd_vmin", "msvdd_alt"}
RATIO_ONE = 65536  # msvdd_clock_ratio.raw_ratio is 16.16 fixed point
SAVABLE = (frozenset(_SIMPLE) | _NVVDD_KEYS | _MSVDD_KEYS
           | {"clock_max", "cap", "msvdd_clock_ratio"})

_BACKUP_MARKERS = (".bak", ".before-", ".tmp", ".rtss")


class ProfileError(RuntimeError):
    pass


def profiles_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "mVolt+" / "profiles"


def _documents(d: Path) -> list[Path]:
    return [p for p in d.glob("*.json")
            if not any(m in p.name.lower() for m in _BACKUP_MARKERS)]


def profile_document(adapter_id: str | None = None) -> Path:
    """The per-GPU profile document.

    Preference: the file whose name contains the adapter id (env MVOLT_ADAPTER_ID
    or the argument), then the only document, then the newest one.
    """
    d = profiles_dir()
    if not d.is_dir():
        raise ProfileError(f"mVolt+ profile directory not found: {d}")
    cands = _documents(d)
    if not cands:
        raise ProfileError(f"no profile document in {d} - open mVolt+ once first")
    adapter_id = adapter_id or os.environ.get("MVOLT_ADAPTER_ID")
    if adapter_id:
        hits = [p for p in cands if str(adapter_id).lower() in p.name.lower()]
        if hits:
            return max(hits, key=lambda p: p.stat().st_mtime)
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


def build_controls(params: dict[str, Any],
                   template: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Map a trial's params onto the exact control schema mVolt+ expects.

    `template` is the controls object of an existing profile on the same GPU.
    Copying it (with every control disabled) keeps controls this module does not
    model - fans, thermal limits - in the exact shape mVolt+ wrote them.
    """
    unsavable = sorted(k for k in params if k not in SAVABLE)
    if unsavable:
        raise ProfileError(
            f"cannot save {', '.join(unsavable)} to a profile: no known profile key "
            f"(mVolt+ would silently drop it). Apply those with the CLI instead.")
    if template:
        c = copy.deepcopy(template)
        for v in c.values():
            if isinstance(v, dict) and "enabled" in v:
                v["enabled"] = False
        for k, v in DEFAULT_CONTROLS.items():
            c.setdefault(k, copy.deepcopy(v))
    else:
        c = copy.deepcopy(DEFAULT_CONTROLS)

    for key, (ctl, field) in _SIMPLE.items():
        if key in params and params[key] is not None:
            c[ctl] = {**c[ctl], "enabled": True, field: int(round(float(params[key])))}
    for rail, (rel, vmin, alt) in _RAILS.items():
        if any(k in params for k in (rel, vmin, alt)):
            c[rail] = {**c[rail], "enabled": True, "restore_default": False,
                       "vmin_offset_uv": int(round(float(params.get(vmin, 0)) * 1000)),
                       "rel_offset_uv": int(round(float(params.get(rel, 0)) * 1000)),
                       "alt_offset_uv": int(round(float(params.get(alt, 0)) * 1000))}
    if params.get("cap") is not None:
        c["power_cap"] = {**c["power_cap"], "enabled": True, "absent": False,
                          "milliwatts": int(round(float(params["cap"]) * 1000))}
    if params.get("msvdd_clock_ratio") is not None:
        c["msvdd_clock_ratio"] = {"enabled": True,
                                  "raw_ratio": int(round(float(params["msvdd_clock_ratio"])
                                                         * RATIO_ONE))}
    if params.get("clock_max"):
        c["clock_range"] = {**c["clock_range"], "enabled": True, "locked": True,
                            "min_mhz": int(params.get("clock_min", 0)),
                            "max_mhz": int(params["clock_max"])}
    return c


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_profile(name: str, params: dict[str, Any],
                  backup_dir: Path | None = None) -> dict[str, Any]:
    """Add or replace one profile. Returns a summary."""
    if app_running():
        raise ProfileError(
            "mVolt+ is running and would overwrite the file. "
            "Close it first (or pass --close-app)."
        )
    if not any(v for v in params.values()):
        raise ProfileError("refusing to write a profile that changes nothing")

    build_controls(params)   # raises on unsavable params before anything is read

    doc_path = profile_document()
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    profiles_list = doc.get("profiles") or []
    template = next((p for p in profiles_list if p.get("id") != name), {})
    controls = build_controls(params, template.get("controls"))

    clobbered = [p for p in profiles_list if p.get("id") == name]
    # Never silently overwrite a profile the user made themselves.
    if clobbered and not str(name).startswith("tuner-"):
        raise ProfileError(
            f"a profile named '{name}' already exists and was not created by this "
            f"tool; refusing to overwrite it"
        )

    backup_dir = backup_dir or (config.DATA / "backup-profile")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"{doc_path.stem}-before-{name}-{stamp}.json"
    shutil.copy2(doc_path, backup)

    # Mirror per-GPU flags from the user's own profiles rather than assuming a
    # laptop: mobile_rel_only is true only on mobile parts.
    entry = {
        "id": name,
        "name": name,
        "xoc": False,
        "partial": False,
        "full_snapshot": False,
        "mobile_rel_only": bool(template.get("mobile_rel_only", False)),
        "confirmed_high_memory": False,
        "controls": controls,
    }
    doc["profiles"] = [p for p in profiles_list if p.get("id") != name] + [entry]
    doc["revision"] = int(doc.get("revision") or 0) + 1
    _atomic_write(doc_path, json.dumps(doc, indent=2))

    # This proves the file is valid JSON with our entry in it. Whether mVolt+
    # accepts the keys is guaranteed only by writing KNOWN_CONTROLS; confirm in
    # the app (Details) after reopening it.
    check = json.loads(doc_path.read_text(encoding="utf-8"))
    written = next((p for p in check["profiles"] if p.get("id") == name), None)
    if not written or not written.get("controls"):
        raise ProfileError(f"'{name}' did not survive the write; restore {backup}")

    enabled = [k for k, v in written["controls"].items() if v.get("enabled")]
    return {"id": name, "controls": len(enabled), "enabled": enabled,
            "revision": check["revision"], "path": str(doc_path), "backup": str(backup)}


def _uv_to_mv(v: Any) -> float:
    mv = round(float(v or 0) / 1000.0, 3)
    return int(mv) if float(mv).is_integer() else mv


def read_profiles() -> list[dict[str, Any]]:
    """Read the profiles that already exist in mVolt+ (does not modify them)."""
    try:
        doc = json.loads(profile_document().read_text(encoding="utf-8"))
    except (ProfileError, OSError, ValueError):
        return []

    reverse = {ctl: (key, field) for key, (ctl, field) in _SIMPLE.items()}
    out: list[dict[str, Any]] = []
    for p in doc.get("profiles") or []:
        c = p.get("controls") or {}
        params: dict[str, Any] = {}
        unmodelled: list[str] = []
        for ctl, v in c.items():
            if not isinstance(v, dict) or not v.get("enabled"):
                continue
            if ctl in reverse:
                key, field = reverse[ctl]
                params[key] = v.get(field, 0)
            elif ctl in _RAILS:
                rel, vmin, alt = _RAILS[ctl]
                params[rel] = _uv_to_mv(v.get("rel_offset_uv"))
                params[vmin] = _uv_to_mv(v.get("vmin_offset_uv"))
                params[alt] = _uv_to_mv(v.get("alt_offset_uv"))
            elif ctl == "power_cap":
                params["cap"] = round(float(v.get("milliwatts") or 0) / 1000.0, 3)
            elif ctl == "msvdd_clock_ratio":
                params["msvdd_clock_ratio"] = round(float(v.get("raw_ratio") or 0) / RATIO_ONE, 4)
            elif ctl == "clock_range":
                params["clock_max"] = v.get("max_mhz")
            else:
                unmodelled.append(ctl)
        out.append({
            "id": p.get("id"),
            "name": p.get("name"),
            "params": params,
            "enabled_controls": [k for k, v in c.items()
                                 if isinstance(v, dict) and v.get("enabled")],
            "unmodelled_controls": unmodelled,
        })
    return out


def profile_only_controls() -> list[dict[str, Any]]:
    """Controls mVolt+ only exposes in the GUI / profiles (no CLI flag, not in
    --status), with the value stored in the user's profiles. Shown for reference;
    the tuner cannot sweep them."""
    try:
        doc = json.loads(profile_document().read_text(encoding="utf-8"))
    except (ProfileError, OSError, ValueError):
        return []
    out = []
    for p in doc.get("profiles") or []:
        bic = (p.get("controls") or {}).get("board_input_current")
        if isinstance(bic, dict) and bic.get("milliamps") is not None:
            out.append({
                "key": "board_input_current", "label": "Board input current limit",
                "value": round(float(bic["milliamps"]) / 1000.0, 3),
                "aux": (round(float(bic["auxiliary_milliamps"]) / 1000.0, 3)
                        if bic.get("auxiliary_milliamps") is not None else None),
                "unit": "A", "enabled_in_profile": bool(bic.get("enabled")),
                "profile": p.get("name"),
                "note": "GUI/profile only - mVolt+ has no CLI flag and --status does not "
                        "report it, so the tuner cannot vary it",
            })
            break
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
    for rp in read_profiles():
        if rp["id"] == pid:
            return {**rp, "source": "active" if doc.get("last_applied_profile_id") else "startup"}
    return None


_COMPANIONS = {"vmin", "alt", "msvdd_vmin", "msvdd_alt"}


def _is_stock(params: dict[str, Any]) -> bool:
    return not any(v for k, v in params.items() if k not in _COMPANIONS and k != "cap")


def aggregate(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per distinct config: mean score/watts over its clean repeats.

    A config with ANY crash or driver reset is marked unstable - a lucky run
    does not make it recommendable. A config that ever broke one of the user's
    limits (watts or voltage) is marked over_limit.
    """
    from .state import OVER_LIMIT, param_key

    groups: dict[str, dict[str, Any]] = {}
    for t in trials:
        params = t.get("params") or {}
        g = groups.setdefault(param_key(params), {"params": params, "scores": [],
                                                  "watts": [], "unstable": False,
                                                  "over_limit": False})
        if t.get("status") in ("crash", "driver_reset", "interrupted"):
            g["unstable"] = True
        if t.get("status") in OVER_LIMIT:
            g["over_limit"] = True
        if t.get("score") and t.get("status", "ok") == "ok":
            g["scores"].append(float(t["score"]))
            if t.get("watts") is not None:
                g["watts"].append(float(t["watts"]))
    out = []
    for g in groups.values():
        if not g["scores"]:
            continue
        n = len(g["scores"])
        mean = sum(g["scores"]) / n
        sd = (sum((s - mean) ** 2 for s in g["scores"]) / (n - 1)) ** 0.5 if n > 1 else None
        out.append({
            "params": g["params"], "n": n, "score": round(mean, 1),
            "score_sd": round(sd, 1) if sd is not None else None,
            "watts": round(sum(g["watts"]) / len(g["watts"]), 2) if g["watts"] else None,
            "unstable": g["unstable"],
            "over_limit": g["over_limit"],
        })
    return out


def pick_configs(trials: list[dict[str, Any]],
                 limit: int = 3) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Choose up to `limit` recommended configs from journalled trials.

    Returns [(label, params, stats)]. Never recommends a config that has ever
    crashed or broken the user's watt/voltage limits. The stock reference has
    empty params and is for comparison only - it is not something to save.
    """
    rows = [r for r in aggregate(trials) if not r["unstable"] and not r["over_limit"]]
    if not rows:
        return []
    # Once anything has been confirmed (measured 2+ times), recommend only from
    # those: a single pass at the stability edge is the least trustworthy result.
    confirmed = [r for r in rows if r["n"] >= 2 and not _is_stock(r["params"])]
    if confirmed:
        rows = confirmed + [r for r in rows if _is_stock(r["params"])]

    base = [r for r in rows if _is_stock(r["params"])]
    base_score = (sum(r["score"] * r["n"] for r in base) / sum(r["n"] for r in base)
                  if base else None)

    picks: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    used: set[str] = set()

    def add(label: str, r: dict[str, Any]) -> None:
        key = json.dumps(r["params"], sort_keys=True)
        if key in used or len(picks) >= limit:
            return
        used.add(key)
        picks.append((label, dict(r["params"]),
                      {"score": r["score"], "watts": r["watts"], "n": r["n"],
                       "score_sd": r["score_sd"]}))

    add("best-performance", max(rows, key=lambda r: r["score"]))
    powered = [r for r in rows if r["watts"]]
    if powered:
        add("best-efficiency", max(powered, key=lambda r: r["score"] / r["watts"]))

    if len(picks) < limit and base_score is not None:
        picks.append(("stock-baseline", {},
                      {"score": round(base_score, 1), "watts": None,
                       "n": sum(r["n"] for r in base)}))
    return picks[:limit]
