# Findings — reference machine (RTX 2060 mobile)

Everything here was measured on one laptop. Treat it as **evidence, not
universal truth** — the tuner re-discovers capabilities per GPU at runtime.

```
GPU      NVIDIA GeForce RTX 2060 (Mobile / Turing), 6 GB
VBIOS    90.06.39.00.4b
Driver   610.88
Power    80 W default & enforced, 85 W max, 1 W min
mVolt+   v0.50.12
```

## Result

**Core offset +150 MHz is the optimum. Everything else is inert or harmful.**

| config | n | mean score | sd |
|---|---|---|---|
| stock | 8 | 5962.6 | 31.1 |
| **core +150, mem 0, REL 0** | 7 | **6150.9** | 17.2 |

**+3.16 %**, and the tuned config is more consistent than stock.

### Core offset sweep (one contiguous run)

| core MHz | score | Δ |
|---|---|---|
| 0 | 5948 | — |
| 50 | 6071 | +2.1 % |
| 100 | 6127 | +3.0 % |
| **150** | **6131** | **+3.1 %** |
| 200 | 6110 | +2.7 % |
| 250 | — | **hung the machine during apply** |

### Why voltage does nothing here

The card is **power-limited, not voltage-limited**. It sits at ~77 W against an
80 W cap, so the NVVDD ceiling never binds:

| REL offset | score | avg W |
|---|---|---|
| 0 mV | 6174 | 76.8 |
| −25 mV | 6190 | 77.2 |
| −50 mV | 6096 | 76.8 |

Power does not move, so a *limit* change cannot help. A real undervolt needs a
**V/F curve shift** (GUI/profile-only), which was never tested. That remains the
main open question.

## Traps

Each of these cost a reboot or a wrong conclusion.

1. **Tuning silently fails without an elevated token.** The error goes to
   **stderr** as JSON. Always parse `{"success":...}`.
2. **`--core 1000` wedges the driver and the OS.** `SetPstates20 failed …
   Rollback warning` — the rollback itself fails. Hard hang, no TDR, needs a
   power cycle.
3. **`--core 250` ` and `+300` have also hung** — sometimes during *apply*,
   before the benchmark starts. Intermittent, which is worse than a cliff.
4. **`--clock-range` takes a persistent lease.** If the holder dies, every later
   apply fails with `Could not open the settings application safety marker`.
   Release with `--clock-range default`. mVolt+ logs "adopted an orphaned
   clock-range lock" after an unclean shutdown, and refuses releases for ~90 s
   after boot.
5. **Power controls do not exist on this GPU.** `--power` → "Power limit is
   unavailable"; `--power-cap-watts` → "Power cap is unavailable or malformed".
   `power_limits` in `--status` reports the *driver's* limit, not a writable one.
6. **`--core-vdemand` is rejected**: "unavailable for this profile".
7. **`--xbar/--sys/--video-offset` and thermal inputs are absent**
   (`limit_min == limit_max == 0`, `"available": false`).
8. **Kombustor writes `_scores.csv` into the process working directory**, not the
   install directory — which is why it looked absent for most of this project.
   Schema:
   `test_name,renderer,vendor_id,device_id,api,api_version,width,height,fullscreen,duration_ms,score,avg_fps`.
   It *appends* one row per run. The per-run source of truth is still
   `_kombustor_log.txt` (`score => N points`), because that file is recreated on
   every launch — harvest it before starting another run or the score is lost.
   Launching Kombustor with a controlled CWD makes the CSV a clean second source
   and is the recommended way to read scores without a regex.
9. **`-benchmark -fullscreen` self-terminates after ~62 s.** `-nogui` runs
   forever and must be killed.
10. **The GPU-data CSV only appears with `-nogui`**, never with `-benchmark`.
    In it `gpu_power` is `-1.0` and `core_clock` is unreliable. Use
    `nvidia-smi` for power and clocks.
11. **`nvidia-smi power.limit` returns `N/A`** on this card; `power.draw` works.
12. **A software watchdog cannot recover a hung kernel.** The watchdog fired
    correctly (`STALE heartbeat: 170.1s → REBOOTING`), but `shutdown /r` needs a
    working kernel, so the machine stayed black until a manual power cycle.
    Unattended recovery needs an external power-cut device.
13. **Windows PowerShell 5.1 specifics** (for the optional `scripts/`):
    no `Wait-Process -Timeout`; non-ASCII in a UTF-8-without-BOM `.ps1` parses as
    ANSI; `$i:` inside a double-quoted string is a parse error (`${i}:`).

14. **mVolt+ silently discards profile control keys it does not recognise.**
    Writing a profile with `"core_clock": {"offset_khz": 150000}` produced a
    valid-looking profile whose `controls` object was `{}` — no error, no warning,
    and the profile applied *nothing*. The real keys are `offset_mhz`:

    ```json
    "controls": {
      "nvvdd":  {"enabled": false, "restore_default": false,
                 "vmin_offset_uv": 0, "rel_offset_uv": 0, "alt_offset_uv": 0},
      "core":   {"enabled": true, "offset_mhz": 150},
      "memory": {"enabled": false, "offset_mhz": 0},
      "xbar": {"enabled": false, "offset_mhz": 0},
      "board_input_current": {"enabled": false, "milliamps": 9000},
      "voltage_boost": {"enabled": false, "percent": 0},
      "sys_clock": {"enabled": false, "offset_mhz": 0},
      "video_clock": {"enabled": false, "offset_mhz": 0},
      "nvvdd_ocp": {"enabled": false, "limit_a": 81},
      "clock_range": {"enabled": false, "locked": false,
                      "min_mhz": 0, "max_mhz": 2100}
    }
    ```

    **Always assert `controls` is non-empty after writing.** Profile ids are
    GUIDs when the GUI creates them; an arbitrary string id is accepted.
    `mobile_rel_only: true` on mobile parts. mVolt+ rewrites the whole document
    from memory, so it must be **closed** while editing, and the edit must bump
    `revision`.

## Open questions

1. Does a **V/F curve shift** (profile-only) beat the +3 % from core offset?
   This is the one untested lever that could still pay on a power-limited card.
2. Is `glfurrymsi` at 1080p partly CPU-bound? FPS sat at 99–103 across every
   config, which would compress all measurements. A heavier test would settle it.
3. Does `--core-vdemand` become usable when `core_voltage_demand_enabled` is
   true in the profile JSON?
4. How stable is +150 across seasons/thermal states? One machine, one week.
