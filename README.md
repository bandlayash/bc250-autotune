# BC-250 AutoTune

An MCP server exposing AMD BC-250 telemetry and tuning controls, plus an
optimizer that uses them to tune a unit automatically — with a boot watchdog
that reverts an unproven configuration if the machine hangs.

> **Every BC-250 is different, and tuning one is your responsibility.** Read
> [docs/SAFETY.md](docs/SAFETY.md) before pointing this at hardware you care
> about. It matters more for an automated tuner than for a manual GUI, not
> less.

## What it does

- **Reads** GPU/CPU temperature, power, clocks, voltage, fan RPM, and SMU
  throttle status — no root, no SMU mailbox contention.
- **Applies** GPU frequency changes through a safety envelope that refuses
  out-of-range values rather than clamping them, one small step per call.
- **Snapshots** the verbatim bytes of every config file before each write, and
  rolls back on demand.
- **Reverts automatically** on the next boot if a config was applied and never
  proven stable — the piece that makes unattended tuning survivable.
- **Benchmarks** with FurMark, sampling telemetry throughout and aborting on an
  over-temperature.

## Supported governors

A BC-250 runs exactly one GPU governor; both in the wild are supported, and the
backend is detected automatically.

| | `oberon-governor` | `cyan-skillfish-governor-smu` |
|---|---|---|
| Operating points | exactly two | full V/F curve |
| Control | config file + restart | **live D-Bus** |
| Survives reboot | yes | no (config file does) |

The D-Bus path is preferred where available: changes are volatile, so a power
cycle is a guaranteed rollback.

## Status

| Phase | State |
|---|---|
| 0 — Recon | done, see [docs/UPSTREAM_INTERFACES.md](docs/UPSTREAM_INTERFACES.md) |
| 1 — Read-only MCP server | done |
| 2 — Guarded writes + watchdog | done, revert verified across a reboot |
| 3 — Benchmark harness | done |
| 4 — Optimizer skill | done, see [skill/bc250-optimizer](skill/bc250-optimizer/SKILL.md) |
| 5 — Agent guardrails | folded into phases 2–3 |
| 6 — README generation | done (this section is generated) |

## Benchmarks

Two 120 s FurMark passes at 1920x1080 on the reference unit, each started from
a cooled GPU (60 °C gate) so the pair is comparable.

**Stock** is oberon's packaged default, `1000–2000 MHz @ 1000 mV`. **Tuned** is
a *lower* clock ceiling at a *lower* voltage, `1000–1600 MHz @ 875 mV` — an
undervolt, not an overclock.

The counter-intuitive result is that the undervolt wins on every metric at
once: faster, cooler, and less power. On a board this thermally constrained
1000 mV generates more heat than the cooler can shed, so the SMU throttles hard
and the *sustained* clock settles below what the cooler 875 mV config holds
comfortably. Stock also peaked at 96.5 °C, within 0.5 °C of the harness abort
threshold.

**Less voltage bought more performance here. Raising limits did not** — a
separate test stepping the ceiling 1600 → 1650 MHz produced no gain at all.

<!-- BENCHMARKS:START -->

| Metric | Stock | Tuned | Change |
|---|---:|---:|---|
| GPU range | 1000–2000 MHz | 1000–1600 MHz | |
| GPU voltage | 1000 mV | 875 mV | |
| Started at | 60.0 °C | 59.2 °C | |
| Score (frames) | 9362 | 11209 | ▲ +1847.0 (+19.7%) ✓ |
| Avg FPS | 78.0 | 95.0 | ▲ +17.0 (+21.8%) ✓ |
| Min FPS | 76.0 | 76.0 | — +0.0 (+0.0%) |
| Avg GPU temp | 91.8 °C | 87.1 °C | ▼ -4.6 °C (-5.0%) ✓ |
| Max GPU temp | 96.5 °C | 94.8 °C | ▼ -1.8 °C (-1.8%) ✓ |
| Avg power | 122.8 W | 111.4 W | ▼ -11.4 W (-9.3%) ✓ |

