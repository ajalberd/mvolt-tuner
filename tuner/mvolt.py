"""mVolt+ CLI driver: verified reads and writes.

Key facts learned from probing this machine:
  * every tuning call requires an elevated token
  * --status returns JSON on stdout
  * tuning calls return {"success":bool,...} on stdout, errors on stderr
  * a rejected write is silent unless you check the JSON
"""
from __future__ import annotations

import ctypes
import json
import subprocess
from typing import Any

from . import config


class MVoltError(RuntimeError):
    pass


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _run(args: list[str], timeout: int = 120, retries: int = 1) -> tuple[int, str, str]:
    """Run the CLI. Combined multi-flag writes can occasionally block, so retry once."""
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
    for s in streams:
        s = s.strip()
        if not s:
            continue
        start = s.find("{")
        if start < 0:
            continue
        try:
            return json.loads(s[start:])
        except json.JSONDecodeError:
            continue
    return None


def status() -> dict[str, Any]:
    rc, out, err = _run(["--status"])
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


def settings_args(params: dict[str, Any]) -> list[str]:
    """Translate a params dict into CLI args via the knob registry."""
    from . import knobs as _knobs
    return _knobs.build_args(params)


def apply_params(params: dict[str, Any], settle_s: float = 1.5) -> dict[str, Any]:
    import time

    args = settings_args(params)
    if not args:
        raise MVoltError("no settings to apply")
    apply(args)
    time.sleep(settle_s)
    st = status()
    _verify(params, st)
    return st


def _verify(params: dict[str, Any], st: dict[str, Any]) -> None:
    """Confirm the driver echoed back what we asked for."""
    from . import knobs as _knobs
    problems = _knobs.verify(params, st)
    if problems:
        raise MVoltError("apply did not stick: " + "; ".join(problems))
