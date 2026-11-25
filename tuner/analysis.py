"""What the measurements say, in units you can act on. No Optuna needed.

  effects()        "core +100 MHz = +X points, REL -10 mV = -Y W" - a linear
                   model fitted to the clean runs, with standard errors, so a
                   small effect inside the noise is reported as such.
  crash_model()    where the stability edge lies: a logistic model of crash vs
                   the settings. The ratio of two coefficients is the trade-off,
                   e.g. "each 10 mV of undervolt costs ~20 MHz of stable core".
  stability_edge() per setting of the other knobs: the highest core that passed
                   and the lowest that crashed.

Why not importance shares (fANOVA): a share of variance depends on how wide each
knob was explored and, scored on clean runs only, is biased by which runs
crashed. Effects per unit answer "how much" directly.
"""
from __future__ import annotations

import json
import math
from typing import Any

UNSTABLE = ("crash", "driver_reset", "interrupted")
MEASURED = ("ok", "over_power_limit")
COMPANIONS = {"vmin", "alt", "msvdd_vmin", "msvdd_alt"}

# Effects are reported per this much of each knob.
UNITS: dict[str, tuple[float, str]] = {
    "core": (100, "MHz"), "mem": (500, "MHz"), "xbar_offset": (100, "MHz"),
    "sys_offset": (100, "MHz"), "video_offset": (100, "MHz"), "clock_max": (100, "MHz"),
    "rel": (10, "mV"), "msvdd_rel": (10, "mV"), "core_vdemand": (10, "mV"),
    "xbar_vdemand": (10, "mV"), "sys_vdemand": (10, "mV"), "video_vdemand": (10, "mV"),
    "cap": (10, "W"), "power": (5, "%"), "boost": (10, "%"),
    "nvvdd_ocp": (10, "A"), "msvdd_ocp": (10, "A"), "msvdd_clock_ratio": (0.05, ""),
}


# ---------------------------------------------------------------------------
# tiny linear algebra (dims are < 20; no numpy needed)
# ---------------------------------------------------------------------------

def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    n = len(a)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[piv][c]) < 1e-12:
            return None
        m[c], m[piv] = m[piv], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def _inverse_diag(a: list[list[float]]) -> list[float] | None:
    n = len(a)
    out = []
    for i in range(n):
        e = [1.0 if j == i else 0.0 for j in range(n)]
        col = _solve(a, e)
        if col is None:
            return None
        out.append(col[i])
    return out


def _value(p: dict[str, Any], key: str, fill: dict[str, float]) -> float:
    v = p.get(key, fill.get(key, 0.0))
    return float(v) if not isinstance(v, bool) else float(v)


def _design(trials: list[dict[str, Any]], only: set[str] | None):
    keys: set[str] = set()
    for t in trials:
        keys |= {k for k in (t.get("params") or {}) if k not in COMPANIONS and k in UNITS}
    if only:
        keys &= set(only)
    caps = [float(t["params"]["cap"]) for t in trials if "cap" in (t.get("params") or {})]
    fill = {"cap": max(caps)} if caps else {}
    if "power" in keys:
        fill["power"] = 100.0
    varied = sorted(k for k in keys if len({_value(t["params"], k, fill) for t in trials}) > 1)
    flat = sorted(keys - set(varied))
    return varied, flat, fill


# ---------------------------------------------------------------------------
# effects
# ---------------------------------------------------------------------------

def _ols(rows: list[tuple[list[float], float]], ridge: float = 1e-6):
    p = len(rows[0][0])
    xtx = [[sum(x[i] * x[j] for x, _ in rows) + (ridge if i == j and i > 0 else 0.0)
            for j in range(p)] for i in range(p)]
    xty = [sum(x[i] * y for x, y in rows) for i in range(p)]
    beta = _solve(xtx, xty)
    if beta is None:
        return None
    resid = [y - sum(b * xi for b, xi in zip(beta, x)) for x, y in rows]
    n = len(rows)
    dof = max(1, n - p)
    s2 = sum(r * r for r in resid) / dof
    ybar = sum(y for _, y in rows) / n
    sst = sum((y - ybar) ** 2 for _, y in rows) or 1e-12
    diag = _inverse_diag(xtx)
    se = [math.sqrt(max(0.0, s2 * d)) for d in diag] if diag else [None] * p
    return {"beta": beta, "se": se, "sd": math.sqrt(s2), "r2": 1 - sum(r * r for r in resid) / sst,
            "dof": dof}


