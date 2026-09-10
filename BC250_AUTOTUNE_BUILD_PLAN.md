# BC-250 AutoTune — Build Plan

**Handoff doc for a Claude Code session.** Goal: an MCP server exposing BC-250 telemetry/tuning controls, plus an optimizer skill that uses it to auto-tune a unit, with before/after FurMark screenshots captured automatically for the README.

**Environment requirement (read first):** this must be built and run on a machine with a real BC-250 physically attached, running as a normal local process (not a sandboxed container) — it needs bash access to sysfs, systemd, and the display session for screenshots. Do not attempt to mock the whole stack and call it done; Phase 0 exists specifically to get real hardware into the loop early.

---

## 0. Repo layout

```
bc250-autotune/
├── README.md                          # gets an auto-generated benchmark section
├── LICENSE                            # MIT, matches upstream ecosystem
├── docs/
│   ├── SAFETY.md
│   └── THIRD_PARTY_NOTICES.md         # credit the 5 upstream tools + FurMark
├── mcp_server/
│   ├── pyproject.toml
│   ├── bc250_mcp/
│   │   ├── server.py                  # MCP entrypoint, stdio transport
│   │   ├── telemetry.py               # read-only sensor reads
│   │   ├── governor.py                # cyan-skillfish-governor TOML safe-points
│   │   ├── cpu_oc.py
│   │   ├── fan.py                     # nct6687d PWM
│   │   ├── cu_config.py               # bc250-cu-live-manager wrapper
│   │   ├── snapshots.py               # config save/rollback
│   │   └── safety_envelope.yaml       # hard ceilings, user-editable
│   └── tests/                         # mocked sysfs, no hardware needed
├── watchdog/
│   ├── bc250-watchdog.service         # systemd unit, survives MCP process death
│   └── watchdog.py
├── benchmarks/
│   ├── scripts/
│   │   ├── run_benchmark.sh
│   │   ├── capture_screenshot.sh
│   │   └── generate_readme_benchmarks.py
│   ├── raw/                           # gitignored; a few curated examples kept
│   └── results.jsonl
└── skill/
    └── bc250-optimizer/
        └── SKILL.md
```

---

## Phase 0 — Recon (do this before writing code)

- Clone and read the actual interfaces of the five upstream tools referenced in `bc250-control-center`'s README: `cyan-skillfish-governor` (smu branch — TOML safe-point format), `bc250_smu_oc`, `bc250-cu-live-manager` (+ SteamOS variant), `bc250-40cu-unlock`, `nct6687d`. Document each tool's CLI flags / config paths / sysfs paths in `docs/UPSTREAM_INTERFACES.md`.
- Confirm which distro you're building/testing on first (Bazzite/Fedora Atomic has different install mechanics than Arch/Ubuntu — reuse `bc250-control-center`'s `mvc/Repository/Os_repository/` distro-detection logic rather than reinventing it).
- Install FurMark 2 on the test machine (native Linux binary `furmark`, plus optional `FurMark_GUI`). Verify it runs and produces a score.
- **Definition of done:** you can, by hand, read GPU temp/power/clock from the box, apply one governor safe-point, and run a 30-second FurMark pass — all via terminal commands, no GUI.

---

## Phase 1 — MCP server: read-only

Tools to implement (Python, `mcp` SDK, stdio transport):

| Tool | Returns |
|---|---|
| `get_telemetry()` | temps, clocks, power draw, fan RPM, timestamp |
| `get_gpu_governor_state()` | current safe-point name + raw values |
| `get_cpu_state()` | current OC value + stock baseline |
| `get_cu_config()` | current 24CU/40CU state |
| `list_gpu_safepoints()` | available TOML safe-points with names/descriptions |

No writes in this phase. Ship it as `v0.1` — it's useful standalone and lets you validate the transport (test with Claude Code / Claude Desktop config pointing at the stdio server) before anything destructive exists.

**Definition of done:** Claude can read live telemetry from the box through the MCP tool, matching what the terminal commands from Phase 0 show.

---

## Phase 2 — MCP server: guarded writes + watchdog

Add:

| Tool | Behavior |
|---|---|
| `apply_gpu_safepoint(name, confirm=False)` | only accepts names from `list_gpu_safepoints()`; anything above the "safe" tier in `safety_envelope.yaml` requires `confirm=True` |
| `apply_cpu_oc(value, confirm=False)` | clamped to `safety_envelope.yaml` ceiling regardless of `confirm` |
| `apply_fan_curve(curve, confirm=False)` | list of (temp, pwm%) points |
| `snapshot_config(label)` | writes current full config to `snapshots/<label>.json` |
| `rollback(to="last_good")` | reapplies a saved snapshot |

**`safety_envelope.yaml`** is the single source of truth for hard ceilings (max freq, min voltage, max fan-off temp, etc.) — the MCP server refuses to exceed it no matter what the caller passes. Default it conservative; the user tightens/loosens it, not the agent.

**Watchdog (`watchdog/watchdog.py`, runs as a systemd service, separate process from the MCP server):**
- On every applied config, the MCP server writes a heartbeat/state file to disk (not just in-memory — the whole point is surviving a hang).
- Watchdog marks a config "stable" after N minutes without a crash/hang signal.
- On boot, if the last session's config was never marked stable, watchdog auto-reverts to `last_good` before anything else starts. This is the piece that makes unattended tuning safe — build and test it before Phase 4.

**Definition of done:** you can force a hang (e.g., apply an intentionally bad test value gated behind a `--i-know-this-may-hang` test flag, never exposed to the agent) and confirm the watchdog reverts on reboot without manual intervention.

---

## Phase 3 — Benchmark harness (FurMark)

