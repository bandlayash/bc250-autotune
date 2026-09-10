# Safety

**Every BC-250 is different. Tuning one is your responsibility, not this
software's.** Silicon quality, cooling, PSU, and BIOS revision all vary between
units, and a configuration that runs for months on one board can be unstable on
another that looks identical. This applies more to an automated tuner than to a
manual GUI, not less: the agent can apply configurations faster than you can
watch them.

The upstream ecosystem this builds on says the same thing, and one of its
authors destroyed a board learning it.

## The one that kills boards

`bc250_smu_oc` upstream, verbatim:

> Increasing the CPU frequency without undervolting will result in uncapped Vid
> scaling & destroy your hardware! (I have managed to permanently brick one
> BC-250 in this way)

CPU core voltage (Vid) must never exceed **1.325 V**. Three independent
mechanisms enforce this here, and they are deliberately redundant:

1. `safety_envelope.yaml` sets the hard ceiling at **1300 mV**, strictly below
   1325. The margin exists so an off-by-one or rounding error in this code
   still cannot reach the documented threshold.
2. Hard bounds **refuse**, never clamp. A refused write applies nothing. A
   silent clamp would leave the caller believing it applied one config while the
   hardware ran another, and every benchmark afterwards would be attributed to
   the wrong settings.
3. `telemetry.py` carries an independent tripwire at 1325 mV that fires on
   *observed* Vid. It duplicates the threshold on purpose: the envelope governs
   what we ask for, the tripwire observes what happened, and it must still fire
   if the envelope is missing, misconfigured, or loosened.

`confirm=True` raises the envelope tier. It cannot unlock a hard bound.

## What protects you

**One step per call.** No tool moves the configuration more than a single
increment (50 MHz for the GPU by default). An agent cannot jump tiers, and
`confirm=True` does not license a jump.

**Snapshot before every write.** The verbatim bytes of every config file are
captured before the hardware is touched. If the snapshot cannot be written, the
write is refused -- a change with no way back does not proceed.

**A watchdog in a separate process.** Before applying anything, the server
fsyncs a `pending.json` marker to disk. If the box hangs, the marker survives,
and on the next boot `bc250-watchdog.service` restores `last_good` before any
governor starts. This is why the watchdog is a separate process: in-memory
state is exactly what a hang destroys.

The watchdog is deliberately conservative. A clean reboot mid-session also
leaves a pending marker, so an orderly restart during tuning reverts too. That
trade is intentional: a spurious revert costs one re-apply, a missed revert
costs a box that boots into a config that hangs it.

**Volatility is a safety asset.** On `cyan-skillfish` the tuning loop runs over
D-Bus, so nothing survives a power cycle -- pulling the plug is a guaranteed
rollback. On `oberon` there is no IPC, so changes are written to
`/etc/oberon-config.yaml` and *do* persist. The watchdog matters far more on
oberon. `get_gpu_governor_state()` reports which case you are in via
`persistence`.

**Dry run.** `DRY_RUN=1` exercises the entire loop -- envelope, step limit,
snapshot, pending marker -- without touching hardware. Refusals are still
refusals in dry run, or the rehearsal would be lying to you.

## What does not protect you

**Thermal limits are not fully reported.** On the test unit the SMU reported
**no throttle flags at 94 °C** under sustained load. The throttle bits from
`gpu_metrics` are the most precise signal available, but they cannot be the only
one, which is why `safety_envelope.yaml` also carries
`optimizer_ceiling_gpu_c` and `abort_gpu_temp_c`. Do not remove those on the
assumption that the hardware will tell you when it is too hot.

**Fan control may not exist on your box.** The in-tree `nct6683` driver that
binds the BC-250's NCT6686 exposes `pwmN` read-only, so the fan cannot be sped
up to buy thermal headroom. Only the out-of-tree `nct6687d` module provides
control. Check `get_telemetry()` for fan readings before assuming otherwise.

**A benchmark is not a stability test.** A config that survives a 60-second
FurMark pass can still fail hours later under a different workload. Upstream
advises hours of `mprime`/`stress-ng` and a `dmesg` check for MCEs before
trusting an overclock -- especially with the two harvested CPU cores unlocked,
which were disabled at the factory and may have been disabled for a reason.

**Cooling is usually the real limit.** Do not raise a temperature limit to stop
the CPU throttling under GPU load. The CPU and GPU share a die and a cooler;
the throttling is telling you something true. Upstream is explicit: improve the
thermal solution instead.

## Recovering a box you cannot log into

1. **Power cycle it fully** (not a warm reboot). Everything volatile is gone:
   live D-Bus governor state, live CU/WGP routing, and the CPU core unlock all
   revert to stock by themselves.
2. On the next boot the watchdog restores `last_good` before any governor
   starts, provided `bc250-watchdog.service` is enabled and
   `/var/lib/bc250-autotune/snapshots/last_good.json` exists. Check both with
   `get_watchdog_status()` **before** starting an unattended run.
3. If it still will not boot, the persistent layers are
   `/etc/oberon-config.yaml`, `/etc/cyan-skillfish-governor-smu/config.toml`,
   and `/etc/bc250-smu-oc.conf` plus its enabled unit. Boot from external
   media and revert them by hand; every snapshot is plain JSON containing the
   original file contents verbatim.

## Before an unattended run

- `get_watchdog_status()` reports `unattended_revert_ready: true`.
- A `last_good` snapshot exists and reflects a config you actually trust.
- You are willing to lose the box. Do not tune a machine you cannot afford to
  have offline.