def effects(trials: list[dict[str, Any]], only: set[str] | None = None) -> dict[str, Any]:
    """Per knob: change in score and watts per unit (see UNITS), with SE."""
    clean = [t for t in trials if t.get("status") in MEASURED and t.get("score")
             and t.get("watts") is not None]
    varied, flat, fill = _design(clean, only)
    out: dict[str, Any] = {"n": len(clean), "knobs": [], "not_varied": flat, "fit": {}}
    if not varied:
        out["note"] = "no knob was varied across clean runs yet"
        return out
    if len(clean) < len(varied) + 3:
        out["note"] = (f"{len(clean)} clean runs for {len(varied)} knobs - need at least "
                       f"{len(varied) + 3} for an estimate")
        return out
    means = {k: sum(_value(t["params"], k, fill) for t in clean) / len(clean) for k in varied}
    lo = {k: min(_value(t["params"], k, fill) for t in clean) for k in varied}
    hi = {k: max(_value(t["params"], k, fill) for t in clean) for k in varied}

    def x(t):
        return [1.0] + [(_value(t["params"], k, fill) - means[k]) / UNITS[k][0] for k in varied]

    fits = {}
    for target in ("score", "watts"):
        f = _ols([(x(t), float(t[target])) for t in clean])
        if f:
            fits[target] = f
            out["fit"][target] = {"r2": round(f["r2"], 3), "noise_sd": round(f["sd"], 2),
                                  "n": len(clean)}
    for i, k in enumerate(varied, start=1):
        unit, uname = UNITS[k]
        row: dict[str, Any] = {"knob": k, "per": unit, "unit": uname,
                               "range": [lo[k], hi[k]]}
        for target, f in fits.items():
            b, se = f["beta"][i], f["se"][i]
            span = (hi[k] - lo[k]) / unit
            row[target] = {
                "per_unit": round(b, 2), "se": round(se, 2) if se is not None else None,
                "across_range": round(b * span, 1),
                # |t| < 2: indistinguishable from noise at this sample size
                "clear": bool(se and abs(b) > 2 * se),
            }
        out["knobs"].append(row)
    out["knobs"].sort(key=lambda r: abs((r.get("score") or {}).get("across_range") or 0),
                      reverse=True)
    return out


# ---------------------------------------------------------------------------
# crash model
# ---------------------------------------------------------------------------

def _logistic(rows: list[tuple[list[float], float]], l2: float = 0.5, iters: int = 60):
    """Newton/IRLS with an L2 penalty (keeps it finite when the data separate
    perfectly, which clean stability data often does)."""
    p = len(rows[0][0])
    beta = [0.0] * p
    for _ in range(iters):
        grad = [0.0] * p
        hess = [[0.0] * p for _ in range(p)]
        for x, y in rows:
            z = sum(b * xi for b, xi in zip(beta, x))
            mu = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
            w = mu * (1 - mu)
            for i in range(p):
                grad[i] += (y - mu) * x[i]
                for j in range(p):
                    hess[i][j] += w * x[i] * x[j]
        for i in range(1, p):
            grad[i] -= l2 * beta[i]
            hess[i][i] += l2
        step = _solve(hess, grad)
        if step is None:
            break
        beta = [b + s for b, s in zip(beta, step)]
        if max(abs(s) for s in step) < 1e-6:
            break
    diag = _inverse_diag(hess) or [float("inf")] * p
    return beta, [math.sqrt(max(d, 0.0)) for d in diag]


