# Upstream interfaces

Reference for every external tool `bc250-autotune` drives. Recorded from reading the
sources, not from documentation alone. Re-verify after upstream bumps.

| Tool | Repo | Role here |
|---|---|---|
| cyan-skillfish-governor (smu branch) | `filippor/cyan-skillfish-governor` @ `smu` | GPU V/F curve + live governor control |
| bc250_smu_oc | `bc250-collective/bc250_smu_oc` | CPU OC/UV via SMU; also a Python SMU library we import |
| bc250-cu-live-manager | `WinnieLV/bc250-cu-live-manager` | 24CU to 40CU WGP routing, CPU core unlock |
| bc250-core-unlock | `rw-r-r-0644/bc250-core-unlock` | Standalone 6c/12t to 8c/16t unlock |
| nct6687d | `Fred78290/nct6687d` | Nuvoton NCT6687-R hwmon driver, fan PWM |

> A tool named `bc250-40cu-unlock` is sometimes referenced but does not exist. The
> 40CU work lives in **bc250-cu-live-manager**, and CPU core unlock in
> **bc250-core-unlock**.

---

## 1. cyan-skillfish-governor (smu branch)

A **daemon**, not a one-shot CLI. It samples GPU load and continuously ramps frequency
between entries of a V/F table, applying via SMU or kernel sysfs.

### Invocation and packaging

```bash
cyan-skillfish-governor-smu [-v|--verbose] [CONFIG]   # CONFIG optional; internal defaults if omitted
```

- Config: `/etc/cyan-skillfish-governor-smu/config.toml`
- Unit: `cyan-skillfish-governor-smu.service`, `ExecStart=/usr/bin/cyan-skillfish-governor-smu /etc/cyan-skillfish-governor-smu/config.toml`, `Restart=on-failure`, `RestartSec=5`
- Conflicts with `cyan-skillfish-governor.service`, `cyan-skillfish-governor-tt.service`, `oberon-governor.service`. Only one governor at a time.
- **Bazzite install:** COPR `filippor/bazzite`; on rpm-ostree, `sudo rpm-ostree install cyan-skillfish-governor-smu` then reboot.

### Config schema (`src/config.rs`)

| Key | Meaning |
|---|---|
| `timing.intervals.sample` / `.adjust` | microseconds sampling / adjustment period |
| `timing.ramp-rates.normal` / `.burst` | MHz per ms ramp rate |
| `timing.burst-samples`, `timing.down-events` | burst trigger / downshift debounce |
| `frequency-range.min` / `.max` | MHz; `0` or omitted means no limit. Clamped to the GPU's own min/max. |
| `frequency-thresholds.adjust` | MHz; ignore changes smaller than this |
| `load-target.upper` / `.lower` | fractions (e.g. `0.65`) |
| `temperature.throttling` / `.throttling_recovery` | degrees C |
| `gpu.set-method` | `"smu"` or `"kernel"` |
| `gpu-usage.method` | `"busy-flag"`, `"process"`, or `"kernel"` |
| `gpu-usage.fix-metrics`, `.fix-freq`, `.flush-every` | metric workarounds |
| `dbus.enabled` | expose the live control interface |
| `[[safe-points]]` | array of `{ frequency = <MHz>, voltage = <mV> }` |

### Safe-points are NOT named tiers

`safe_points` parses into a `BTreeMap<u32 /*MHz*/, u32 /*mV*/>` — an **ordered V/F curve**,
with no names or descriptions anywhere in the format. The governor picks a point
dynamically from GPU load; it does not "apply a safe-point".

Validation enforced by `validate_safe_points()`:

- Frequency keys must be unique (duplicate gives `multiple supposedly safe voltages for N MHz`).
- Sorted by frequency, voltage must be **monotonically non-decreasing**. A higher
  frequency may never carry a lower voltage than a lower frequency.
- Neither value may exceed `10000` (else `unrealistic`).
- Empty array is rejected. If the key is absent entirely, defaults are `350 MHz @ 700 mV`
  and `2000 MHz @ 1000 mV`.

Shipped `default-config.toml` curve (conservative; higher points ship commented out):
`500@700, 1000@800, 1175@850, 1500@900, 1600@910, 1700@920, 1850@930, 2000@960`.
The commented-out extension reaches `2400 MHz @ 1150 mV`.

