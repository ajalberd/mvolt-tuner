"""Knob registry: declare mVolt+ controls once, discover them at runtime.

WHY THIS EXISTS
---------------
Different GPUs expose very different sets of mVolt+ sliders. An RTX 2060
mobile has core/mem/NVVDD only; an RTX 5090 also has XBAR, SYS, VIDEO,
MSVDD, fan curves and thermal limits. Hardcoding a param list makes the
tuner single-machine.

Each Knob declares:
  key        - name used in the params dict, e.g. {"core": 200}
  build      - how to turn that param into CLI args
  readback   - how to read the applied value back out of --status
  bounds     - the driver-reported (min, max) for the control, if any
  write_only - True when the CLI can set it but --status never reports it

A knob is *available* on this machine when:
  - its readback function returns a value, or
  - it is write_only but explicitly listed by the caller
  - and, if it declares bounds, those bounds are non-degenerate

Degenerate bounds are the reliable "not on this GPU" signal: mVolt+ reports
limit_min == limit_max == 0 for controls the card does not support.

ADDING A KNOB
-------------
1. Append a Knob to KNOBS.
2. Point build() at the CLI flag (see `mVolt+.exe --help`).
3. Point readback() at the matching --status field (may be None).
4. For enumerated switches, use `kind="switch"` and map booleans in build().
Nothing else needs to change - the orchestrator sweeps whatever is available.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class Knob:
    key: str
    build: Callable[[dict[str, Any]], list[str]]
    readback: Callable[[dict[str, Any]], Any] | None
    bounds: Callable[[dict[str, Any]], tuple[float, float] | None] | None = None
    write_only: bool = False
    unit: str = ""
    group: str = "misc"
    note: str = ""
    # params that must be present together with this key
    requires: tuple[str, ...] = field(default_factory=tuple)
    # another knob that must be available for this one to make sense
    depends_on: str | None = None
    # how far the readback may differ from the request and still count as applied
    tolerance: float = 0.51


def _get(d: dict[str, Any], *path: str, default: Any = None) -> Any:
    cur: Any = d
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
        if cur is None:
            return default
    return cur


def _mv_from_uv(v: Any) -> float | None:
    return None if v is None else round(float(v) / 1000.0, 3)


# --------------------------------------------------------------------------
# individual knob declarations
# --------------------------------------------------------------------------

def _build_core(p: dict[str, Any]) -> list[str]:
    return ["--core", str(int(p["core"]))]


def _build_mem(p: dict[str, Any]) -> list[str]:
    return ["--mem", str(int(p["mem"]))]


def _build_nvvdd(p: dict[str, Any]) -> list[str]:
    # VMIN,REL,ALT[,OV] - a list of signed mV offsets
    vals = [int(p.get("vmin", 0)), int(p.get("rel", 0)), int(p.get("alt", 0))]
    if "ov" in p:
        vals.append(int(p["ov"]))
    return ["--nvvdd-offsets", ",".join(str(v) for v in vals)]


def _build_msvdd(p: dict[str, Any]) -> list[str]:
    vals = [int(p.get("msvdd_vmin", 0)), int(p.get("msvdd_rel", 0)), int(p.get("msvdd_alt", 0))]
    if "msvdd_ov" in p:
        vals.append(int(p["msvdd_ov"]))
    return ["--msvdd-offsets", ",".join(str(v) for v in vals)]


def _build_domain_offset(flag: str) -> Callable[[dict[str, Any]], list[str]]:
    def build(p: dict[str, Any]) -> list[str]:
        key = flag.lstrip("-").replace("-", "_")
        return [flag, str(int(p[key]))]
    return build


def _build_domain_vdemand(flag: str) -> Callable[[dict[str, Any]], list[str]]:
    def build(p: dict[str, Any]) -> list[str]:
        key = flag.lstrip("-").replace("-", "_")
        return [flag, str(int(p[key]))]
    return build


def _build_power_pct(p: dict[str, Any]) -> list[str]:
    return ["--power", str(int(p["power"]))]


def _build_power_cap(p: dict[str, Any]) -> list[str]:
    return ["--power-cap-watts", str(p["cap"])]


def _build_boost(p: dict[str, Any]) -> list[str]:
    return ["--boost", str(int(p["boost"]))]


def _build_boost_lock(p: dict[str, Any]) -> list[str]:
    return ["--boost-lock", "on" if p["boost_lock"] else "off"]


def _build_clock_range(p: dict[str, Any]) -> list[str]:
    return ["--clock-range", f"{int(p.get('clock_min', 0))},{int(p['clock_max'])}"]


def _build_ocp(flag: str) -> Callable[[dict[str, Any]], list[str]]:
    def build(p: dict[str, Any]) -> list[str]:
        key = flag.lstrip("-").replace("-", "_")
        return [flag, str(int(p[key]))]
    return build


KNOBS: dict[str, Knob] = {}


def _register(k: Knob) -> None:
    KNOBS[k.key] = k


_register(Knob(
    key="core", build=_build_core, unit="MHz", group="clocks",
    readback=lambda s: s.get("core_offset_mhz"),
    bounds=lambda s: (s.get("core_offset_mhz_limit_min"), s.get("core_offset_mhz_limit_max")),
    note="shifts the whole core V/F curve up; the main performance lever",
))

_register(Knob(
    key="mem", build=_build_mem, unit="MHz", group="clocks",
    readback=lambda s: s.get("memory_offset_mhz"),
    bounds=lambda s: (s.get("memory_offset_mhz_limit_min"), s.get("memory_offset_mhz_limit_max")),
    note="VRAM clock; helps memory-bound loads, costs few watts",
))

def _first(s: dict[str, Any], *names: str) -> Any:
    """First non-null field among several spellings of the same reading."""
    for n in names:
        v = s.get(n)
        if v is not None:
            return v
    return None


# The fabric domains have been seen both as `xbar_offset_mhz` (matching
# core_offset_mhz) and as `xbar_offset`. Accept either spelling consistently in
# presence, readback and bounds - a mismatch made these knobs undetectable.
def _domain_readback(k: str) -> Callable[[dict[str, Any]], Any]:
    return lambda s: _first(s, f"{k}_mhz", k)


def _domain_bounds(k: str) -> Callable[[dict[str, Any]], tuple[Any, Any]]:
    return lambda s: (_first(s, f"{k}_mhz_limit_min", f"{k}_limit_min"),
                      _first(s, f"{k}_mhz_limit_max", f"{k}_limit_max"))


for _k, _flag in (("xbar_offset", "--xbar-offset"),
                  ("sys_offset", "--sys-offset"),
                  ("video_offset", "--video-offset")):
    _register(Knob(
        key=_k, build=_build_domain_offset(_flag), unit="MHz", group="fabric",
        readback=_domain_readback(_k),
        bounds=_domain_bounds(_k),
        note="internal fabric/video clock; small effect, mostly a stability risk",
    ))

_register(Knob(
    key="rel", build=_build_nvvdd, unit="mV", group="voltage",
    readback=lambda s: _mv_from_uv(_get(s, "nvvdd_offsets_uv", "rel")),
    note="core voltage ceiling; negative = undervolt (fewer watts, less headroom)",
    requires=("vmin", "alt"),
))

_register(Knob(
    key="msvdd_rel", build=_build_msvdd, unit="mV", group="voltage",
    readback=lambda s: _mv_from_uv(_get(s, "msvdd_offsets_uv", "rel")),
    note="fabric-rail voltage ceiling (the driver sets -50 by default)",
    requires=("msvdd_vmin", "msvdd_alt"),
))

# mVolt+ v0.50.x reports these as voltage_demand_mv: {"Core":0,"Xbar":0,...}
for _k, _flag, _dep, _field in (("core_vdemand", "--core-vdemand", "core", "Core"),
                                ("xbar_vdemand", "--xbar-vdemand", "xbar_offset", "Xbar"),
                                ("sys_vdemand", "--sys-vdemand", "sys_offset", "SYS"),
                                ("video_vdemand", "--video-vdemand", "video_offset", "Video")):
    _register(Knob(
        key=_k, build=_build_domain_vdemand(_flag), unit="mV", group="voltage",
        readback=(lambda f: (lambda s: _get(s, "voltage_demand_mv", f)))(_field),
        depends_on=_dep,
        note="voltage asked for at every clock: negative = undervolt (fewer watts, "
             "can fail at any clock), positive = more voltage",
    ))

_register(Knob(
    key="power", build=_build_power_pct, unit="%", group="power",
    readback=lambda s: s.get("power_limit_percent"),
    bounds=lambda s: (s.get("power_limit_percent_limit_min"),
                      s.get("power_limit_percent_limit_max")),
    note="power limit in % of stock (your watt limit sets this for you)",
))

_register(Knob(
    key="cap", build=_build_power_cap, unit="W", group="power",
    # requested_mw is what we asked for; enforced_mw may be lower for reasons
    # unrelated to the write. Unavailable readings are null, never 0.
    readback=lambda s: (None if _get(s, "power_limits", "requested_mw") is None
                        else round(float(_get(s, "power_limits", "requested_mw")) / 1000.0, 3)),
    note="power limit in watts (your watt limit sets this for you)",
))

_register(Knob(
    key="boost", build=_build_boost, unit="%", group="voltage",
    readback=lambda s: s.get("voltage_boost_percent"),
    note="lets voltage rise above REL toward ALT/OP",
))

_register(Knob(
    key="boost_lock", build=_build_boost_lock, unit="", group="clocks",
    readback=lambda s: s.get("boost_lock"),
    note="holds the boost clock (steadier runs)",
))

_register(Knob(
    key="clock_max", build=_build_clock_range, unit="MHz", group="clocks",
    readback=lambda s: _get(s, "clock_range_mhz", "max"),
    bounds=lambda s: (_get(s, "clock_range_mhz", "limit_min"),
                      _get(s, "clock_range_mhz", "limit_max")),
    note="caps the core clock",
))

# Reported as power_channel_a: {"NVVDD OCP": {"current":300,"default":300,
# "limit_min":0,"limit_max":5001}, ...}.
for _k, _flag, _dep, _field in (("nvvdd_ocp", "--nvvdd-ocp", "rel", "NVVDD OCP"),
                                ("msvdd_ocp", "--msvdd-ocp", "msvdd_rel", "MSVDD OCP")):
    _register(Knob(
        key=_k, build=_build_ocp(_flag), unit="A", group="power",
        readback=(lambda f: (lambda s: _get(s, "power_channel_a", f, "current")))(_field),
        bounds=(lambda f: (lambda s: (_get(s, "power_channel_a", f, "limit_min"),
                                      _get(s, "power_channel_a", f, "limit_max"))))(_field),
        depends_on=_dep,
        note="rail current protection limit",
    ))

_register(Knob(
    key="msvdd_clock_ratio", build=lambda p: ["--msvdd-clock-ratio", str(p["msvdd_clock_ratio"])],
    unit="ratio", group="fabric", readback=lambda s: s.get("msvdd_clock_ratio"),
    depends_on="msvdd_rel", tolerance=0.005,
    note="how fabric clocks follow the core clock",
))


# --------------------------------------------------------------------------
# capability discovery
# --------------------------------------------------------------------------

@dataclass
class KnobStatus:
    key: str
    available: bool
    reason: str
    current: Any
    bounds: tuple[float, float] | None
    unit: str
    group: str
    write_only: bool
    note: str


def effective_bounds(b: tuple[Any, Any] | None) -> tuple[float, float] | None:
    """None means 'no usable range' - the control is absent or unbounded."""
    if not b or b[0] is None or b[1] is None:
        return None
    try:
        lo, hi = float(b[0]), float(b[1])
    except (TypeError, ValueError):
        return None
    if hi <= lo:
        return None
    return (lo, hi)


def survey(status: dict[str, Any], writable: dict[str, bool | None] | None = None) -> list[KnobStatus]:
    """Describe every registered knob against a live --status payload.

    `writable` comes from capability.probe(): a readback alone does not prove
    mVolt+ can write a control (the power limit reads back but is rejected).
    """
    out: list[KnobStatus] = []
    seen: dict[str, bool] = {}
    for key, k in KNOBS.items():
        if k.key not in _present_keys(status):
            # cheap pre-filter: skip domain knobs whose fields are absent entirely
            if k.readback is None and k.write_only:
                available, reason = True, "write-only (assumed settable)"
            else:
                available, reason = False, "field absent from --status"
        else:
            available, reason = True, "reported"

        b = effective_bounds(k.bounds(status) if k.bounds else None)
        if k.bounds is not None:
            if b is None:
                available, reason = False, "degenerate range (limit_min == limit_max)"
        cur = None
        if k.readback is not None:
            try:
                cur = k.readback(status)
            except Exception:
                cur = None
            if cur is None and available:
                available, reason = False, "readback returned null"

        if k.depends_on and not seen.get(k.depends_on, False):
            available, reason = False, f"needs {k.depends_on}, which is unavailable"
        if available and writable is not None and writable.get(key) is False:
            available, reason = False, "driver rejects writes to this control"
        seen[key] = available

        out.append(KnobStatus(key, available, reason, cur, b, k.unit, k.group,
                              k.write_only, k.note))
    return out


def _present_keys(status: dict[str, Any]) -> set[str]:
    """Keys we can judge from this particular status payload."""
    present: set[str] = set()
    if "core_offset_mhz" in status:
        present.add("core")
    if "memory_offset_mhz" in status:
        present.add("mem")
    for k in ("xbar_offset", "sys_offset", "video_offset"):
        if f"{k}_mhz" in status or k in status:
            present.add(k)
    if "nvvdd_offsets_uv" in status:
        present.add("rel")
    if "msvdd_offsets_uv" in status:
        present.add("msvdd_rel")
    if "power_limit_percent" in status:
        present.add("power")
    if "power_limits" in status:
        present.add("cap")
    if "voltage_boost_percent" in status:
        present.add("boost")
    if "boost_lock" in status:
        present.add("boost_lock")
    if "clock_range_mhz" in status:
        present.add("clock_max")
    vd = status.get("voltage_demand_mv")
    if isinstance(vd, dict):
        for k, f in (("core_vdemand", "Core"), ("xbar_vdemand", "Xbar"),
                     ("sys_vdemand", "SYS"), ("video_vdemand", "Video")):
            if f in vd:
                present.add(k)
    ch = status.get("power_channel_a")
    if isinstance(ch, dict):
        if "NVVDD OCP" in ch:
            present.add("nvvdd_ocp")
        if "MSVDD OCP" in ch:
            present.add("msvdd_ocp")
    if "msvdd_clock_ratio" in status:
        present.add("msvdd_clock_ratio")
    return present


def available_keys(status: dict[str, Any],
                   writable: dict[str, bool | None] | None = None) -> list[str]:
    return [k.key for k in survey(status, writable) if k.available]


def build_args(params: dict[str, Any]) -> list[str]:
    """Translate a params dict into mVolt+ CLI args, in registry order."""
    if "power" in params and "cap" in params:
        # mVolt+ rejects --power together with --power-cap-watts.
        raise ValueError("'power' and 'cap' are mutually exclusive in one apply")
    args: list[str] = []
    for key, k in KNOBS.items():
        if key in params:
            args += k.build(params)
    return args


# Companion values that ride along with a knob's CLI form but are not swept.
# Sweeping REL must not silently zero the user's VMIN/ALT offsets.
COMPANIONS: dict[str, tuple[str, ...]] = {
    "rel": ("vmin", "alt"),
    "msvdd_rel": ("msvdd_vmin", "msvdd_alt"),
}


# REL and ALT/OP both cap a rail's voltage (mVolt+ guide: "If REL rises while
# ALT/OP ... remains more restrictive, MAX may stay unchanged"), and on desktop
# RTX 50 ALT's baseline sits ~20 mV above REL's. So whenever REL moves, ALT
# moves by the same amount, keeping the starting gap - like mVolt+'s linked
# editing. Otherwise raising REL past ALT would silently change nothing.
RAIL_LINKS = (("rel", "alt"), ("msvdd_rel", "msvdd_alt"))


def link_reference(params: dict[str, Any]) -> dict[str, float]:
    """The starting REL/ALT pair(s) that later points are linked against."""
    out: dict[str, float] = {}
    for rel, alt in RAIL_LINKS:
        if rel in params:
            out[rel] = float(params[rel] or 0)
            out[alt] = float(params.get(alt, 0) or 0)
    return out


def link_rails(params: dict[str, Any], ref: dict[str, float]) -> dict[str, Any]:
    """Set ALT from REL: alt = alt0 + (rel - rel0). Mutates and returns params."""
    for rel, alt in RAIL_LINKS:
        if rel in params and rel in ref:
            v = ref[alt] + (float(params[rel]) - ref[rel])
            params[alt] = int(v) if float(v).is_integer() else round(v, 3)
    return params


def companions_from_status(status: dict[str, Any]) -> dict[str, float]:
    """Current VMIN/ALT offsets in mV, so a REL sweep keeps them as they are."""
    out: dict[str, float] = {}
    for rail, prefix in (("nvvdd_offsets_uv", ""), ("msvdd_offsets_uv", "msvdd_")):
        for part in ("vmin", "alt"):
            v = _mv_from_uv(_get(status, rail, part))
            if v is not None:
                out[f"{prefix}{part}"] = v
    return out


# Which direction of each knob is *more aggressive* (more likely to crash).
# Used to skip grid points that are at least as aggressive as a known failure.
#   +1: higher is riskier     -1: lower is riskier
# Knobs not listed must match exactly for a failure to rule a point out.
AGGRESSION: dict[str, int] = {
    "core": +1, "mem": +1, "xbar_offset": +1, "sys_offset": +1, "video_offset": +1,
    "boost": +1, "clock_max": +1,
    # MORE voltage is riskier at a given core offset, not less. The offset
    # shifts the whole V/F curve; a lower voltage cap keeps the card off the top
    # points, which are the least stable with an offset added. Measured on an
    # RTX 5080 (64 runs): highest clean core +470 at REL -90..-160 mV, +395 at
    # -30..-90, +365 at -30..+30, +335 at +30..+100.
    "rel": +1, "msvdd_rel": +1,
    # A bigger power budget lets the card run higher clocks and voltage under
    # load, so it is never safer. Without a direction here, a crash could only
    # rule out configs with the exact same budget - useless once the budget
    # itself is explored.
    "cap": +1, "power": +1,
    # Voltage demand: a NEGATIVE offset asks for less voltage at every clock,
    # so deeper is riskier (classic undervolt; can fail at any clock).
    "core_vdemand": -1, "xbar_vdemand": -1, "sys_vdemand": -1, "video_vdemand": -1,
}


def at_least_as_aggressive(candidate: dict[str, Any], failed: dict[str, Any]) -> bool:
    """True when `candidate` is no safer than `failed` on every knob.

    Missing keys count as 0 (stock). A candidate that is safer on any knob, or
    differs on a knob with no known direction, is not ruled out.
    """
    keys = (set(candidate) | set(failed)) - {"vmin", "alt", "msvdd_vmin", "msvdd_alt"}
    for key in keys:
        c, f = candidate.get(key, 0), failed.get(key, 0)
        direction = AGGRESSION.get(key)
        try:
            c, f = float(c), float(f)
        except (TypeError, ValueError):
            if c != f:
                return False
            continue
        if direction is None:
            if c != f:
                return False
        elif (c - f) * direction < 0:
            return False
    return True


def validate_params(params: dict[str, Any], status: dict[str, Any] | None = None
                    ) -> list[str]:
    """Problems with a params dict before it goes anywhere near the GPU or a profile.

    Checks well-formedness only: known controls, finite numbers, the range the
    driver itself reports (when `status` is given), and power/cap exclusivity.
    The tuner deliberately imposes no ceilings of its own on offsets.
    """
    problems: list[str] = []
    allowed = set(KNOBS) | {c for cs in COMPANIONS.values() for c in cs}
    for key, val in params.items():
        if key not in allowed:
            problems.append(f"unknown control '{key}'")
            continue
        if isinstance(val, bool):
            continue
        try:
            fv = float(val)
        except (TypeError, ValueError):
            problems.append(f"{key}: not a number ({val!r})")
            continue
        if fv != fv or fv in (float("inf"), float("-inf")):
            problems.append(f"{key}: not a finite number")
            continue
        if status is not None and key in KNOBS and KNOBS[key].bounds is not None:
            b = effective_bounds(KNOBS[key].bounds(status))
            if b and not (b[0] <= fv <= b[1]):
                problems.append(f"{key}={val} outside the driver range {b[0]}..{b[1]}")
    if "power" in params and "cap" in params:
        problems.append("'power' and 'cap' are mutually exclusive")
    return problems


def snapshot(status: dict[str, Any], keys: list[str] | None = None) -> dict[str, Any]:
    """Current values of readable knobs, in params form, for restoring later."""
    out: dict[str, Any] = {}
    for key, k in KNOBS.items():
        if k.readback is None or (keys is not None and key not in keys):
            continue
        try:
            v = k.readback(status)
        except Exception:
            v = None
        if v is not None:
            out[key] = v
    if "rel" in out or "msvdd_rel" in out:
        out.update({c: v for c, v in companions_from_status(status).items()
                    if (c.startswith("msvdd_") and "msvdd_rel" in out)
                    or (not c.startswith("msvdd_") and "rel" in out)})
    return out


# mVolt+ voltage editing: standard mode tops out at 1.15 V (or a lower device
# maximum); XOC mode (`--xoc`) allows up to each rail's reported device maximum.
STANDARD_VOLT_CEILING_MV = 1150.0
# With mVolt+'s "Black-screen fix" on (the default on RTX 50), the driver itself
# lowers MSVDD REL by 50 mV. A reading of exactly -50 is therefore stock.
MSVDD_DRIVER_REL_MV = -50.0


def rail_info(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Voltage limit per rail: current, stock (offsets removed), and how high it may go."""
    out: dict[str, dict[str, Any]] = {}
    for rail, label in (("nvvdd", "core"), ("msvdd", "fabric")):
        cur = status.get(f"{rail}_max_mv")
        rel = _mv_from_uv(_get(status, f"{rail}_offsets_uv", "rel")) or 0.0
        if rail == "msvdd" and rel == MSVDD_DRIVER_REL_MV:
            rel = 0.0   # the driver's own -50 mV (Black-screen fix) is part of stock
        dev = _get(status, f"{rail}_device_range_uv", "maximum")
        dev_mv = float(dev) / 1000.0 if dev else None
        standard = min(STANDARD_VOLT_CEILING_MV, dev_mv) if dev_mv else STANDARD_VOLT_CEILING_MV
        out[label] = {
            "rail": rail.upper(),
            "current_mv": cur,
            # The effective limit moves with the REL offset; strip it for the stock value.
            "stock_mv": round(float(cur) - rel, 1) if cur is not None else None,
            "stock_exact": rel == 0,
            "standard_max_mv": standard,
            "xoc_max_mv": dev_mv,
            "min_mv": status.get(f"{rail}_min_mv"),
        }
    return out