def crash_model(trials: list[dict[str, Any]], only: set[str] | None = None) -> dict[str, Any]:
    """Crash risk per knob, and the core-vs-undervolt trade-off when both vary."""
    done = [t for t in trials if t.get("status") in MEASURED + UNSTABLE]
    crashes = sum(t["status"] in UNSTABLE for t in done)
    out: dict[str, Any] = {"n": len(done), "crashes": crashes, "knobs": []}
    if crashes == 0 or crashes == len(done):
        out["note"] = ("no crashes yet - the stability edge is beyond what was tried"
                       if crashes == 0 else "everything crashed")
        return out
    varied, _flat, fill = _design(done, only)
    if not varied:
        return out
    sd = {}
    mean = {}
    for k in varied:
        vals = [_value(t["params"], k, fill) for t in done]
        mean[k] = sum(vals) / len(vals)
        sd[k] = (sum((v - mean[k]) ** 2 for v in vals) / len(vals)) ** 0.5 or 1.0
    rows = [([1.0] + [(_value(t["params"], k, fill) - mean[k]) / sd[k] for k in varied],
             1.0 if t["status"] in UNSTABLE else 0.0) for t in done]
    beta, se = _logistic(rows)
    clear: dict[str, bool] = {}
    for i, k in enumerate(varied, start=1):
        per_unit = beta[i] / sd[k] * UNITS[k][0]
        # Same rule as the score effects: under 2 standard errors is noise.
        clear[k] = abs(beta[i]) > 2 * se[i]
        out["knobs"].append({"knob": k, "per": UNITS[k][0], "unit": UNITS[k][1],
                             "log_odds": round(per_unit, 3), "clear": clear[k],
                             "effect": ("no clear effect" if not clear[k] else
                                        "raises crash risk" if per_unit > 0 else
                                        "lowers crash risk")})
    out["knobs"].sort(key=lambda r: (r["clear"], abs(r["log_odds"])), reverse=True)
    if "core" in varied and "rel" in varied and clear["core"] and clear["rel"]:
        bc = beta[varied.index("core") + 1] / sd["core"]
        br = beta[varied.index("rel") + 1] / sd["rel"]
        if bc > 0:
            # Boundary bc*core + br*rel = const  ->  dcore/drel = -br/bc.
            # Per 10 mV of UNDERVOLT (rel -10) the stable core moves by +10*br/bc.
            gain = 10 * br / bc
            out["core_per_10mv_undervolt"] = round(gain, 1)
            out["tradeoff"] = (f"roughly: each 10 mV of undervolt "
                               f"{'gains' if gain > 0 else 'costs'} ~{abs(gain):.0f} MHz of "
                               f"stable core (the edge search measures it exactly)")
    return out


def edge_model(trials: list[dict[str, Any]]):
    """P(crash) as a function of core, REL and power budget, fitted to every
    finished run. Other settings are held at their average, so this is a prior
    for the edge search, not a verdict. Returns None without enough data."""
    keys = ["core", "rel", "cap"]
    # Runs with a voltage-demand offset fail for a different reason (too little
    # voltage at every clock); they would bias the core edge, so they stay out.
    done = [t for t in trials if t.get("status") in MEASURED + UNSTABLE
            and "core" in (t.get("params") or {})
            and not any(float((t.get("params") or {}).get(k) or 0)
                        for k in ("core_vdemand", "xbar_vdemand", "sys_vdemand",
                                  "video_vdemand"))]
    crashes = sum(t["status"] in UNSTABLE for t in done)
    if len(done) < 10 or crashes < 3 or crashes == len(done):
        return None
    caps = [float(t["params"]["cap"]) for t in done if "cap" in t["params"]]
    fill = {"cap": max(caps) if caps else 0.0}
    keys = [k for k in keys if len({_value(t["params"], k, fill) for t in done}) > 1]
    if "core" not in keys:
        return None
    mean = {k: sum(_value(t["params"], k, fill) for t in done) / len(done) for k in keys}
    sd = {k: ((sum((_value(t["params"], k, fill) - mean[k]) ** 2 for t in done) / len(done))
              ** 0.5) or 1.0 for k in keys}
    rows = [([1.0] + [(_value(t["params"], k, fill) - mean[k]) / sd[k] for k in keys],
             1.0 if t["status"] in UNSTABLE else 0.0) for t in done]
    beta, _se = _logistic(rows, l2=0.3)
    raw = {k: beta[i + 1] / sd[k] for i, k in enumerate(keys)}
    if raw["core"] <= 0:
        return None
    b0 = beta[0] - sum(raw[k] * mean[k] for k in keys)

    def p_crash(core: float, rel: float = 0.0, cap: float | None = None) -> float:
        z = b0 + raw["core"] * core
        if "rel" in raw:
            z += raw["rel"] * rel
        if "cap" in raw:
            z += raw["cap"] * (cap if cap is not None else fill["cap"])
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))

    def core_at(p: float, rel: float = 0.0, cap: float | None = None) -> float:
        """Core offset at which P(crash) = p for this REL and budget."""
        z = math.log(p / (1 - p)) - b0
        if "rel" in raw:
            z -= raw["rel"] * rel
        if "cap" in raw:
            z -= raw["cap"] * (cap if cap is not None else fill["cap"])
        return z / raw["core"]

    return {"p_crash": p_crash, "core_at": core_at, "n": len(done), "crashes": crashes,
            "coef": {k: round(v, 5) for k, v in raw.items()}}


