"""MCP entrypoint for BC-250 AutoTune (stdio transport).

Phase 1: read-only. Nothing here writes to hardware, so it is safe to point a
client at this server on a machine you care about. Guarded writes arrive in
Phase 2 behind the safety envelope and an explicit confirm flag.

Run with::

    python -m bc250_mcp.server

Every tool returns plain JSON-able dicts with a ``warnings`` list rather than
raising on absent hardware -- a BC-250 with no Super I/O driver loaded should
still give the agent the sensors it does have.
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import cpu_oc, cu_config, envelope, governor, telemetry

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
        "phase": "1 (read-only)",
        "dry_run": _dry_run(),
        "writes_implemented": False,
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


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
