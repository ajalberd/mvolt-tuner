# Findings: RTX 5080, first night (2026-11-10/11)

This covers the 79 runs in `state/journal.db`. The benchmark is Kombustor FurMark-MSI at 1080p fullscreen, about 62 s per run.
Scores are anchored per boot (explained further down). Only trials 6–91 are directly comparable.

## Where the card's edge is

How much core offset stays stable depends mostly on **REL (NVVDD voltage)**. Contrary to the usual intuition, a **deeper undervolt holds more core, not less**.

| REL band (mV) | highest clean core | lowest failing core |
|---|---|---|
| −160 … −90 | +470 | +560 |
| −90 … −30 | +395 | +410 |
| −30 … +30 | +365 | +455 |
| +30 … +100 | +335 | +395 and up all failed |

A crash model fitted to every run (logistic in core, REL and power budget) gives:

- **about +8 MHz of stable core for every 10 mV of undervolt**
- a small penalty for a higher power budget (about −1 MHz per 10 W)

Modelled crash chance at a 335 W budget:

| REL | 10% | 50% | 90% |
|---|---|---|---|
| −140 | +450 | +529 | +608 |
| −100 | +418 | +497 | +576 |
| −65 | +390 | +469 | +548 |
| −25 | +358 | +437 | +516 |
| 0 | +338 | +417 | +496 |
| +50 | +298 | +377 | +456 |

These are wide bands of about ±80 MHz between 10% and 90%. That is partly real marginality and partly noise from the other settings, which the model averages over. The edge search narrows the bands lane by lane.

## What moves the score

Effect sizes come from ordinary least squares on all comparable runs. Run-to-run noise is about ±700 points.

| control | score | watts |
|---|---|---|
| power budget | **+356 per 10 W** | (sets the watts) |
| core offset | **+665 per 100 MHz** | small |
| XBAR offset | **+322 per 100 MHz**, clear | small |
| REL (undervolt) | no measurable score effect | **−1.6 W per 10 mV** |
| memory offset | none measurable here | +3.2 W per 500 MHz |
| SYS offset | none | none |

In practice:

- Undervolting does not buy score directly. It buys **headroom**: more core at the same watts, and fewer watts at the same core.
- Memory and SYS cost watts or risk for nothing in this benchmark. Hold them modest.

## Front so far (vs stock 37.8k @ 352 W)

| trial | score | watts | core | REL | cap | XBAR |
|---|---|---|---|---|---|---|
| 51 | 40,022 (+5.9%) | 252 | +425 | −110 | 335 | +350 |
| 72 | 40,708 (+7.7%) | 297 | +470 | −145 | 350 | +400 |
| 66 | 41,052 (+8.6%) | 318 | +395 | −45 | 355 | +450 |
| 67 | 41,214 (+9.0%) | 339 | +335 | 0 | 370 | +500 |

At **~250–300 W you already beat stock by 6–8%** while using 50–100 W less. These are single runs, not yet confirmed.

## What errors out

There were **21 driver resets**. The PC survives them, but each one costs a run. All of them came from core sitting above the edge for its REL.

There were **4 hard freezes** (black screen, manual reset). The hard-crash guard now blocks each one:

| trial | config | why it froze | guard blocks |
|---|---|---|---|
| 5 | core +615 / SYS +750 | SYS far beyond anything clean | core ≥ 615 & SYS ≥ 750 & REL ≥ −100 & cap ≥ 290 |
| 21 | XBAR +700 | fabric clock; it also runs at idle (clean max: +550) | XBAR ≥ 700 |
| 88 | core +750 at REL +50 | ~370 MHz past the edge. The old edge search's "ceiling probe" did this | core ≥ 750 & REL ≥ +50 & cap ≥ 320 |
| 96 | core +480 at REL +50 | the old search assumed *more* voltage = more stable, so it went the wrong way | core ≥ 480 & REL ≥ +50 & cap ≥ 300 |

Both freezes during fine-tuning (trials 88 and 96) were bugs in the old edge search, not bad luck. Both causes are fixed:

1. The voltage direction was backwards. A pass at REL +50 was taken to prove REL −100 was stable, and a crash at −100 was taken to prove +50 would crash. Both are now the right way round. `knobs.AGGRESSION` says more voltage is riskier on this card, and the guard uses that too.
2. Each lane started with a jump to the top of the core range. Each lane now starts where the crash model predicts **10% crash chance**, and never steps more than **2 × 15 MHz above its highest pass**.