def power_info(status: dict[str, Any]) -> dict[str, Any]:
    pl = status.get("power_limits") or {}
    pc = status.get("power_client") or {}
    default = float(pl["default_mw"]) / 1000.0 if pl.get("default_mw") else None
    lo_pct = status.get("power_limit_percent_limit_min")
    hi_pct = status.get("power_limit_percent_limit_max")
    maximum = float(pc["maximum_mw"]) / 1000.0 if pc.get("maximum_mw") else (
        default * float(hi_pct) / 100.0 if default and hi_pct else None)
    return {
        "default_w": default,
        "current_w": float(pl["enforced_mw"]) / 1000.0 if pl.get("enforced_mw") else None,
        "min_w": round(default * float(lo_pct) / 100.0, 1) if default and lo_pct else None,
        "max_w": maximum,
        "percent_range": [lo_pct, hi_pct],
    }


def card_defaults(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Stock value per control, from what mVolt+ reports. `None` = not reported."""
    pw = power_info(status)
    ocp = status.get("power_channel_a") or {}
    cr = status.get("clock_range_mhz") or {}
    zero = {"default": 0, "source": "stock offset"}
    out: dict[str, dict[str, Any]] = {
        k: dict(zero) for k in ("core", "mem", "xbar_offset", "sys_offset", "video_offset",
                                "rel", "core_vdemand", "xbar_vdemand", "sys_vdemand",
                                "video_vdemand", "boost")}
    ms = _mv_from_uv(_get(status, "msvdd_offsets_uv", "rel"))
    out["msvdd_rel"] = ({"default": MSVDD_DRIVER_REL_MV,
                         "source": "driver default with mVolt+ 'Black-screen fix' on"}
                        if ms == MSVDD_DRIVER_REL_MV else
                        {"default": 0, "source": "stock offset (-50 if 'Black-screen fix' is on)"})
    out["power"] = {"default": 100, "source": f"= {pw['default_w']:g} W" if pw["default_w"] else ""}
    out["cap"] = {"default": pw["default_w"], "source": "driver default board power"}
    out["boost_lock"] = {"default": False, "source": "stock"}
    out["clock_max"] = {"default": cr.get("limit_max"), "source": "no clock lock"}
    for key, field in (("nvvdd_ocp", "NVVDD OCP"), ("msvdd_ocp", "MSVDD OCP")):
        out[key] = {"default": (ocp.get(field) or {}).get("default"), "source": "firmware default"}
    out["msvdd_clock_ratio"] = {"default": None, "source": "not reported by mVolt+"}
    return out


def _as_bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("on", "true", "1", "yes", "enabled")
    return bool(v)


def verify(params: dict[str, Any], status: dict[str, Any]) -> list[str]:
    """Return a list of human-readable mismatches (empty means good)."""
    problems: list[str] = []
    for key, k in KNOBS.items():
        if key not in params or k.readback is None:
            continue
        try:
            got = k.readback(status)
        except Exception:
            got = None
        if got is None:
            problems.append(f"{key}: no readback")
            continue
        want = params[key]
        if isinstance(want, bool) or isinstance(got, bool) or key == "boost_lock":
            ok = _as_bool(got) == _as_bool(want)
        else:
            try:
                ok = abs(float(got) - float(want)) < k.tolerance
            except (TypeError, ValueError):
                ok = got == want
        if not ok:
            problems.append(f"{key}: reported {got}{k.unit} != requested {want}{k.unit}")
    # VMIN/ALT companions are written with REL; check them too.
    current = companions_from_status(status)
    for comp in ("vmin", "alt", "msvdd_vmin", "msvdd_alt"):
        if comp in params and comp in current:
            if abs(float(current[comp]) - float(params[comp])) >= 0.51:
                problems.append(f"{comp}: reported {current[comp]}mV != requested {params[comp]}mV")
    return problems


def describe(status: dict[str, Any],
             writable: dict[str, bool | None] | None = None) -> str:
    """Human-readable capability table, used by `python -m tuner --survey`."""
    rows = survey(status, writable)
    width = max(len(r.key) for r in rows) + 2
    lines = [f"{'knob'.ljust(width)}{'state'.ljust(10)}{'unit'.ljust(6)}"
             f"{'current'.ljust(12)}{'range'.ljust(18)}note"]
    for r in rows:
        state = "OK" if r.available else "off"
        rng = f"{r.bounds[0]}..{r.bounds[1]}" if r.bounds else "-"
        cur = "-" if r.current is None else str(r.current)
        lines.append(f"{r.key.ljust(width)}{state.ljust(10)}{r.unit.ljust(6)}"
                     f"{cur.ljust(12)}{rng.ljust(18)}{r.reason if not r.available else r.note}")
    return "\n".join(lines)
