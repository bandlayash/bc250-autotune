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
| 4 — Optimizer skill | in progress |
| 5 — Agent guardrails | folded into phases 2–3 |
| 6 — README generation | done |

## Benchmarks

<!-- BENCHMARKS:START -->

_No completed benchmark runs recorded yet._

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