Efficiency: **0.635 → 0.853 FPS/W** (+34.3%).

| Stock | Tuned |
|---|---|
| ![Stock](benchmarks/raw/20260910-144633-stock-final/screenshot.jpg) | ![Tuned](benchmarks/raw/20260910-144925-tuned-final/screenshot.jpg) |

<details>
<summary>All completed runs</summary>

| Run | Config | GPU range | Start | Frames | Avg FPS | Max temp | Avg power |
|---|---|---|---:|---:|---:|---:|---:|
| 2026-09-10T00:40:22-0700 | oc-1600 | 1000–1600 MHz | 56.0 °C | 12199 | 102.0 | 95.0 °C | 117.6 W |
| 2026-09-10T00:44:50-0700 | oc-1650 | 1000–1650 MHz | 55.8 °C | 11979 | 101.0 | 94.8 °C | 116.4 W |
| 2026-09-10T00:48:49-0700 | oc-1600 | 1000–1600 MHz | 55.8 °C | 12261 | 103.0 | 94.8 °C | 117.9 W |
| 2026-09-10T14:40:48-0700 | stock-2000-1000mv | 1000–2000 MHz | 57.2 °C | 9385 | 78.0 | 96.8 °C | 122.6 W |
| 2026-09-10T14:43:33-0700 | oc-1600-875mv | 1000–1600 MHz | 62.2 °C | 10843 | 92.0 | 94.8 °C | 109.2 W |
| 2026-09-10T14:46:33-0700 | stock-final | 1000–2000 MHz | 60.0 °C | 9362 | 78.0 | 96.5 °C | 122.8 W |
| 2026-09-10T14:49:25-0700 | tuned-final | 1000–1600 MHz | 59.2 °C | 11209 | 95.0 | 94.8 °C | 111.4 W |

</details>

> **3 run(s) aborted on a thermal limit** and are excluded from the table above, since an aborted run produces no score. Most recent: GPU reached 92.25 C, at or above the 92.0 C abort threshold

> **What "stock" means here.** Only the *GPU governor configuration* is stock. Both runs were taken on a machine with the **40 CU unlock and the 8c/16t CPU core unlock already active** — 40 of 40 compute units (20 WGPs) applied by a BC-250-patched kernel via `amdgpu.bc250_cc_write_mode=3`, and 16 threads online against the factory 6c/12t.

> A stock-topology BC-250 (24 CU, 6c/12t) will score lower on **both** sides. These numbers isolate the effect of the governor configuration with the unlocks held constant; the unlocks themselves are a separate and almost certainly larger lever.

<sub>Generated by `benchmarks/scripts/generate_readme_benchmarks.py`. FurMark runs start from a cooled GPU (60 °C gate) so results are comparable; every row records the temperature it actually started at.</sub>

<!-- BENCHMARKS:END -->

## Layout

```
mcp_server/bc250_mcp/    MCP server: telemetry, governor, writes, benchmark
  safety_envelope.yaml   hard/safe bounds — the single source of truth
watchdog/                boot-time revert service (separate process, by design)
benchmarks/scripts/      standalone CLI wrappers
docs/                    recon, decisions, safety, target-machine profile
```

## Documentation

- [docs/SAFETY.md](docs/SAFETY.md) — what protects you, what does not
- [docs/UPSTREAM_INTERFACES.md](docs/UPSTREAM_INTERFACES.md) — every upstream tool's real interface, read from source
- [docs/DECISIONS.md](docs/DECISIONS.md) — design decisions and corrections to the original plan
- [docs/TARGET_MACHINE.md](docs/TARGET_MACHINE.md) — the test unit, and what it taught us
- [docs/THIRD_PARTY_NOTICES.md](docs/THIRD_PARTY_NOTICES.md) — credits

## License

MIT — see [LICENSE](LICENSE).
