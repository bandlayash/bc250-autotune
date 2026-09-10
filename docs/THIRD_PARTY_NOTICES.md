# Third-party notices

This project drives, wraps, or builds on the work below. None of it is bundled;
all of it is installed separately by the operator. Interfaces were read from
source rather than documentation — see
[UPSTREAM_INTERFACES.md](UPSTREAM_INTERFACES.md).

## BC-250 ecosystem

**[bc250-control-center](https://github.com/movacx/bc250-control-center)** — the
Linux control center that catalogues the BC-250 tool ecosystem. Our starting
point for finding everything below.

**[cyan-skillfish-governor](https://github.com/filippor/cyan-skillfish-governor)**
(`smu` branch) — adaptive GPU governor. One of the two supported backends. Its
live D-Bus interface is what makes volatile, instantly-revertible GPU tuning
possible.

**[oberon-governor](https://gitlab.com/mothenjoyer69/oberon-governor)** —
the other supported backend, and the one running on the test unit. Originally
by [TuxThePenguin0](https://gitlab.com/TuxThePenguin0/oberon-governor).

**[bc250_smu_oc](https://github.com/bc250-collective/bc250_smu_oc)** by the
[BC-250 Collective](https://github.com/bc250-collective/) — CPU overclocking and
undervolting via SMU mailbox, plus the `bc250_smu` Python library. Its
`bc250_limits.py` supplies the hard bounds in our safety envelope, and its
hard-won warning about 1.325 V Vid is the reason those bounds exist at all.
`bc250-detect` is already a competent CPU autotuner, so we delegate to it rather
than reimplementing that search.

**[bc250-cu-live-manager](https://github.com/WinnieLV/bc250-cu-live-manager)** —
live WGP/CU routing for 24CU ⇄ 40CU, and CPU core unlock.

**[bc250-core-unlock](https://github.com/rw-r-r-0644/bc250-core-unlock)** —
standalone 6c/12t → 8c/16t unlock via the SMU core presence mask.

**[nct6687d](https://github.com/Fred78290/nct6687d)** — out-of-tree hwmon driver
for the Nuvoton NCT6687-R. Required for *fan control*; the in-tree `nct6683`
driver that binds this chip exposes PWM read-only.

**[amdgpu_top](https://github.com/Umio-Yasuno/amdgpu_top)** — recommended
upstream for SMU metric monitoring.

## Benchmark

**[FurMark 2](https://geeks3d.com/furmark/)** by Geeks3D — proprietary freeware,
**not bundled**. Install it yourself; the harness locates it via
`BC250_FURMARK_DIR` or a few conventional paths.

## On the test unit

The `gpu_metrics` decoder in `bc250_mcp/gpu_metrics.py` is adapted from
`bc250-metrics.py`, written by the owner of the test unit, whose struct offsets
were verified on-device by correlating `system_clock_counter` against uptime and
`average_socket_power` against the hwmon PPT reading. That verification is why
the decoder is trustworthy; header-derived offsets alone would not have been.

## License

This project is MIT licensed. Each project above carries its own license —
consult each repository.
