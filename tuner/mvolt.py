"""mVolt+ CLI driver: verified reads and writes.

Key facts learned from probing the reference machine and the mVolt+ guide:
  * every tuning call requires an elevated token
  * --status returns JSON on stdout; unavailable readings are null, not zero
  * tuning calls return {"success":bool,...} on stdout, errors on stderr
  * a rejected write is silent unless you check the JSON
  * a driver reset clears applied tuning, and mVolt+ reconnects WITHOUT
    reapplying it - so re-reading --status after a benchmark reveals a reset
  * --profile NAME applies a saved profile (cannot be combined with direct tuning)
"""
from __future__ import annotations

import ctypes
import json
import subprocess
import time
from typing import Any

from . import config


class MVoltError(RuntimeError):
    pass


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _run(args: list[str], timeout: int = 120, retries: int = 0) -> tuple[int, str, str]:
    """Run the CLI. Only reads are retried: re-sending a write after a timeout
    would apply it a second time while the first may still be in flight."""
    last: tuple[int, str, str] = (1, "", "no attempt")
    for attempt in range(retries + 1):
        try:
            p = subprocess.run(
                [str(config.mvolt_path()), *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
            return p.returncode, p.stdout or "", p.stderr or ""
        except subprocess.TimeoutExpired:
            last = (1, "", f"timeout after {timeout}s (attempt {attempt + 1})")
            continue
    return last


def _json_from(streams: tuple[str, str]) -> dict[str, Any] | None:
    """First JSON object in either stream, tolerating text before or after it."""
    dec = json.JSONDecoder()
    for s in streams:
        idx = s.find("{")
        while idx >= 0:
            try:
                obj, _end = dec.raw_decode(s, idx)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
            idx = s.find("{", idx + 1)
    return None


def status() -> dict[str, Any]:
    rc, out, err = _run(["--status"], retries=1)
    obj = _json_from((out, err))
    if obj is None:
        raise MVoltError(f"--status returned no JSON (rc={rc}, out={out[:120]!r})")
    return obj


def apply(args: list[str]) -> dict[str, Any]:
    """Apply tuning args. Raises unless the CLI reports success."""
    rc, out, err = _run(args)
    obj = _json_from((out, err))
    if obj is None:
        raise MVoltError(f"{args} -> no JSON (rc={rc}, out={out[:200]!r}, err={err[:200]!r})")
    if not obj.get("success"):
        raise MVoltError(f"{args} rejected: {obj.get('error')}")
    return obj


def apply_profile(name: str) -> dict[str, Any]:
    """Apply a saved mVolt+ profile by name or id."""
    return apply(["--profile", str(name)])


def settings_args(params: dict[str, Any]) -> list[str]:
    """Translate a params dict into CLI args via the knob registry."""
    from . import knobs as _knobs
    return _knobs.build_args(params)


class DriverIgnoresRails(MVoltError):
    """mVolt+ applied the settings but the NVIDIA driver ignored the voltage-rail
    offsets (`rail_offsets_ignored_by_driver`). Not a property of the config:
    every config with an undervolt would fail the same way, so the run stops."""


def _apply_with_lease_recovery(args: list[str], log=print) -> dict[str, Any]:
    """After an unclean shutdown mVolt+ may hold an orphaned clock-range lock and
    refuse every apply ("safety marker"). Release it only when that happens,
    instead of unconditionally clobbering a lock the user set on purpose."""
    try:
        return apply(args)
    except MVoltError as exc:
        if "safety marker" not in str(exc):
            raise
        from . import recovery
        log("apply refused with 'safety marker' - releasing orphaned clock-range lease")
        if not recovery.force_release(log=log):
            raise
        return apply(args)


def apply_params(params: dict[str, Any], settle_s: float = 1.5, log=print,
                 xoc: bool = False) -> dict[str, Any]:
    """Apply, wait, read back, verify. `xoc` selects mVolt+'s extended voltage
    range - required for voltage limits above the standard 1.15 V ceiling."""
    args = settings_args(params)
    if not args:
        raise MVoltError("no settings to apply")
    if xoc:
        args.append("--xoc")
    res = _apply_with_lease_recovery(args, log=log) or {}
    time.sleep(settle_s)
    st = status()
    try:
        _verify(params, st)
    except MVoltError as exc:
        if res.get("rail_offsets_ignored_by_driver"):
            raise DriverIgnoresRails(
                "the NVIDIA driver ignored the voltage offsets (mVolt+ reports "
                "rail_offsets_ignored_by_driver) - no undervolt can be applied with this "
                "driver. Update mVolt+ or roll the NVIDIA driver back, then Start again. "
                f"({exc})") from exc
        raise
    return st


def _verify(params: dict[str, Any], st: dict[str, Any]) -> None:
    """Confirm the driver echoed back what we asked for."""
    from . import knobs as _knobs
    problems = _knobs.verify(params, st)
    if problems:
        raise MVoltError("apply did not stick: " + "; ".join(problems))