FurMark 2's Linux build is a CLI tool (`furmark`) with a separate optional GUI (`FurMark_GUI`). Confirmed flags to use:

```bash
furmark --demo furmark-gl \
  --width 1920 --height 1080 \
  --max-time <duration_ms> \
  --log-gpu-data \
  --export-dir benchmarks/raw/<run_id>/ \
  --hw-polling-interval 500
```

- `--log-gpu-data` writes a CSV with clocks/temp/power/fps over the run into `--export-dir` — this is your ground-truth numeric data, not the screenshot.
- Leave the on-screen score box enabled (don't pass `--no-score-box`) so it's visible for the screenshot.
- `--x11-display-name` exists if you need to target a specific display/screen.

**`benchmarks/scripts/run_benchmark.sh <config_label> <duration_s>`:**
1. Generate a `run_id` (timestamp + config_label).
2. Launch `furmark` as above in the background.
3. Sleep until ~1s before `--max-time` elapses, then call `capture_screenshot.sh` to grab the score box: use `grim` on Wayland or `scrot`/`import` on X11 (detect via `$XDG_SESSION_TYPE`), saved to `benchmarks/raw/<run_id>/screenshot.png`.
4. Wait for FurMark to exit, parse the exported CSV, compute summary stats (avg/min/max fps, avg/max temp, avg power, score).
5. Append one line to `benchmarks/results.jsonl`:
   ```json
   {"run_id": "...", "timestamp": "...", "config_label": "stock", "duration_s": 60,
    "avg_fps": 0, "score": 0, "avg_temp_c": 0, "max_temp_c": 0, "avg_power_w": 0,
    "screenshot_path": "benchmarks/raw/.../screenshot.png"}
   ```

Expose this as an MCP tool too: `run_benchmark(config_label, duration_s)` → `run_id`, `get_benchmark_result(run_id)` → the parsed summary. Only ever run this *after* a config has been applied through the guarded-write tools, so every benchmark row is tied to a known, snapshotted config.

**Definition of done:** running the script against stock settings and against one manually-changed safe-point produces two rows in `results.jsonl` with two screenshots, and the numbers in the screenshot match the CSV-derived summary.

---

## Phase 4 — Optimizer skill

`skill/bc250-optimizer/SKILL.md` — instructions, no new code, using only Phase 1–3 tools:

1. Read baseline telemetry. Ask the user their objective (max performance / perf-per-watt / quieter+cooler) if not stated.
2. `snapshot_config("stock")`, run a baseline benchmark, label it in `results.jsonl` as `stock`.
3. Step from the current safe-point toward the next more aggressive one (small, defined increments — do not jump tiers).
4. After each change: `snapshot_config`, `run_benchmark` (short duration, e.g. 60–120s stress pass), check for thermal/stability red flags in telemetry.
5. On any failure signal (temp over threshold, watchdog didn't mark stable, benchmark crashed): `rollback("last_good")`, record the boundary you just found, and don't retry that tier again this session.
6. On success, keep stepping until failure or until you hit the `safety_envelope.yaml` ceiling.
7. Once a boundary is found, back off one increment as the final recommendation and run one longer confirmation benchmark (e.g. 10 minutes) before calling it done.
8. Run `generate_readme_benchmarks.py` (Phase 6) to update the README with the stock-vs-tuned comparison.
9. Report a plain-language summary: what changed, the perf delta, and where the stability wall was.

**Definition of done:** running the skill end-to-end on the test unit produces a tuned config plus a `results.jsonl` with at least a `stock` row and a final tuned row, no manual intervention beyond the initial objective question.

---

## Phase 5 — Agent-side guardrails (cuts across everything above)

- Small step sizes only; no tool lets the agent jump more than one tier per call.
- Anything past the "safe" tier requires `confirm=True`, and the skill instructions should have Claude surface that to the user rather than auto-confirming, at least for the first run on a new unit.
- A dry-run mode (`DRY_RUN=1` env var on the MCP server) that logs intended actions without touching hardware, for testing the whole loop safely.
- Every applied config and every benchmark run is logged to disk before anything else happens — nothing lives only in the agent's context.

---

## Phase 6 — README benchmark generation

`benchmarks/scripts/generate_readme_benchmarks.py`:
- Reads `results.jsonl`, finds the `stock` row and the latest tuned row per session.
- Writes a markdown table (score, avg fps, avg/max temp, avg power) plus the two screenshots side by side.
- Injects it into `README.md` between `<!-- BENCHMARKS:START -->` / `<!-- BENCHMARKS:END -->` markers — idempotent, safe to rerun after every tuning session so the README always reflects the latest run.

---

## Repo hygiene

- MIT license, matching the upstream project's convention.
- `docs/THIRD_PARTY_NOTICES.md` crediting all five upstream BC-250 tools and FurMark (proprietary freeware — don't bundle it, just document the install step).
- `docs/SAFETY.md` — restate the upstream project's "every unit is different, you're responsible" language; this applies at least as much to an automated tuner as to a manual GUI.
- GitHub Actions CI: lint + unit tests against **mocked** sysfs/CLI calls only. There is no hardware in CI — don't let Claude Code try to build hardware-in-the-loop tests there.
- Consider pinging the `bc250-control-center` maintainer before/at publish — they may have per-revision safe-range knowledge worth folding into `safety_envelope.yaml`'s defaults.

---

## Open questions to answer before starting the Claude Code session

1. First-target distro (affects Phase 0 install steps)?
2. Default optimization objective if the user doesn't specify (perf, perf/watt, or quiet)?
3. Repo name/visibility, and license (defaulting to MIT above — confirm)?
4. How strict should the default `confirm=True` gate be for a first public release — require it for every write, or only above the safe tier?
