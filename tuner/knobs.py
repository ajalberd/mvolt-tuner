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
    note="global core clock offset; coexists with regional V/F edits",
))

_register(Knob(
    key="mem", build=_build_mem, unit="MHz", group="clocks",
    readback=lambda s: s.get("memory_offset_mhz"),
    bounds=lambda s: (s.get("memory_offset_mhz_limit_min"), s.get("memory_offset_mhz_limit_max")),
    note="memory clock offset",
))

for _k, _flag in (("xbar_offset", "--xbar-offset"),
                  ("sys_offset", "--sys-offset"),
                  ("video_offset", "--video-offset")):
    _register(Knob(
        key=_k, build=_build_domain_offset(_flag), unit="MHz", group="fabric",
        readback=(lambda k: (lambda s: s.get(k)))(_k),
        bounds=(lambda k: (lambda s: (s.get(f"{k}_limit_min"), s.get(f"{k}_limit_max"))))(_k),
        note="fabric/video domain clock offset (absent on RTX 20 mobile)",
    ))

_register(Knob(
    key="rel", build=_build_nvvdd, unit="mV", group="voltage",
    readback=lambda s: _mv_from_uv(_get(s, "nvvdd_offsets_uv", "rel")),
    note="NVVDD REL voltage-limit offset; needs vmin/alt companions in params",
    requires=("vmin", "alt"),
))

_register(Knob(
    key="msvdd_rel", build=_build_msvdd, unit="mV", group="voltage",
    readback=lambda s: _mv_from_uv(_get(s, "msvdd_offsets_uv", "rel")),
    note="MSVDD fabric-rail REL offset (not reported on this card)",
    requires=("msvdd_vmin", "msvdd_alt"),
))

for _k, _flag, _dep in (("core_vdemand", "--core-vdemand", "core"),
                        ("xbar_vdemand", "--xbar-vdemand", "xbar_offset"),
                        ("sys_vdemand", "--sys-vdemand", "sys_offset"),
                        ("video_vdemand", "--video-vdemand", "video_offset")):
    _register(Knob(
        key=_k, build=_build_domain_vdemand(_flag), unit="mV", group="voltage",
        readback=None, write_only=True, depends_on=_dep,
        note="voltage-demand offset; NOT reported by --status, so unverifiable",
    ))

_register(Knob(
    key="power", build=_build_power_pct, unit="%", group="power",
    readback=lambda s: s.get("power_limit_percent"),
    note="power limit as percent of default; mutually exclusive with cap",
))

_register(Knob(
    key="cap", build=_build_power_cap, unit="W", group="power",
    readback=lambda s: _get(s, "power_limits", "enforced_mw", default=0) and
                       round(_get(s, "power_limits", "enforced_mw") / 1000.0, 3),
    note="absolute watt cap; mutually exclusive with power percent",
))

_register(Knob(
    key="boost", build=_build_boost, unit="%", group="voltage",
    readback=lambda s: s.get("voltage_boost_percent"),
    note="Voltage Boost: extra headroom from REL toward ALT/OP",
))

_register(Knob(
    key="boost_lock", build=_build_boost_lock, unit="", group="clocks",
    readback=lambda s: s.get("boost_lock"),
    note="pins boost behaviour on/off; immediate action",
))

_register(Knob(
    key="clock_max", build=_build_clock_range, unit="MHz", group="clocks",
    readback=lambda s: _get(s, "clock_range_mhz", "max"),
    bounds=lambda s: (_get(s, "clock_range_mhz", "limit_min"),
                      _get(s, "clock_range_mhz", "limit_max")),
    note="upper bound of the requested core clock range",
))

for _k, _flag, _dep in (("nvvdd_ocp", "--nvvdd-ocp", "rel"),
                        ("msvdd_ocp", "--msvdd-ocp", "msvdd_rel")):
    _register(Knob(
        key=_k, build=_build_ocp(_flag), unit="A", group="power",
        readback=None, write_only=True, depends_on=_dep,
        note="rail output current limit; settable but not reported",
    ))

_register(Knob(
    key="msvdd_clock_ratio", build=lambda p: ["--msvdd-clock-ratio", str(p["msvdd_clock_ratio"])],
    unit="ratio", group="fabric", readback=None, write_only=True, depends_on="msvdd_rel",
    note="fabric clock demand relative to core; not reported",
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
        if f"{k}_mhz" in status:
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
    return present


def available_keys(status: dict[str, Any],
                   writable: dict[str, bool | None] | None = None) -> list[str]:
    return [k.key for k in survey(status, writable) if k.available]


def build_args(params: dict[str, Any]) -> list[str]:
    """Translate a params dict into mVolt+ CLI args, in registry order."""
    args: list[str] = []
    for key, k in KNOBS.items():
        if key in params:
            args += k.build(params)
    return args


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
        if isinstance(want, bool) or isinstance(got, bool):
            ok = bool(got) == bool(want)
        else:
            try:
                ok = abs(float(got) - float(want)) < 0.51
            except (TypeError, ValueError):
                ok = got == want
        if not ok:
            problems.append(f"{key}: reported {got}{k.unit} != requested {want}{k.unit}")
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