## Session score shift (open question)

After the reboot at 03:42, the same settings scored **~25% higher**: stock measured 49,300 @ 346 W, against 37,800 earlier. Resolution, fullscreen and settings were all the same. The cause is unknown; candidates are a driver or display state after the freeze, or a Kombustor preset change.

Because of this, each boot now gets one stock **reference run**, and its scores are scaled by `median_ref / this_boot_ref`. A boot without a reference shows no score: it is measured, but not compared. The tuner runs the reference automatically at the start of a session.

## What the 2nd step (edge + confirm) can still squeeze

The front above is already near the edge in its REL bands, so expect **another +1–2% at the same watts**, mainly from:

- **Deep undervolt + higher core:** REL −120 … −140 at core +450 … +480. The model says this is ~10–20% crash risk. The edge search pins it down, and confirm proves it holds 3 times.
- **XBAR around +450 … +500** is worth ~150 points over +350. Stay below +550, the highest clean value.
- Spending the watts saved by undervolting on budget: +356 per 10 W.

Suggested edge plan, which the web page fills in after "Analyse":

- **Budgets:** 395 / 335 / 275 W
- **REL:** −25 / −65 / −100 / −140 mV
- **Core range:** +315 … +615, step 15
- **Size:** about 30 runs, ~50 min

When a lane's edge rests on one lucky pass, the confirm candidates now sit a further 1–2 steps below it, guided by the crash model. In a dry run that replays this card's measured behaviour, all 3 candidates confirmed with this change. Without it, 2 of 3 failed confirmation.

## Dry runs

Dry runs use `state/journal-dryrun.db`, which starts as a copy of the real journal, so the model and the guard behave as they would for real. They also write `data/*-dryrun.json`. The simulator follows the measured edge of this card. The real journal is never touched; this was checked end to end.

## Update: edge-search run of 2026-11-11 morning (trials 97–106), 5th hard freeze

- **Trial 97 (stock reference):** 37,740 @ 349 W. This matches the first night's regime (37.8k), so the 49.3k session was a one-off.
- **Lane 395 W / REL −25:** clean at +345 / +375 / +405 / +435, scoring 41.5–41.8k at ~350 W. +465 and +450 both reset the driver. **Edge: +435 at REL −25.**
- **Lanes 335 W and 275 W at REL −25:** re-probed +465 and +450 (two more resets), then **+465 at 275 W hard-froze**. That was four resets in about 75 s, and the freeze came on the fifth unstable config.

Two causes, both fixed:

1. **Crashes didn't carry across budgets.** On this card a crash hits seconds into the load (average 69–163 W before the reset), long before any power cap binds. The budget doesn't move the edge. A crash now counts for every budget at the same or lower voltage, which cuts the remaining search from about 30 runs to about 9.
2. **Resets accumulate.** Three of the five freezes (trials 21, 96, 106) came after 2+ driver resets in the same boot, with 23–26 NVIDIA driver errors logged first. The run now idles 30 s after a reset. After **2 resets in one boot it stops and asks for a Windows restart**; press Start to resume.

The hard-crash guard now also judges core against clean runs at the same or more voltage. +465 is not new across all runs (+470 ran clean at REL −145), but it is new at REL −25. Trial 106 therefore gets the rule `core ≥ +465 and REL ≥ −25`.