### D-Bus live control — the important find

The daemon owns a **system-bus** name, so tuning can be changed live with no config
rewrite and no service restart. This is strictly better than rewriting the TOML and restarting,
and is what the server uses for iterative tuning.

- Bus: **system**
- Name: `com.cyanskillfish.Governor`
- Object: `/com/cyanskillfish/Governor`

`com.cyanskillfish.Governor.PerformanceMode` (policy: **any authenticated user**):

| Member | Signature | Notes |
|---|---|---|
| `SetFixedFrequency` | `(u frequency)` | pin frequency |
| `SetRange` | `(u min, u max)` | MHz window — the main tuning knob |
| `SetLoadTarget` | `(d min, d max)` | fractions |
| `SetTemperatureThresholds` | `(u throttling, u recovery)` | degrees C |
| `SetParameters` | `(u minF, u maxF, d loadMin, d loadMax, u throttleT, u recoveryT)` | all at once |
| properties | `LoadTargetMin/Max`, `TemperatureThrottling/Recovery`, `Enabled` | read/write |

Invalid arguments come back as `org.freedesktop.DBus.Error.InvalidArgs` — a free
validation layer on top of our own envelope.

Range objects, interface `com.cyanskillfish.Governor.Range` (`Min`, `Max` properties):

| Object path | Meaning |
|---|---|
| `/com/cyanskillfish/Governor/Range/Current` | live window (**writable**) |
| `/com/cyanskillfish/Governor/Range/Allowed` | hardware ceiling, read-only |
| `/com/cyanskillfish/Governor/Range/Initial` | window from config, read-only |

`com.cyanskillfish.Governor.TestMode` — `SetTestMode(u frequency, u voltage)` sets an
arbitrary off-curve V/F pair. **Root only**, explicitly denied to `context="default"` in
`com.cyanskillfish.Governor.conf`. This is exactly the "may hang" primitive the watchdog
test needs, and it must never be exposed as an MCP tool.

---

## 2. bc250_smu_oc

Python package. Two console entry points plus an importable SMU library.

### `bc250-detect` is already a CPU autotuner

```
bc250-detect -f MHz -v mV [-t degC] [-k] [-c path]
  -f/--frequency  target OC frequency (MHz)
  -v/--vid        core voltage limit (mV)
  -t/--temp       CPU+GPU temp limit degC (default 90; stock is 100)
  -k/--keep       keep the OC after exit (otherwise reverts on exit)
  -c/--config     config path (default ./overclock.conf)
```

It runs its own closed-loop search: 100 MHz steps up from 3500, `stress` load, reads live
Vid, drops the V/F curve `scale` when Vid exceeds the limit, watches per-core clocks for
throttling, and writes the best-known-good config after each successful step.

**Scope consequence:** do not reimplement CPU OC search in the optimizer skill. Delegate to
`bc250-detect` and spend the skill's search budget on the GPU governor, which has no
equivalent autotuner.

### `bc250-apply`

```
bc250-apply [-a|--apply] [-i|--install] path
```

- Config format is **INI**, not JSON or TOML:

  ```ini
  [overclock]
  frequency = 4000
  scale = -12
  max_temperature = 90
  ```

- Installed config: `/etc/bc250-smu-oc.conf`
- Unit: `/etc/systemd/system/bc250-smu-oc.service`, enable with `systemctl enable bc250-smu-oc`
- Apply order: set CPU max temp, set GPU max temp, `disable_extra_cpu_gpu_voltage(True)`,
  scale V/F curve, set max CPU boost clock.

### Hard limits from `bc250_limits.py` (source of truth for `safety_envelope.yaml`)

| Parameter | Min | Max |
|---|---:|---:|
| CPU frequency (MHz) | 3500 | 4500 |
| Vid (mV) | 950 | 1325 |
| Temperature (degC) | 0 | 100 |
| V/F curve `scale` | -50 | 0 |

Upstream's own guidance, stronger than the numeric limits:

- **Vid must never exceed 1.325 V under any circumstance.** The author bricked a board by
  raising frequency without undervolting and letting Vid scale uncapped.