# ---------------------------------------------------------------------------
# stability edge (moved here from optuna_report; no Optuna needed)
# ---------------------------------------------------------------------------

def stability_edge(trials: list[dict[str, Any]], axis: str = "core") -> list[dict[str, Any]]:
    """For every combination of the OTHER knobs: highest `axis` value that ran
    clean and lowest that crashed. The true edge lies between them."""
    groups: dict[str, dict[str, Any]] = {}
    for t in trials:
        p = t.get("params") or {}
        if axis not in p or t.get("status") not in MEASURED + UNSTABLE:
            continue
        rest = {k: v for k, v in p.items() if k != axis}
        key = json.dumps(rest, sort_keys=True)
        g = groups.setdefault(key, {"setting": rest, "highest_stable": None,
                                    "lowest_failed": None, "trials": 0})
        g["trials"] += 1
        v = float(p[axis])
        if t["status"] in UNSTABLE:
            if g["lowest_failed"] is None or v < g["lowest_failed"]:
                g["lowest_failed"] = v
        elif g["highest_stable"] is None or v > g["highest_stable"]:
            g["highest_stable"] = v
    out = []
    for g in groups.values():
        hs, lf = g["highest_stable"], g["lowest_failed"]
        if hs is not None and lf is not None:
            g["edge"] = f"between {hs:g} and {lf:g}" if hs < lf else \
                f"inconsistent: {hs:g} passed but {lf:g} failed (noise or thermal drift)"
        elif lf is None:
            g["edge"] = f"at least {hs:g} (nothing failed yet)"
        else:
            g["edge"] = f"below {lf:g} (nothing passed yet)"
        out.append(g)
    return sorted(out, key=lambda g: json.dumps(g["setting"], sort_keys=True))


def summary(trials: list[dict[str, Any]], only: set[str] | None = None) -> dict[str, Any]:
    return {"effects": effects(trials, only), "crash": crash_model(trials, only),
            "stability_edge": stability_edge(trials)}


def render(s: dict[str, Any]) -> str:
    """Plain-text version for the CLI."""
    ef, cr = s["effects"], s["crash"]
    lines = [f"EFFECTS  ({ef['n']} clean runs; '~' = within the noise)"]
    if ef.get("note"):
        lines.append(f"  {ef['note']}")
    for k in ef.get("knobs") or []:
        def part(name: str, unit: str) -> str:
            e = k.get(name)
            if not e:
                return ""
            mark = "" if e["clear"] else "~"
            return f"{mark}{e['per_unit']:+g} {unit} (+-{e['se']:g})"
        lines.append(f"  {k['knob']:<18} per +{k['per']:g} {k['unit']:<4} "
                     f"score {part('score', 'pts'):<24} watts {part('watts', 'W')}")
    fit = (ef.get("fit") or {}).get("score")
    if fit:
        lines.append(f"  fit: R2 {fit['r2']}, run-to-run noise +-{fit['noise_sd']} pts")
    lines.append("")
    lines.append(f"STABILITY  ({cr['crashes']} of {cr['n']} runs crashed)")
    if cr.get("tradeoff"):
        lines.append(f"  {cr['tradeoff']}")
    for k in cr.get("knobs") or []:
        lines.append(f"  {k['knob']:<18} {k['effect']}")
    if cr.get("note"):
        lines.append(f"  {cr['note']}")
    edges = [e for e in s.get("stability_edge") or [] if e.get("lowest_failed") is not None]
    if edges:
        lines.append("")
        lines.append("STABILITY EDGE - core per setting of the other knobs")
        for e in edges[:15]:
            st = ", ".join(f"{k}={v}" for k, v in e["setting"].items()
                           if k not in COMPANIONS or v) or "stock"
            lines.append(f"  {st:<48} core {e['edge']}")
    return "\n".join(lines)
