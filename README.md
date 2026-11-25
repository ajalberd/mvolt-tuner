# mvolt-tuner

Automatic GPU tuning on top of **[mVolt+](https://github.com/b00nz/mVolt)** and
**MSI Kombustor** or **GravityMark**. It discovers which settings your GPU actually exposes, sweeps
them, records score and power per trial in a crash-safe journal, and reports what
won.

- **Nothing is hardcoded per machine.** Settings are discovered from the driver.
- **Every write is verified** by reading the value back.
- **Every trial is journalled before it runs**, so a crash can never lose data.
- **Unavailable controls are reported, not guessed at.**

## Requirements

| | |
|---|---|
| OS | Windows 10/11, x64 |
| GPU | NVIDIA, supported by mVolt+ (RTX 50 primary; RTX 20/30/40 experimental) |
| Python | 3.10+ |
| Software | [mVolt+](https://github.com/b00nz/mVolt/releases/latest) and a benchmark: [MSI Kombustor](https://www.geeks3d.com/furmark/kombustor/) or [GravityMark](https://gravitymark.tellusim.com/) |
| Access | an **elevated** terminal — mVolt+ rejects tuning writes without it |

mVolt+ is found automatically if it sits next to this folder, in `Program Files`,
or if you set `MVOLT_EXE`. Kombustor is found under `Program Files\Geeks3D`, or
set `KOMBUSTOR_EXE`. GravityMark is found at `Program Files\GravityMarkin`, or
set `GRAVITYMARK_EXE`.

### Choosing the benchmark

Pick it at the top of the Setup & Run page. Each benchmark is its **own study**:
scores are in different units, so each keeps its own journal
(`state/journal.db` for Kombustor, `state/journal-gravitymark.db` for GravityMark)
and its own explore/edge/confirm plans; switching puts one away and brings the
other back. Your limits are shared. **Hard freezes and driver resets are shared
too**: they are hardware events, so a freeze recorded under one benchmark guards
the other, and the driver-resets-per-boot stop counts both.

GravityMark runs one fullscreen Direct3D 11 benchmark pass at your desktop
resolution (`-d3d11 -benchmark 1 -close 1 -fullscreen 1 ...`, frame times via
`-times`). A pass that exits normally without a readable score (for example, the
window was closed) stops the run as a tooling error; it is never recorded as a
crash.

## Quick start (web interface)

```powershell
# Administrator PowerShell, inside the mvolt-tuner folder
python -m tuner --serve      # then open http://127.0.0.1:8712/
```

The **Setup & Run** page walks through it in order:

1. **Your limits** - max board power, max core (NVVDD) and fabric (MSVDD)
   voltage, each shown against the card's stock value and maximum. Nothing else
   unlocks until they are saved; empty = no limit, above stock = allow raising.
2. **Your GPU** - every control with its current value, default and driver range,
   plus GUI/profile-only ones (e.g. board input current). An optional button
   confirms which controls the driver accepts writes for.
3. **What to test** - **Smart search** (recommended), around an existing mVolt+
   profile or your current settings:
   - **Explore**: an even spread (Latin hypercube) over wide ranges of every
     ticked control plus the power budget - typically 30 runs. Built to measure
     what each control does; crashes are useful data.
   - **Analyse**: effect sizes in units ("core +100 MHz = +234 pts ± 6, REL
     +10 mV = +9 W"), with anything inside the run-to-run noise greyed out, and
     a crash model of which controls raise the risk.
   - **Edge search**: GPUs get faster with core offset until they become
     unstable, and that edge moves with voltage. So for each power budget ×
     undervolt level ("lane") the tuner finds the highest stable core by
     halving the gap between the highest pass and lowest crash (~5 runs per
     lane). Lanes share results: a crash with more voltage implies one with
     less, a pass with less voltage implies one with more.
   - **Confirm**: the best lane per budget is backed off a safety margin
     (default 15 MHz) and re-run several times (default 3, interleaved). Any
     crash fails it. Only confirmed configs are recommended on Results.

   Voltage is searched as the REL offset with **ALT/OP moved by the same
   amount** (keeping the starting gap, like mVolt+'s linked editing): both cap
   the rail, so raising REL alone past ALT would change nothing. To cover the
   whole score-vs-watts front, step 1's *Fill in suggested for this card* sets
   the card's full power range, core voltage +50 mV over stock and fabric at
   stock; the default explore and edge plans then span undervolt to overvolt
   and every power budget.

   Optuna is optional (a secondary importance view); the tuner doesn't need it.

   **Manual grid** is still there for stepping one or two controls yourself.
4. **Run** - Start / Stop with live progress. Results land on the **Results**
   (Pareto) and **Knob importance** pages.

mVolt+ may stay open; just don't press Apply in it, switch profiles, or let the PC
sleep during a run. Close it only to save results as profiles.

### Terminal equivalent

```powershell
python -m tuner --check      # verify install, set limits, list this GPU's settings
python -m tuner --setup      # choose which knobs to sweep
python -m tuner --list --from-config   # preview the resulting grid
python -m tuner --from-config --trials 10   # run
```

`--check` prints a capability table for *your* card and explains every exclusion:

```
core        OK    MHz   0        -1000..1000     global core clock offset
xbar_offset off   MHz   -        -               degenerate range (limit_min == limit_max)
power       off   %     0        -               driver rejects writes to this control
cap         off   W     80.0     -               driver rejects writes to this control
```

`--setup` first asks for **your limits**, then one question per available knob
(accept the default with Enter), and writes `state/config.json`. Use
`--setup --yes` for defaults without prompting (no limits).

### Limits: the only ceilings

The tuner puts **no caps on offsets** - how far a core offset can go depends on
the voltage (an undervolt lowers it), so a fixed MHz ceiling is meaningless. The
only ceilings are the three you set first (web page step 1, or `--limits`,
`--max-watts N|none`, `--max-mv N|none`, `--max-fabric-mv N|none`). Each may be
**above stock**, up to what the card allows:

| limit | how it is enforced | may go up to |
|---|---|---|
| max board power (W) | becomes the card's own watt cap on every trial (above stock it *raises* the power limit); a run averaging >5% over is stopped | the card maximum (`power_client.maximum_mw`) |
| max core voltage (mV) | after each apply, before any load, the effective NVVDD limit (`nvvdd_max_mv`) is checked; over = reverted and skipped | 1.15 V standard, the rail's device max with XOC (`--xoc` is used automatically) |
| max fabric voltage (mV) | same, on MSVDD (`msvdd_max_mv`) | same |

Over-limit configs are never recommended. Values typed in `--setup` are clamped
only to the range the driver itself reports.

### Knob importance (Optuna)

```powershell
pip install optuna scikit-learn
python -m tuner --importance
```

Replays the journal into Optuna and reports, per outcome (score, watts,
efficiency, crash risk), the share each knob explains (fANOVA; PED-ANOVA too at
20+ trials) with its direction. It also prints the **stability edge**: for each
voltage/other setting, the highest core offset that passed and the lowest that
failed. Also written to `data/importance.json` after every run and shown on
`web/importance.html`.

## What a run does

Before the first trial it records **your current settings** (and the applied
mVolt+ profile, if `--status` verifies one). That restore point is kept in the
journal, so even a run that resumes after a crash puts back what you had before
the study, not stock values. Nothing is ever capped or zeroed that the grid does
not sweep: there is no power cap unless you pass `--caps`.

For each point in the grid:

1. **journals the trial** with status `applying` (durable before touching the GPU)
2. applies the settings and **verifies them** against `--status`; a rejected or
   silently-ignored write aborts the trial rather than poisoning the data
3. runs the selected benchmark, sampling `nvidia-smi` power/clocks/temp
4. harvests the score from the benchmark's log
5. **re-reads `--status` and the Windows System log**. A driver reset clears
   applied tuning (mVolt+ reconnects without reapplying it), so if the settings
   changed or a TDR/Xid event appeared, the trial is recorded as `driver_reset`
   and its partial score is discarded
6. journals the result and updates `data/live.json`

At the end of the run (or on Ctrl+C / error) your settings are restored.

**Unstable configs are never retried automatically.** A trial that ends in
`crash`, `driver_reset`, or a host reboot mid-trial rules out every grid point at
least as aggressive (higher clocks AND same-or-deeper undervolt, everything else
equal). So a crash at +425 with −50 mV does not rule out +425 at stock voltage.
Resume is keyed on the config itself, not a grid index, so changing the grid
between runs neither skips nor repeats work. A rejected apply (`aborted`) is not
evidence of instability and is retried next run. `--retry-failed` overrides the
skip on purpose.

The driver's reported ranges apply to **every** grid source, including
`--from-config` and `--baseline-profile`.

### Recovery modes

```
--recovery auto       (default)  assume mVolt+ reverts its own settings after a
                                 crash; no external watchdog, nothing to install
--recovery hardmode              arm scripts/watchdog.ps1, which reboots the box
                                 if the heartbeat goes stale
```

`auto` is correct for most users: on most machines a bad offset produces a driver
reset (TDR), which the tuner detects and records. **`hardmode` cannot rescue a
hung kernel** — a reboot still needs a working OS. See
[docs/FINDINGS.md](docs/FINDINGS.md) trap 12. The heartbeat stops if a phase
overruns its deadline (so a hung benchmark is noticed), and the watchdog disarms
instead of rebooting when the tuner process has simply exited.

## Seeing results

```powershell
python -m tuner --survey        # capability table only
python -m tuner --check         # preflight + capability table
python tools\export_live.py     # write state/journal.db -> data/live.json
```

`data/live.json` and `data/recommendation.json` drive the web UI in `web/`.
Scores, power, clocks and failure reasons are all in `state/journal.db`.

### Web UI

```powershell
python -m tuner --serve            # http://127.0.0.1:8712/
```

The page reads real trials from `data/live.json` and shows the recommended
configuration. Each pick card has a **Save as mVolt+ profile** button that posts
to the local API — writing `tuner-best-1/2/3`, never touching live GPU state.
mVolt+ must be closed for the write; the route refuses otherwise. The server
only serves `web/` and the UI data files, rejects cross-origin requests, and
checks every value is a known control and a real number. Settings a profile
cannot hold (e.g. boost lock) are refused rather than silently dropped.

## Tests

```powershell
python -m unittest discover -s tests -v
```

## Scope and honesty

This is a tuner, not a magic overclock. On a **power-limited** laptop the only
lever may be a clock offset worth a few percent — and some offsets will hang the
machine. The tool's value is finding that limit safely, proving the result, and
refusing the settings that break things.

Measured results on the reference machine, plus 13 traps worth reading before
changing anything: **[docs/FINDINGS.md](docs/FINDINGS.md)**.

## Safety

The tuner restores your previous settings on every exit path. It imposes no
offset ceilings of its own: your watt and voltage limits are the only ones, and
crashes teach it which combinations to stop trying. Start with small ranges.

Nothing here is affiliated with NVIDIA, MSI or Geeks3D. Use at your own risk.

## Layout

```
tuner/
  config.py       path discovery + constants
  preflight.py    install/GPU checks
  knobs.py        capability registry  <- add new controls here
  mvolt.py        mVolt+ CLI driver (verified writes)
  kombustor.py    Kombustor runner, score harvest, power sampler
  gravitymark.py  GravityMark runner and score/frame-time parsing
  setup.py        interactive sweep planning
  state.py        SQLite journal, heartbeat, boot bookkeeping
  recovery.py     stale clock-range lease handling
  orchestrator.py CLI entry point
scripts/          optional hardmode watchdog + task registration
docs/FINDINGS.md  reference-machine results and traps
web/index.html    standalone web UI (served by --serve)
```

## License

MIT