- Stay **below 1300 mV** in practice; try 1275 first, 1300 only if unstable.
- Stock Vid at 3.5 GHz is around 1180 mV, so even a stock-clock tune yields roughly 200 mV
  of undervolt.
- Prefer a 90 degC limit over the 100 degC stock. Do **not** raise the temp limit to fix
  GPU-load-induced CPU throttling — CPU and GPU share a die and a cooler.

### `bc250_smu` library (importable — our server is Python too)

`from bc250_smu import Bc250Smu`. Root required; touches
`/sys/bus/pci/devices/0000:00:00.0/config`. Construct with `use_flock=True` to coordinate
with other processes. `close()` when done.

| Method | Use |
|---|---|
| `check_test_message()` | mailbox health probe, call before anything |
| `get_smu_version()` | version |
| `query_gfxclk()` | GFX clock (MHz) |
| `get_gfx_vid()` | GFX Vid (mV) |
| `q3_0x36_get_current_cpu_voltage()` | live CPU Vid (mV) |
| `q3_0x37_get_current_gpu_voltage()` | live GPU voltage |
| `q3_0x43_get_core_freq(core_id)` | per-core clock, throttle detection |
| `q3_0x40_get_cpu_temp_max()` | current CPU temp limit |
| `q3_0x8b_set_cpu_max_temperature(degC)` / `q3_0x8c_set_gpu_max_temperature(degC)` | temp limits |
| `q3_0x50_scale_f_vid_curve(scale)` | V/F curve scale (-50..0) |
| `q3_0x8f_set_max_cpu_boost_clk(MHz)` | CPU boost ceiling |
| `disable_extra_cpu_gpu_voltage(bool)` | must be `True` before raising clocks |
| `force_gfx_freq(MHz)` | pin GFX clock |

Revert-to-stock sequence (from `bc250_detect.revert_defaults`), the CPU half of `rollback()`:
`set_max_cpu_boost_clk(3500)`, `scale_f_vid_curve(0)`, `disable_extra_cpu_gpu_voltage(False)`,
`set_cpu_max_temperature(100)`, `set_gpu_max_temperature(100)`.

VID codec: `voltage_v = vid * -0.00625 + 1.55`; helpers `codec.mv_to_vid()` / `codec.vid_to_mv()`.

Mailbox status bytes: `0x01` OK, `0xFF` failed, `0xFE` unknown command, `0xFD` rejected
(prerequisite), `0xFC` rejected (busy).

**Contention:** the governor daemon also talks to the SMU. `bc250-cu-live-manager` stops
`cyan-skillfish-governor-smu.service` around its own SMU writes and restarts it after. Our
server must do the same, and always use `use_flock=True`.

---

## 3. bc250-cu-live-manager

Single bash script, fully non-interactive when given a subcommand. Requires `umr` for
register access and `setpci` (pciutils) for the SMU mailbox.

```
bc250-cu-live-manager.sh <command> [options]
```

| Command | Effect |
|---|---|
| `status` | dashboard; includes `CUs active & routed : X/40` and CPU thread state |
| `enable all` | route all 20 WGPs, 40 CUs |
| `disable all` | disable all dispatch WGPs |
| `stock-dispatch` | restore driver boot topology, 24 CUs |
| `enable-wgp SE.SH.WGP...` / `disable-wgp ...` | per-WGP, e.g. `1.0.4` |
| `cpu-unlock` | SMU core presence mask `0x77` to `0xFF` (6c/12t to 8c/16t) |
| `write-service-table` | save live table to `/etc/bc250-cu-live-manager.conf` |
| `install-service` / `uninstall-service` / `apply-service` | boot restore |
| `install-umr` | install umr via apt/pacman/paru/rpm-ostree/dnf |
| `table` / `menu` | interactive — **never call from the MCP server** |

| Option | Note |
|---|---|
| `-y, --yes` | skip the risky-write acknowledgment, **required** for automation |
| `-n, --dry-run` | print UMR writes without executing, back this with `DRY_RUN=1` |
| `-i, --umr-instance N` | force umr DRI instance |
| `--force` | proceed when the BC-250 PCI ID is not detected |

Paths: config `/etc/bc250-cu-live-manager.conf`; unit `bc250-cu-live-manager.service` in
`/etc/systemd/system/`; binary installed to `/usr/local/bin/bc250-cu-live-manager`
(falls back to `/var/usrlocal/bin/` on rpm-ostree systems like Bazzite).

