# Target machine profile

Recorded from `benchmarks/scripts/phase0_recon.sh` on the test unit. Re-run it
after any OS image bump -- several facts here are kernel- and image-specific.

| | |
|---|---|
| Access | driven over SSH; passwordless sudo required (SMU, `umr` and systemd all need root) |
| OS | Bazzite 43 Kinoite (`bazzite-deck` variant), rpm-ostree, `/usr` immutable |
| Kernel | `6.17.7-ba29.fc43.bc250cu` -- a BC-250-specific kernel, layered as a LocalOverride |
| GPU | Cyan Skillfish `1002:13fe` at `01:00.0`, amdgpu, `card1` |
| CPU | AMD BC-250, 8c/16t -- the two harvested cores are already unlocked |
| Session | gamescope/Steam on DP-1 (3840x2160); SSH lands on a tty |
| RAM | 10 GiB visible to the host |

## Findings that shaped the build

**40 CUs are already active, from the kernel.** The boot line
`amdgpu.bc250_cc_write_mode=3` makes the patched amdgpu rewrite the CC/SPI masks
during probe:

```
amdgpu: bc250-40cu-enable: mode=3 se=0 sh=0 CC=0xfff80000->0xffe00000 SPI=0x00000007->0x0000001f
amdgpu: SE 2, SH per SE 2, CU per SH 10, active_cu_number 40
```

Persistent, applied before userspace, no `umr` involved. `bc250-cu-live-manager`
is therefore not needed on this box and `cu_config.py` is read-only.

**The Super I/O driver is `nct6686`, not `nct6687`.** It is in-tree and already
bound (`nct6686-isa-0a20`), exposing `fan1-5_input` and `pwm1-5`. No DKMS build
is required, which removes the immutable-OS problem the plan anticipated and
means fan control is available much earlier than expected. Code matches any of
`nct6686` / `nct6687` / `nct6683` by hwmon `name`.

**`gpu_busy_percent` is unsupported** on gfx1013 (`Operation not supported`).
This is why both governors derive GPU load by other means, and why telemetry
reports it as `null` rather than treating it as an error.

**`pp_dpm_sclk` has a moving middle row.** The table reads `0: 350Mhz`,
`1: <live>Mhz *`, `2: 2230Mhz`, where row 1 is a live readout that changes
between samples (89, 22, 12, 7 MHz observed). It is not a selectable DPM state.
The real bounds are 350-2230 MHz -- see `sysfs.parse_dpm_clock`.

**Screenshots must go through XWayland.** The session is gamescope, and SSH
lands on a tty with no `DISPLAY`. Both `:0` and `:1` are live at 3840x2160.
`grim` and `scrot` are absent; ImageMagick `import` and `convert` are present,
so `capture_screenshot.sh` targets `DISPLAY=:0` with `import`.

## Software state

| Component | State |
|---|---|
| `oberon-governor` | layered, **active + enabled** -- the governor in charge |
| `cyan-skillfish-governor-smu` | v0.4.12 layered from COPR `filippor/bazzite`, installed but **disabled** |
| `stress` | layered (required by `bc250-detect`; `stress-ng` is not a substitute) |
| `bc250_smu_oc` | not yet installed |
| FurMark | not yet installed |
| `umr`, `amdgpu_top`, `dkms` | absent, and not needed given the kernel-side 40CU |

Both governors are installed deliberately, so each backend can be exercised on
real hardware. Only one may run at a time -- their units declare `Conflicts`.

### Existing hand-tuning (do not clobber)

`/etc/oberon-config.yaml` is **1000-1600 MHz @ 875 mV**, with a trail of
backups showing prior points: 2000 MHz @ 1000 mV, 1600 @ 1000, 1500 @ 900, and
an `/etc/oberon-config.safe` at 2000 @ 1000. The progression suggests a
deliberate walk toward a deeper undervolt. Snapshot before touching it, and
treat `oberon-config.safe` as the operator's own known-good fallback.

## Memory configuration

Switched from zram to zswap on request. zram is disabled
(`/etc/systemd/zram-generator.conf` emptied; original at
`zram-generator.conf.bak-pre-zswap`), leaving the 16 GB `/var/swapfile` as the
only swap device, fronted by zswap:

```
zswap.enabled=1 zswap.compressor=zstd zswap.zpool=zsmalloc zswap.max_pool_percent=20
```

Set as persistent rpm-ostree kargs and verified across a reboot. The two do not
usefully stack -- zram sits at priority 100 and is already compressed RAM, so
leaving it enabled would have made zswap inert.

## FurMark

Installed at `~/furmark/FurMark_linux64/furmark` (v2.10.2). Three operational
facts the harness must encode:

**1. It segfaults without an X resource-manager string.** On a freshly started
XWayland display the RM property is empty, and FurMark's DPI probe passes NULL
straight to `strlen`:

```
X11_GetMonitorDPI -> XrmGetStringDatabase -> GetDatabase -> __strlen_avx2   SIGSEGV
```

Seed it before launching, which is what the operator's own `fmtest.sh` does:

```bash
echo "Xft.dpi: 96" | xrdb -merge
```

Unrelated to `--no-score-box`; it crashes identically either way.

**2. It does not reliably exit at `--max-time`.** Runs overshot an 80 s wall
clock on a 30 s request. The harness must impose its own `timeout`, as
`fmtest.sh` does with `timeout $((SECS+25))`. A killed run does not append to
`_scores_maxtime.csv`, so the timeout must be generous enough to let FurMark
write its result.

**3. Results land in `_scores_maxtime.csv`**, already parseable, no
`--export-dir` needed:

```
date,demo,platform,vendor,renderer,api_version,width,height,fullscreen,
antialiasing,max_time,frames,max_gpu_temp,avg_fps,min_fps,max_fps
```

Note `frames` is the headline "score"; `avg_fps` is separate. Prior runs at
1920x1080 on this box: 120 s -> 14602 frames, 121 avg fps, 82 C max; 600 s ->
73126 frames, 121 avg fps, 83 C max.

Launch environment: `XDG_RUNTIME_DIR=/run/user/1000 DISPLAY=:1`, `cd` into the
FurMark directory first (it loads `dylibs/` relatively).

## Thermal reality on this unit

Under FurMark at the current 1000-1600 MHz @ 875 mV tune, sustained readings
were **90-94.75 C GPU** at ~137 W package power, fan at 2777 RPM, with the GPU
holding only ~772 MHz. Two independent temperature sources agree, so this is
real.

Two consequences for the optimizer:

- The box is already thermally limited at its current settings. Headroom for a
  performance-oriented tune is small, and cooling is the binding constraint
  rather than voltage or frequency.
- **The SMU reported no throttle flags at 94 C.** The throttle-bit stopping
  condition cannot be the only signal; the optimizer must also enforce the
  temperature ceilings in `safety_envelope.yaml` (`optimizer_ceiling_gpu_c`,
  `abort_gpu_temp_c`), which is why both exist.
