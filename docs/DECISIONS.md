# Decisions and plan corrections

Answers to the build plan's open questions, plus corrections forced by Phase 0 recon.
See [UPSTREAM_INTERFACES.md](UPSTREAM_INTERFACES.md) for the evidence behind each correction.

## Open questions, answered

| # | Question | Answer |
|---|---|---|
| 1 | First-target distro | **Bazzite** (rpm-ostree / immutable) |
| 2 | Default optimization objective | **Max performance up to the onset of thermal throttling** |
| 3 | License | **MIT**, matching the upstream ecosystem |
| 4 | `confirm=True` strictness | **Only above the "safe" tier** in `safety_envelope.yaml` |

### On objective (2)

The stopping condition is **thermal throttle onset, not the stability wall**. That is a
cheaper and much safer signal than crash-hunting, and the hardware hands it to us directly:

- CPU: `q3_0x43_get_core_freq(core)` reading below the commanded clock means clock
  stretching. `bc250-detect` already uses a 50 MHz threshold for exactly this.
- GPU: the governor's own `temperature.throttling` threshold, plus GPU temp from hwmon.

So the optimizer climbs until throttling appears, backs off one increment, and confirms.
It does **not** need to drive the box into a hang to find the answer. The watchdog stays as
the safety net for the case where instability arrives before throttling does.

### On the confirm gate (4)

Writes inside the safe tier proceed unattended; anything above it requires `confirm=True`,
which the skill surfaces to the user rather than auto-confirming. Two hard carve-outs that
`confirm=True` can never unlock, regardless of what the caller passes:

- `safety_envelope.yaml` ceilings are clamps, not warnings. Vid in particular is capped
  below upstream's 1325 mV brick threshold.
- `com.cyanskillfish.Governor.TestMode.SetTestMode` (arbitrary off-curve V/F) is never
  exposed as an MCP tool at all. It is reserved for the Phase 2 watchdog test behind
  `--i-know-this-may-hang`.

## Corrections to the build plan

### 1. Governor safe-points are not named tiers

The plan assumed `list_gpu_safepoints()` returns "available TOML safe-points with
names/descriptions" and `apply_gpu_safepoint(name)` applies one. Neither exists. The TOML
`[[safe-points]]` array parses to a `BTreeMap<MHz, mV>` — an anonymous, ordered V/F curve
that the governor interpolates across based on live GPU load.

**Revised tool surface:**

| Plan | Actual |
|---|---|
| `list_gpu_safepoints()` | `get_gpu_curve()` — the V/F points, plus allowed/current/initial ranges |
| `apply_gpu_safepoint(name)` | `set_gpu_range(min_mhz, max_mhz, confirm)` — the real tuning knob |
| — | `set_gpu_curve_point(mhz, mv, confirm)` — edits the V/F curve, monotonicity enforced |

"Tiers" for the confirm gate become **frequency bands** we define in
`safety_envelope.yaml`, not upstream names.

### 2. Tune over D-Bus, not by rewriting TOML

The governor owns `com.cyanskillfish.Governor` on the **system** bus and accepts
`SetRange`, `SetLoadTarget`, `SetTemperatureThresholds`, and `SetParameters` live. The plan's
implied edit-config-and-restart loop is unnecessary and much worse: a restart drops the
governor for `RestartSec=5`, and a bad config leaves the daemon crash-looping.

**Consequence:** iterative tuning is entirely volatile and therefore cheap to undo — a
power cycle is a guaranteed rollback. Persisting to `config.toml` becomes a deliberate,
separate, `confirm`-gated final step once a config has been proven, not part of the search
loop. This also simplifies the watchdog: it only has to reason about the persistent layer.

### 3. Do not reimplement CPU overclock search

`bc250-detect` is already a closed-loop CPU autotuner: 100 MHz steps, live Vid feedback,
automatic undervolt, per-core throttle detection, best-known-good written after every step.
Rebuilding that would be redundant and strictly more dangerous.

**Consequence:** the optimizer skill delegates CPU tuning to `bc250-detect` and spends its
own search budget on the **GPU governor**, which has no equivalent autotuner. The MCP server
wraps `bc250-detect`/`bc250-apply` rather than driving the SMU directly for CPU OC.

### 4. Fan control is last, not a Phase 1 dependency

`nct6687d` is an out-of-tree DKMS module and Bazzite is immutable. It may need akmods, a
COPR kmod, or rpm-ostree layering, and it needs re-verification after every OS image bump.

**Consequence:** `get_telemetry()` must degrade gracefully when `nct6687` is absent —
report fan RPM as `null` rather than failing. `apply_fan_curve` lands only after the module
is confirmed working on this specific box. The "quiet + cool" objective depends on it.

### 5. `bc250-40cu-unlock` does not exist

The plan named five upstream tools; the fifth is not a real repo. 40CU work is in
**bc250-cu-live-manager** (`enable all` / `stock-dispatch`), and CPU core unlock is
**bc250-core-unlock** — itself duplicated by `bc250-cu-live-manager cpu-unlock`, which we
prefer so there is one code path.

**Consequence:** CPU core unlock is out of scope for automated tuning. It is volatile across
cold boots, upstream advises hours of stress testing before trusting the extra cores, and it
changes the machine underneath every benchmark taken before it.

### 6. SMU access must be serialized

The governor daemon, `bc250_smu_oc`, and the CU manager all drive the same SMU mailboxes.
Every direct SMU path in our code must use `Bc250Smu(use_flock=True)` and stop
`cyan-skillfish-governor-smu.service` around the write, restarting it after — the behaviour
`bc250-cu-live-manager` already implements. Preferring D-Bus avoids the problem entirely.