Topology: 1 WGP = 2 CUs. Four rows (`SE0.SH0`, `SE0.SH1`, `SE1.SH0`, `SE1.SH1`) times 5 WGPs.
Factory is WGP0-2 per row (12 WGPs / 24 CUs); full is 20 WGPs / 40 CUs.
Status markers: `D+` driver+routed, `S+` SPI+routed (not in boot map), `D!` driver+off, `--` off.

Live routing is **volatile**. It reverts on reboot unless the table is saved *and* the
service is installed.

---

## 4. bc250-core-unlock

```bash
sudo systemctl stop cyan-skillfish-governor-smu
sudo ./bc250-unlock-cores.py       # -f to force a non-0x77 mask
sudo reboot
```

- Refuses to write unless the mask reads exactly `0x77`; any other value suggests a real
  harvest of defective cores. `-f` overrides at the user's risk.
- Survives a **warm** reboot; a cold power cycle reverts to stock. Re-run after power loss.
- The governor service must be stopped for SMU access.
- Upstream advises hours of `mprime`/`stress-ng` plus a `dmesg` MCE check before trusting
  the extra cores.
- Functionally duplicated by `bc250-cu-live-manager cpu-unlock`. Prefer the CU manager so
  there is one code path, and treat core unlock as out of scope for automated tuning.

---

## 5. nct6687d

Out-of-tree DKMS kernel module for the Nuvoton NCT6687-R Super I/O chip. Without it there
are no fan RPM readings or PWM controls.

- Build: `make dkms/install` (needs `dkms`, `make`, `gcc`, kernel headers).
- Load: `modprobe nct6687`; persist via `/etc/modules-load.d/nct6687.conf`.
- Appears in lm-sensors as **`nct6687-isa-0a20`**.
- `manual=1` module parameter if voltage sensors read wrong on a given board.

Control is plain **hwmon sysfs**, no CLI. Resolve the instance by matching `name`:

```bash
for h in /sys/class/hwmon/hwmon*; do [ "$(cat "$h/name")" = "nct6687" ] && echo "$h"; done
```

Then `pwmN` (0-255 duty), `pwmN_enable` (fan control mode), `fanN_input` (RPM),
`tempN_input` (millidegrees C). **Instance numbers are not stable across boots. Always
resolve by `name`, never hardcode `hwmon2`.**

> Bazzite is rpm-ostree / immutable. DKMS is not the normal path there — this needs
> `akmods`, a COPR-provided kmod, or `rpm-ostree` layering, and it must be re-verified
> after every OS image bump. Confirm on the box before building fan control on it, and
> treat fan control as the *last* feature to land, not a dependency of anything else.

---

## Cross-cutting notes

**SMU is a single contended resource.** The governor daemon, `bc250_smu_oc`, the CU
manager's `cpu-unlock`, and core-unlock all drive the same mailboxes. Rules:

1. Always `Bc250Smu(use_flock=True)`.
2. Stop `cyan-skillfish-governor-smu.service` around direct SMU writes; restart after.
3. Prefer the governor's **D-Bus** interface over direct SMU writes whenever it can express
   the change. No contention, no restart, and it validates arguments for us.

**Volatility ladder** — what survives what:

| Change | Reboot | Cold power cycle |
|---|---|---|
| Governor D-Bus (`SetRange`, ...) | no | no |
| Governor `config.toml` | yes | yes |
| CPU OC via `bc250-apply --install` plus enabled unit | yes | yes |
| CU/WGP routing, live | no | no |
| CU/WGP routing plus saved table plus service | yes | yes |
| CPU core unlock | yes (warm) | no |

The watchdog's "revert to last_good on boot" only needs to undo the persistent rows.
Everything volatile is already gone by the time it runs, which is a safety asset: an
unattended tuning session that hangs the box recovers to stock on a power cycle by itself.

**Two independent temperature ceilings** that must not drift apart: the governor's
`temperature.throttling` (GPU governor behaviour) and the SMU's CPU/GPU max temperature
(`q3_0x8b` / `q3_0x8c`, set by `bc250-apply`). `safety_envelope.yaml` owns both.
