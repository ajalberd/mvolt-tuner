# mvolt-tuner

Automatic GPU tuning on top of **[mVolt+](https://github.com/b00nz/mVolt)** and
**MSI Kombustor**. It discovers which settings your GPU actually exposes, sweeps
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
| Software | [mVolt+](https://github.com/b00nz/mVolt/releases/latest) and [MSI Kombustor](https://www.geeks3d.com/furmark/kombustor/) |
| Access | an **elevated** terminal — mVolt+ rejects tuning writes without it |

mVolt+ is found automatically if it sits next to this folder, in `Program Files`,
or if you set `MVOLT_EXE`. Kombustor is found under `Program Files\Geeks3D`, or
set `KOMBUSTOR_EXE`.

## Quick start

```powershell
# elevated terminal
python -m tuner --check      # 1. verify install, list this GPU's settings
python -m tuner --setup      # 2. choose which knobs to sweep and their limits
python -m tuner --list --from-config   # 3. preview the resulting grid
python -m tuner --from-config --trials 10   # 4. run
```

`--check` prints a capability table for *your* card and explains every exclusion:

```
core        OK    MHz   0        -1000..1000     global core clock offset
xbar_offset off   MHz   -        -               degenerate range (limit_min == limit_max)
power       off   %     0        -               driver rejects writes to this control
cap         off   W     80.0     -               driver rejects writes to this control
```

`--setup` asks one question per available knob (accept the default with Enter) and
writes `state/config.json`. Use `--setup --yes` for defaults without prompting.

## What a run does

For each point in the grid:

1. **journals the trial** with status `applying` (durable before touching the GPU)
2. applies the settings and **verifies them** against `--status`; a rejected or
   silently-ignored write aborts the trial rather than poisoning the data
3. runs the Kombustor benchmark, sampling `nvidia-smi` power/clocks/temp
4. harvests the score from Kombustor's log
5. journals the result, updates `data/live.json`, restores safe defaults

Crashed or interrupted trials are recorded with the reason and excluded from
results. On restart, any trial left mid-flight is reclaimed as `interrupted`.

### Recovery modes

```
--recovery auto       (default)  assume mVolt+ reverts its own settings after a
                                 crash; no external watchdog, nothing to install
--recovery hardmode              arm scripts/watchdog.ps1, which reboots the box
                                 if the heartbeat goes stale
```

`auto` is correct for most users. **`hardmode` cannot rescue a hung kernel** — a
reboot still needs a working OS. See [docs/FINDINGS.md](docs/FINDINGS.md) trap 12.

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
mVolt+ must be closed for the write; the route refuses otherwise.

## Scope and honesty

This is a tuner, not a magic overclock. On a **power-limited** laptop the only
lever may be a clock offset worth a few percent — and some offsets will hang the
machine. The tool's value is finding that limit safely, proving the result, and
refusing the settings that break things.

Measured results on the reference machine, plus 13 traps worth reading before
changing anything: **[docs/FINDINGS.md](docs/FINDINGS.md)**.

## Safety

The tuner restores safe defaults on every exit path. It cannot stop you from
choosing a range the hardware dislikes — that is what `MAX_AUTO_CORE_MHZ` and the
`--setup` limits are for. Start with small ranges.

Nothing here is affiliated with NVIDIA, MSI or Geeks3D. Use at your own risk.

## Layout

```
tuner/
  config.py       path discovery + constants
  preflight.py    install/GPU checks
  knobs.py        capability registry  <- add new controls here
  mvolt.py        mVolt+ CLI driver (verified writes)
  kombustor.py    benchmark runner, score harvest, power sampler
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
