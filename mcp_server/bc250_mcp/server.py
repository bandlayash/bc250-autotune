"""MCP entrypoint for BC-250 AutoTune (stdio transport).

Phase 2: read-only tools plus guarded writes. Every write is gated by
safety_envelope.yaml, limited to one step per call, and snapshotted to disk
before the hardware is touched so the boot watchdog can revert it.

Run with::

    python -m bc250_mcp.server

Every tool returns plain JSON-able dicts with a ``warnings`` list rather than
raising on absent hardware -- a BC-250 with no Super I/O driver loaded should
still give the agent the sensors it does have.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import (
    apply,
    benchmark,
    cpu_oc,
    cu_config,
    envelope,
    governor,
    snapshots,
    telemetry,
)

mcp = MCPServer("bc250-autotune")

# Phase 1 exposes nothing that mutates hardware. Advertising that explicitly
# lets a client surface these as safe to call without prompting.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)


def _dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")


@mcp.tool(annotations=READ_ONLY)
def get_telemetry() -> dict[str, Any]:
    """Read live sensors: GPU temp/power/clock/voltage, CPU temp/clocks, fan RPM.

    Read-only sysfs; no SMU access, so this is safe to poll during a benchmark
    without contending with the governor daemon. Any sensor the box does not
    provide comes back as null with an explanation in `warnings` -- notably
    `gpu_busy_percent`, which gfx1013 does not implement, and fan readings,
    which need the nct6686/nct6687 Super I/O driver loaded.
    """
    return telemetry.collect_dict()


@mcp.tool(annotations=READ_ONLY)
def get_gpu_governor_state() -> dict[str, Any]:
    """Report which GPU governor is in charge and how it is currently configured.

    Two governors exist for the BC-250 and a box runs exactly one:

    - `oberon`: two operating points from /etc/oberon-config.yaml, no IPC.
      Changes need a file rewrite plus a service restart and survive reboot.
    - `cyan-skillfish`: a full V/F curve plus a live D-Bus interface. Values set
      over D-Bus are volatile, so a power cycle reverts them.

    `supports_live_control` tells you which tuning strategy is available, and
    `persistence` tells you whether a change would survive a reboot.
    """
    return governor.get_state_dict()


@mcp.tool(annotations=READ_ONLY)
def get_gpu_curve() -> dict[str, Any]:
    """List the GPU voltage/frequency operating points the governor will use.

    This replaces the `list_gpu_safepoints` idea from the original build plan.
    There are no named safe-point tiers in either governor: cyan-skillfish
    stores an anonymous ordered frequency->voltage map that it interpolates
    across by load, and oberon has exactly two points. Tuning means moving the
    frequency range or editing curve points, not selecting a named tier.
    """
    state = governor.get_state()
    return {
        "backend": state.backend,
        "curve": [
            {"frequency_mhz": p.frequency_mhz, "voltage_mv": p.voltage_mv}
            for p in state.curve
        ],
        "point_count": len(state.curve),
        "current_range_mhz": {
            "min": state.min_freq_mhz,
            "max": state.max_freq_mhz,
        },
        "allowed_range_mhz": {
            "min": state.allowed_min_mhz,
            "max": state.allowed_max_mhz,
        },
        "supports_live_control": state.supports_live_control,
        "warnings": state.warnings,
    }


@mcp.tool(annotations=READ_ONLY)
def get_cpu_state() -> dict[str, Any]:
    """Report configured CPU overclock versus the stock baseline, and core count.

    Reads the config file and /proc only -- no SMU access, so live Vid is not
    included here. `cores_unlocked` reflects whether the two factory-harvested
    cores are online (8c/16t versus the stock 6c/12t).
    """
    return cpu_oc.get_state_dict()


@mcp.tool(annotations=READ_ONLY)
def get_cu_config() -> dict[str, Any]:
    """Report the GPU compute-unit topology: 24 CU factory versus 40 CU full.

    `mechanism` matters as much as the count. A BC-250-patched kernel applies
    this at probe time via `amdgpu.bc250_cc_write_mode` and it is persistent;
    bc250-cu-live-manager routes WGPs at runtime and is volatile unless its boot
    service is installed. Read-only: changing CU topology mid-session would
    invalidate every benchmark taken before it.
    """
    return cu_config.get_config_dict()


@mcp.tool(annotations=READ_ONLY)
def get_safety_envelope() -> dict[str, Any]:
    """Return the safety envelope: the hard and safe bounds on every tunable.

    Consult this before proposing any value. Values above a `safe_` bound need
    confirm=True and should be surfaced to the user; values beyond a `hard_`
    bound are refused outright and cannot be unlocked by any flag.
    """
    try:
        return envelope.summary()
    except envelope.EnvelopeError as exc:
        return {"error": str(exc), "loaded": False}


@mcp.tool(annotations=READ_ONLY)
def get_server_status() -> dict[str, Any]:
    """Report server mode and which capabilities this box actually supports.

    Call this first on a new box: it says whether writes would touch hardware
    (`dry_run`), which governor backend was detected, and which upstream tools
    are missing, so the agent can plan around gaps rather than discovering them
    mid-tune.
    """
    state = governor.get_state()
    cpu = cpu_oc.get_state()
    reading = telemetry.collect()

    return {
        "phase": "3 (benchmark harness)",
        "dry_run": _dry_run(),
        "writes_implemented": True,
        "governor_backend": state.backend,
        "governor_active": state.active,
        "supports_live_control": state.supports_live_control,
        "tools_available": cpu.tools_available,
        "sensors": {
            "gpu_temp": reading.gpu_temp_c is not None,
            "gpu_power": reading.gpu_power_w is not None,
            "gpu_clock": reading.gpu_clock_mhz is not None,
            "fan_rpm": bool(reading.fans),
            "cpu_temp": reading.cpu_temp_c is not None,
        },
        "warnings": sorted(set(state.warnings + cpu.warnings + reading.warnings)),
    }


# Phase 2 writes. Marked destructive so a client can prompt on them, and
# non-idempotent because each call steps the config rather than setting an
# absolute state the caller can safely repeat.
MUTATING = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)

# Rollback and snapshot are safe to repeat: both converge on a known state.
RECOVERY = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)


@mcp.tool(annotations=RECOVERY)
def snapshot_config(label: str) -> dict[str, Any]:
    """Capture the current tuning configuration so it can be restored later.

    Stores the verbatim contents of every relevant config file plus live state,
    under a location the boot watchdog can read as root. Take one before any
    tuning session; `set_gpu_range` also snapshots automatically before each
    write, so you rarely need to call this by hand mid-session.
    """
    try:
        return snapshots.capture_and_save(label)
    except snapshots.SnapshotError as exc:
        return {"label": label, "saved": False, "error": str(exc)}


@mcp.tool(annotations=READ_ONLY)
def list_snapshots() -> dict[str, Any]:
    """List saved configuration snapshots available to `rollback`."""
    return {"state_root": str(snapshots.state_root()), "snapshots": snapshots.list_snapshots()}


@mcp.tool(annotations=MUTATING)
def set_gpu_range(min_mhz: int, max_mhz: int, confirm: bool = False) -> dict[str, Any]:
    """Set the GPU frequency window. The primary GPU tuning knob.

    Guarded three ways, and none can be bypassed:

    - Values beyond a `hard_` bound in the safety envelope are refused outright,
      even with confirm=True.
    - Values above a `safe_` bound require confirm=True. Surface that to the
      user rather than auto-confirming.
    - No call may move the ceiling more than one step (default 50 MHz) from
      where it is now. Step gradually.

    A snapshot and a watchdog pending-marker are written to disk before the
    hardware is touched, so a config that hangs the box is reverted on the next
    boot. Call `mark_stable` once a config has proven itself, or `rollback` to
    undo it now.

    On oberon this rewrites /etc/oberon-config.yaml and restarts the daemon, and
    is persistent. On cyan-skillfish it uses D-Bus and is volatile.
    """
    return apply.set_gpu_range(min_mhz, max_mhz, confirm=confirm)


@mcp.tool(annotations=RECOVERY)
def rollback(to: str = "last_good", dry_run: bool = False) -> dict[str, Any]:
    """Restore a saved snapshot and clear the watchdog's pending marker.

    Never gated on confirm: getting back to a known-good state must not be
    harder than leaving it. Pass dry_run=True to see exactly what would change
    without changing it. Returns a per-step report; a partly-failed restore
    reports which steps failed rather than silently half-applying.
    """
    return apply.rollback(to, dry_run=dry_run)


@mcp.tool(annotations=RECOVERY)
def mark_stable() -> dict[str, Any]:
    """Promote the current config to `last_good` after it has proven stable.

    Refuses while the GPU is thermally throttling, since a config that is
    throttling right now has not proven anything. Until this is called, the
    watchdog treats the applied config as unproven and reverts it on the next
    boot.
    """
    return apply.mark_stable()


@mcp.tool(annotations=READ_ONLY)
def get_watchdog_status() -> dict[str, Any]:
    """Report whether a config is pending, and whether unattended revert works.

    `pending` non-null means a config was applied and has not been marked
    stable, so the watchdog will revert it on the next boot. Check
    `unattended_revert_ready` before starting an unattended tuning run.
    """
    root = snapshots.state_root()
    pending_path = root / "pending.json"
    pending: dict[str, Any] | None = None
    try:
        pending = json.loads(pending_path.read_text())
    except (OSError, ValueError):
        pending = None

    labels = [s["label"] for s in snapshots.list_snapshots()]
    unit_installed = Path("/etc/systemd/system/bc250-watchdog.service").exists()

    problems: list[str] = []
    if snapshots.LAST_GOOD_LABEL not in labels:
        problems.append(
            f"no {snapshots.LAST_GOOD_LABEL!r} snapshot; there is nothing to revert to"
        )
    if root != snapshots.SYSTEM_STATE_ROOT:
        problems.append(
            f"snapshots are in {root}, which the boot watchdog does not read"
        )
    if not unit_installed:
        problems.append("bc250-watchdog.service is not installed")

    return {
        "state_root": str(root),
        "pending": pending,
        "snapshots": labels,
        "watchdog_unit_installed": unit_installed,
        "unattended_revert_ready": not problems,
        "problems": problems,
    }


@mcp.tool(annotations=READ_ONLY)
def get_benchmark_environment() -> dict[str, Any]:
    """Check whether a benchmark can run right now, and report what is missing.

    Call this before `run_benchmark` on a new box. FurMark needs a reachable X
    display even when driven over SSH, plus `xrdb` (without it FurMark
    segfaults on a fresh XWayland display) and a screenshot tool.
    """
    return benchmark.environment_report()


@mcp.tool(annotations=MUTATING)
def run_benchmark(
    config_label: str,
    duration_s: int,
    width: int = 1920,
    height: int = 1080,
) -> dict[str, Any]:
    """Run a FurMark pass under the current config and record the result.

    Blocks for roughly `duration_s`. Loads the GPU to its limit, so only run
    this after applying a config through the guarded write tools -- every row
    is tied to a known, snapshotted configuration.

    Telemetry is sampled every second throughout and the run is **aborted** if
    the GPU crosses `benchmark.abort_gpu_temp_c` in the safety envelope. That
    ceiling matters on this hardware: the test unit reaches 90-95 C under
    FurMark while the SMU reports no throttle flags at all, so temperature is
    the only reliable stop signal.

    Returns the parsed result including `score_frames`, fps, temperature and
    power summaries, observed throttle flags, and a screenshot path. A row is
    appended to benchmarks/results.jsonl regardless of outcome, so an aborted
    run is recorded rather than lost.
    """
    try:
        return benchmark.run(
            config_label, duration_s, width=width, height=height
        ).to_dict()
    except (benchmark.BenchmarkError, envelope.EnvelopeError) as exc:
        return {"config_label": config_label, "completed": False, "error": str(exc)}


@mcp.tool(annotations=READ_ONLY)
def get_benchmark_result(run_id: str) -> dict[str, Any]:
    """Fetch a recorded benchmark result by run_id."""
    row = benchmark.get_result(run_id)
    if row is None:
        return {"run_id": run_id, "found": False}
    return {"found": True, **row}


@mcp.tool(annotations=READ_ONLY)
def list_benchmark_results(limit: int = 20) -> dict[str, Any]:
    """List recorded benchmark runs, most recent last.

    Use this to compare a tuned config against the `stock` baseline rather than
    relying on remembered numbers.
    """
    rows = benchmark.load_results()
    trimmed = rows[-limit:] if limit > 0 else rows
    return {
        "results_file": str(benchmark.results_path()),
        "total": len(rows),
        "results": [
            {
                key: row.get(key)
                for key in (
                    "run_id", "config_label", "iso_time", "duration_s",
                    "completed", "aborted", "score_frames", "avg_fps",
                    "avg_temp_c", "max_temp_c", "avg_power_w", "avg_clock_mhz",
                    "gpu_range_mhz", "thermally_throttled", "screenshot_path",
                )
            }
            for row in trimmed
        ],
    }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
