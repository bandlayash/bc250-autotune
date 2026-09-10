---
name: bc250-optimizer
description: Tune an AMD BC-250's GPU configuration automatically, measuring each step with FurMark and reverting anything that fails. Use when the user asks to tune, optimize, overclock, undervolt, or benchmark a BC-250, or to find its stability or thermal limits.
---

# BC-250 optimizer

Tune a BC-250 by measurement, not assumption. Every step is applied through the
guarded MCP tools, measured with a benchmark, and either kept or rolled back.

You add no new code. Everything here uses the `bc250-autotune` MCP server.

## Before touching anything

1. **`get_server_status()`** — confirms the governor backend, which sensors
   work, and whether `dry_run` is set. Do this first on any unfamiliar box.
2. **`get_watchdog_status()`** — must report `unattended_revert_ready: true`.
   If it does not, say so and stop. Without the watchdog, a config that hangs
   the machine stays applied across reboot and the user has to recover it by
   hand.
3. **`get_safety_envelope()`** — the bounds you must reason within.
4. **`snapshot_config("stock")`** — before any write.

Ask the user for an objective only if they have not stated one. Default:
**maximum performance up to the onset of thermal throttling.**

## Establish the noise floor first

**Do not skip this, and do not shorten it.** Run the baseline benchmark **twice**
without changing anything, and compare the two scores.

That spread is your noise floor. Any later change smaller than it is not a
result. On the reference unit two unchanged stock runs differed by roughly 2%,
which is larger than the effect of a 50 MHz step — so a single run showing
"+1.5%" means nothing at all.

If you skip this you will report thermal drift and run-to-run variance as tuning
wins, which is worse than not tuning.

## The loop

For each step:

1. `snapshot_config("<label>")` — `set_gpu_range` also snapshots automatically,
   but an explicit label makes the session readable afterwards.
2. `set_gpu_range(min, max)` — one step per call; the tool refuses larger jumps.
   If it returns `requires_confirmation`, **surface that to the user** with the
   value and the reason. Do not auto-confirm, especially on a first run on an
   unfamiliar unit.
3. `run_benchmark(label, 120)` — the harness waits for the GPU to cool first,
   so runs are comparable. Do not disable that.
4. Read the result:
   - `aborted: true` → the thermal ceiling was hit. This is the wall. Roll back.
   - `thermally_throttled: true` → the SMU throttled. Also the wall.
   - `score_frames` improved by **more than the noise floor** → keep going.
   - otherwise → no measurable gain; stop climbing.
5. On any failure signal: `rollback("last_good")`, record the boundary, and do
   not retry that value this session.
6. On success: `mark_stable()` once you are confident, which promotes the config
   and clears the watchdog's pending marker.

Finish by backing off one step from the boundary, running one longer
confirmation benchmark (600 s), and only then calling `mark_stable()`.

## What the reference unit taught us

Read this before predicting a result. It is a single board, but the failure
modes generalise.

**Raising the GPU ceiling may do nothing.** Stepping 1600 → 1650 MHz produced
*no* improvement — 12199 → 11979 frames, inside the noise floor — because the
board was already thermally saturated and never reached even 1600 MHz. If the
GPU is thermally limited, frequency headroom is not the constraint and raising
it changes nothing except the number in the config file.

**Check thermals before assuming there is headroom.** That unit runs FurMark at
87 °C average and 95 °C peak *at stock*, drawing ~130 W, with the fan already at
2790 RPM. There was nothing to gain and it was visible up front.

**When the box is thermally bound, undervolting is the lever, not overclocking.**
Less voltage means less heat means the SMU sustains a higher clock. Prefer
lowering voltage at a fixed frequency over raising frequency. Note the guarded
GPU tool currently adjusts frequency only; voltage tuning on `oberon` means
editing its config, and on `cyan-skillfish` means curve points.

**Do not trust the reported GPU clock.** On gfx1013 both `cur_gfxclk` and the
`pp_dpm_sclk` live row read near zero under full load — FurMark's own panel
agrees, showing "Max clock 100MHz" for a run at 95 °C. `avg_gfxclk` is
plausible but does not track load well. **Use benchmark score as your signal**,
never the clock.

**`gpu_busy_percent` and `gfx_activity` are unavailable.** There is no GPU
utilisation reading. Do not wait for one.

## Rules

- **Never** auto-confirm a `requires_confirmation` result on a first run.
- **Never** raise a temperature limit to make a failing config pass. The CPU and
  GPU share a die and a cooler; the throttling is telling you something true.
- **Never** change CU topology mid-session — it invalidates every earlier
  benchmark.
- If `get_telemetry()` ever warns about CPU Vid, **stop and roll back
  immediately**. That is the value that destroys boards.
- Log as you go. Everything is already on disk in `results.jsonl` and the
  snapshots directory; nothing important should live only in your context.

## Reporting

Finish with a plain-language summary:

- what changed, in real units;
- the performance delta **against the noise floor** — say "within noise" when it
  is, rather than quoting a percentage that means nothing;
- where the wall was, and whether it was thermal, power, or stability;
- what the user should do next, including anything the hardware ruled out.

Then run `benchmarks/scripts/generate_readme_benchmarks.py` to update the
README.

An honest "no improvement available, and here is why" is a good outcome. Do not
manufacture a win by picking the best run out of several.
