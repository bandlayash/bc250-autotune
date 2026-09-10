# Design notes

Why this project is shaped the way it is. Most of these choices were forced by
what the BC-250 and its ecosystem actually do, which is often not what the
documentation suggests — see [UPSTREAM_INTERFACES.md](UPSTREAM_INTERFACES.md)
for the source-level evidence behind each one.

## Defaults

| | |
|---|---|
| Optimization objective | maximum performance up to the onset of thermal throttling |
| Confirmation gate | required only above the "safe" tier in `safety_envelope.yaml` |
| License | MIT, matching the upstream ecosystem |

### Why "thermal throttle onset" and not "the stability wall"

Climbing until the machine crashes is expensive and dangerous. Throttle onset is
a cheaper signal and the hardware hands it over directly:

- **CPU** — `q3_0x43_get_core_freq(core)` reading below the commanded clock is
  clock stretching. `bc250-detect` uses a 50 MHz threshold for exactly this.
- **GPU** — the SMU's throttle status bits, plus GPU temperature from hwmon.

So the optimizer climbs until throttling appears, backs off one increment, and
confirms. It never needs to drive the box into a hang. The watchdog remains the
safety net for the case where instability arrives before throttling does.

One caveat found on real hardware: **the SMU reported no throttle flags at
94 °C**. The bits are the most precise signal available but cannot be the only
one, which is why `safety_envelope.yaml` also carries temperature ceilings.

### Why the confirm gate is where it is

Writes inside the safe tier proceed unattended; anything above needs
`confirm=True`, which the optimizer surfaces to the user rather than
auto-confirming. Two carve-outs `confirm=True` can never unlock:

- Envelope ceilings are clamps, not warnings. CPU Vid in particular is capped
  **below** the 1325 mV that upstream documents as having destroyed a board.
- `com.cyanskillfish.Governor.TestMode.SetTestMode` — arbitrary off-curve
  voltage/frequency — is never exposed as a tool at all.

## Governor safe-points are not named tiers

A natural assumption is that the governor offers named safe-point presets to
select between. It does not. The TOML `[[safe-points]]` array parses into a
`BTreeMap<MHz, mV>` — an anonymous, ordered V/F curve the governor interpolates
across by live GPU load. `oberon` is simpler still: exactly two operating
points.

So the tool surface is:

| Tool | What it does |
|---|---|
| `get_gpu_curve()` | the V/F points, plus allowed/current/initial ranges |
| `set_gpu_range(min, max, confirm)` | move the frequency window, one step per call |
| `set_gpu_config(min, max, min_mv, max_mv, confirm)` | apply a complete config absolutely |

"Tiers" for the confirm gate are frequency bands defined in
`safety_envelope.yaml`, not names that exist upstream.

## Tune over D-Bus where it exists

`cyan-skillfish-governor-smu` owns `com.cyanskillfish.Governor` on the system
bus and accepts `SetRange`, `SetLoadTarget`, `SetTemperatureThresholds` and
`SetParameters` live. Rewriting `config.toml` and restarting the daemon instead
would be worse in two ways: the restart drops the governor for its
`RestartSec` window, and a bad config leaves it crash-looping.

It also makes the search loop **volatile**, so a power cycle is a guaranteed
rollback. Persisting a config becomes a deliberate, confirm-gated final step
once it has been proven, rather than a side effect of every step.

`oberon` has no IPC at all, so on that backend every change is a file rewrite
plus a restart and *is* persistent. The watchdog matters far more there.
`get_gpu_governor_state()` reports which case you are in.

## CPU overclock search is delegated, not reimplemented

`bc250-detect` is already a closed-loop CPU autotuner: 100 MHz steps, live Vid
feedback, automatic undervolt, per-core throttle detection, best-known-good
written after every step. Rebuilding that would be redundant and strictly more
dangerous, so the server wraps `bc250-detect` / `bc250-apply` rather than
driving the SMU directly for CPU tuning, and the optimizer spends its search
budget on the GPU governor, which has no equivalent.

## Fan control is optional, and often unavailable

The in-tree `nct6683` driver that binds the BC-250's NCT6686 creates `pwmN` as
mode `0444` with no store handler and no `pwmN_enable` at all — writes fail
with `EACCES` even as root. Only the out-of-tree
[`nct6687d`](https://github.com/Fred78290/nct6687d) module provides control.

So telemetry degrades gracefully when fan data is absent, snapshots record
per-channel writability so a rollback reports honestly instead of failing on
something it could never have changed, and fan curves are the last feature to
land rather than a dependency of anything else.

Whether that is worth pursuing is board-specific, and on the reference unit it
turned out not to be. Once `nct6687d` was built and fan control worked, pinning
the fan to 100% produced no measurable gain — because the chip's automatic mode
already runs it at 100% continuously. The fan was never the constraint; the
cooler is. Fan control there is a noise lever, not a performance one.

## Compute units and core unlock are read-only

40 CU can be applied two very different ways — by a BC-250-patched kernel at
probe time via `amdgpu.bc250_cc_write_mode`, which is persistent and needs no
tooling, or by `bc250-cu-live-manager` through `umr` at runtime, which is
volatile unless its boot service is installed. CPU core unlock (6c/12t →
8c/16t) is volatile across cold power cycles and upstream advises hours of
stress testing before trusting the extra cores.

Both are reported, neither is applied. Changing CU topology mid-session would
invalidate every benchmark taken before it, and core unlock changes the machine
underneath the comparison. They are one-time setup steps, not tuning steps.

## The SMU is a single contended resource

The governor daemon, `bc250_smu_oc`, and `bc250-cu-live-manager` all drive the
same SMU mailboxes. Every direct SMU path must use `Bc250Smu(use_flock=True)`
and stop `cyan-skillfish-governor-smu.service` around the write, restarting it
after — the behaviour `bc250-cu-live-manager` already implements. Preferring
D-Bus sidesteps the problem entirely, which is another reason to.

## Measurement discipline

Two hard-won rules, both from results that were wrong before they were right:

**Start every run from the same thermal state.** The same configuration scored
12261 and 10843 depending only on whether the GPU started at 55 °C or 62 °C —
an 11% swing from thermal state alone. Every benchmark waits for a 60 °C
cooldown and records the temperature it actually started at.

**Establish a noise floor before believing a delta.** Run the baseline twice
with nothing changed. A difference smaller than that spread is not a result,
and reporting it as one turns run-to-run variance into a fictional tuning win.