Confirm now picks distinct points: **efficiency**, the most core per budget (deduplicated when the cap wouldn't bind), and **performance**, the best measured score at the top budget.

## Confirm results and the core voltage-demand sweep (2026-11-11 afternoon)

**Confirm (3 runs each, all clean):**

| config | score | watts |
|---|---|---|
| test1 as saved (REL −50, +400, cap 395 W) | 41,909 ± 166 | 344 |
| 335 W / REL −100 / +465 | 40,904 ± 226 | 299 |
| 275 W / REL −100 / +465 | 38,203 ± 134 | 262 |

**Voltage-demand sweep (trials 126–140).** The configs were the two efficiency points, with core voltage demand walked 0 → −10 → −20 → −30 mV, 2 runs per value:

| demand | 335 W lane: score / W / MHz | 275 W lane: score / W / MHz |
|---|---|---|
| 0 | 48,018 / 318 / 2438 (trial 128) | 44,125 / 275 / 2160 |
| −10 | 47,274 / 301 / 2329 | 44,739 / 275 / 2185 |
| −20 | 48,606 / 305 / 2411 | 45,332 / 275 / 2225 |
| −30 | driver reset **on apply** | driver reset at load start |

- **mVolt+ accepts negative values** on this card, and they read back correctly.
- **Edge: −20 mV is the deepest clean value** in both lanes. −30 reset the driver, once while being applied (not under load), which is the "fails at any clock" behaviour expected of a demand offset. −20 has only 2 clean runs each, so it is not confirmed.
- **275 W (cap binding):** each −10 mV bought about 30 MHz and about +1.4% score at the same 275 W. That is the expected mechanism, and the trend is monotone.
- **335 W (cap not binding):** about −13 W to −16 W at a similar score.

**Caveat: the score regime flipped in the middle of this boot.** The same configs scored ~40.6k / ~38.0k earlier in the boot (trials 118–125, ~2790 / ~2550 MHz). From trial 126 they scored ~44–48k, at *lower* reported SM clocks (~2160–2440 MHz). That is the same signature as the 49.3k session after the 03:42 reboot (trials 92–95: ~47.8k at ~2390 MHz). The flip came with the first run that wrote `--core-vdemand` (even at 0), but the 03:42 session had no vdemand writes, so the cause is open. Until it is explained:

- the within-lane deltas above, all measured in the new regime, are the usable result;
- the absolute scores are not comparable with the confirm table;
- the per-boot anchoring assumption (one regime per boot) is broken.

Next test: after a restart, run the same config with no vdemand key and with vdemand 0, interleaved. This shows whether writing the control flips the regime.

### A/B check after a restart (trials 141–149): writing vdemand does not cause the jump

The config was 275 W / REL −100 / +465, run as A A A B B B A A. A had no vdemand write; B had `core_vdemand 0`.

| | runs | score | watts | MHz |
|---|---|---|---|---|
| stock reference | 1 | 37,900 | 352 | 2726 |
| A (before) | 3 | 37,973 | 262 | 2547 |
| B (vdemand 0) | 3 | 37,898 | 263 | 2541 |
| A (after) | 2 | 37,801 | 262 | 2547 |

All runs sit within noise of the earlier confirm (38,203 @ 262 W), so **writing the control changes nothing**. The ~18% jump in trials 126–140 was the same unexplained state as the 03:42 session, and it happened to start at the same time.

Consequences:

- **The vdemand gains still need re-measuring in the normal state.** The earlier deltas (+2.7% at 275 W for −20 mV; −13 to −16 W at 335 W) were measured entirely inside the odd state, where the card ran ~400 MHz lower for more score. They may not carry over.
- **The importance report is wrong about vdemand.** It currently says "core_vdemand −4,187 pts per 10 mV". It is confounded: every vdemand run so far is in the high state, and the reference runs are not. Treat it as an artefact.
- **The tracking problem remains.** A state change inside a boot slips past the per-boot anchor. The signature is easy to spot: score up ~18% while SM clock is ~400 MHz *lower*.

### Voltage-demand re-run in the normal state (trials 150–163): the real answer

The configs were REL −100 / +465, with 3 runs per value. Raw scores:

| demand | 335 W: score / W / MHz | 275 W: score / W / MHz |
|---|---|---|
| 0 | 40,942 / 302.2 / 2792 | 37,864 / 262.4 / 2542 |
| −10 | 41,034 / 299.3 / 2839 | 38,278 / 262.7 / 2588 |
| −20 | driver reset | driver reset |

- **−10 mV is the deepest clean value. −20 mV reset the driver at both budgets.** In the odd high-score state, −20 had passed; that state ran ~400 MHz lower clocks, which would explain why it was more forgiving. The earlier "−20 is clean" result is withdrawn.
- **At 275 W (cap binding):** +46 MHz and **+1.1% score** at the same watts. Real, but small.
- **At 335 W (cap not binding):** +47 MHz, +0.2% score, −3 W. Within noise.
- **Margin:** one 10 mV step separates clean from crash. That is the same thinness as the core edge, and these are 3 benchmark runs, not hours of games.

**Verdict:** core voltage demand −10 mV is worth about 1% at a binding power budget and nothing measurable otherwise. It is optional, and only worth trying if you game with a 275 W-style cap. Test it in real games before keeping it.

The importance report's "core_vdemand −3,320 pts per 10 mV" is still the odd-state artefact; ignore it.

## GravityMark (2026-11-15)

The tuner can now use GravityMark 1.89 (Direct3D 11, 3440x1440 fullscreen,
200,000 asteroids, temporal AA) instead of Kombustor. It is its own study: none
of the Kombustor scores above carry over. Its first stage measures the Kombustor
clean Pareto front (14 configs) plus the test1 reference, 2 runs each, so the
GravityMark front is measured, not assumed.

Stock pass (tuner journal trial 1, 2026-11-15, nothing applied, power 100 %):

| score | FPS | pass | watts avg | SM clock avg |
|---|---|---|---|---|
| 47,950 | 287.1 | 167 s | 223 W | 2869 MHz |

An earlier pass (19,394, 116 FPS) ran while a game was using the GPU; it is
not a measurement and is not in the journal.

Notes:

- **Lighter on power than Kombustor.** About 223 W at stock against
  Kombustor's 300+ W. Power caps below about 230 W will bind; the 250-395 W
  caps of the Kombustor front may not, so those configs could score alike here.
- **About 3 minutes per run** (Kombustor: about 1 minute). A 15-config seed at
  2 runs each is about 1.6 hours.
- The score is the frame count: average FPS × 167 s (287.1 × 167 ≈ 47,950).

## NVIDIA driver 621.24 ignores core voltage offsets (2026-11-15)

After driver 621.24 was installed (2026-11-14), every NVVDD rel/alt write read
back 0. mVolt+ v0.50.11 accepts the write but reports
`"rail_offsets_ignored_by_driver": true`, alone or with other settings, at idle
or with a cap set. Core, memory, fabric offsets and the power cap still apply.
No undervolt (REL) is possible with this driver and mVolt+ build; the tuner now
stops at the first config with that message instead of aborting three.
mVolt+ v0.50.13 (same day) behaves the same. The core voltage-demand offset
(`--core-vdemand`) still applies and reads back on 621.24.

Also found: `--return-to-percentage` keeps the last cap's wattage as the new
percentage (a 250 W cap came back as 69 %). The tuner now writes the
percentage from before the study back afterwards.

## Tuning without REL on driver 621.24 (2026-11-15/16)

With core/fabric voltage offsets ignored by the driver, the study ran on
GravityMark with REL 0 (no undervolt), holding test1's other settings
(memory +2000, XBAR +400, SYS +300, MSVDD clock ratio 1.0).

**Explore** (16 runs, core +200..+440 x caps 250-395 W): caps do not bind on
GravityMark (214-240 W at every cap). Score rises with core and levels off
near +320. Edge at REL 0: clean at +380, driver reset at +395.

**Confirm** (GravityMark, +365 @ 330 W, 3 runs, all clean):

| config | score | watts | SM clock |
|---|---|---|---|
| stock | 47,950 | 223 W | 2869 MHz |
| +365, 330 W cap | **53,015** (+10.6 %) ± 35 | 232 W | 3179 MHz |

**Kombustor validation** (same config, true draw under a heavy load, 2 runs each):

| config | score | watts | SM clock |
|---|---|---|---|
| stock (this session) | 38,417 | 353 W | 2722 MHz |
| +365, 330 W cap | 40,746 (+6.1 %) | 319 W | 2899 MHz |
| +365, 396 W cap | 41,969 (+9.2 %) | 362 W | 2991 MHz |

For comparison, with REL on the old driver: test1 (REL -50, +400) 41,936 @
344 W; tuner-330W (REL -100, +465) about 41.0k @ 300 W. Without REL, the same
score costs about 18 W more, and at a 330 W cap it is about 20 W less
efficient than the REL -100 config. The light GravityMark load hides this:
it never reaches the cap.

**Voltage-demand sweep** (GravityMark, +365 @ 330 W, REL 0, 2026-11-16, PC
left alone; earlier attempts while the PC was in use were under-loaded and
voided):

| core demand | score | watts | SM clock |
|---|---|---|---|
| 0 mV | 52,466 | 229.8 W | 3182 MHz |
| -10 mV | 52,564 (+0.2 %) | 231.0 W | 3201 MHz |
| -20 mV | driver reset | | |

-10 mV does not lower the watts: the card spends the margin on about 19 MHz
more clock at the same power, worth +0.2 % (within run-to-run noise). -20 mV
crashes, as it did on Kombustor. Without REL, voltage demand is not a useful
undervolt on this card; the recommendation stays +365 @ 330 W at 0 mV.
