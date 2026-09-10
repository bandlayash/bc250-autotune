"""Guarded writes -- the only module in the package that changes tuning state.

Every write goes through the same gate, in this order, and no path skips it:

1. **Envelope check.** Values beyond a hard bound are refused outright; values
   above a safe bound require ``confirm=True``. Out-of-range values are never
   silently clamped -- a clamp would leave the caller believing it applied one
   config while the hardware ran another, and every benchmark afterwards would
   be attributed to the wrong settings.
2. **Step-size check.** No single call may move more than one increment. Small
   steps are what make an unattended search recoverable.
3. **Snapshot + pending marker**, written and fsynced to disk *before* the
   hardware is touched, so a box that dies mid-apply still has a marker telling
   the watchdog to revert on the next boot.
4. **Apply**, then report what actually happened.

``DRY_RUN=1`` short-circuits step 4 while still exercising 1-3, so the whole
loop -- including the optimizer skill driving it -- can be tested without
touching hardware.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import envelope, governor, restore, snapshots, telemetry
from .envelope import Check, Verdict

_TIMEOUT = 30


def dry_run_enabled() -> bool:
    return os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class ApplyResult:
    action: str
    applied: bool
    dry_run: bool
    detail: str = ""
    checks: list[dict[str, Any]] = field(default_factory=list)
    requires_confirmation: bool = False
    refused: bool = False
    snapshot: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "applied": self.applied,
            "dry_run": self.dry_run,
            "detail": self.detail,
            "checks": self.checks,
            "requires_confirmation": self.requires_confirmation,
            "refused": self.refused,
            "snapshot": self.snapshot,
            "warnings": self.warnings,
        }


def _gate(action: str, checks: list[Check], confirm: bool) -> ApplyResult | None:
    """Return a blocking result if the envelope forbids this, else None."""
    worst = envelope.worst(checks)
    payload = [c.to_dict() for c in checks]

    if worst.verdict is Verdict.REFUSED:
        return ApplyResult(
            action=action,
            applied=False,
            dry_run=dry_run_enabled(),
            detail=worst.reason,
            checks=payload,
            refused=True,
        )

    if worst.verdict is Verdict.CONFIRM and not confirm:
        return ApplyResult(
            action=action,
            applied=False,
            dry_run=dry_run_enabled(),
            detail=(
                f"{worst.reason}. Surface this to the user and re-call with "
                "confirm=True if they approve."
            ),
            checks=payload,
            requires_confirmation=True,
        )
    return None


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _prepare(action: str, description: str, doc: dict[str, Any]) -> tuple[str, list[str]]:
    """Snapshot current state and mark it pending, before touching hardware.

    Returns the snapshot label and any warnings. Raises SnapshotError if the
    snapshot cannot be written -- a write with no way back must not proceed.
    """
    warnings: list[str] = []
    label = f"pre-{action}-{int(__import__('time').time())}"
    result = snapshots.capture_and_save(label, notes=[description])
    warnings.extend(result.get("warnings", []))

    stability = int(
        doc.get("optimizer", {}).get("stability_window_minutes", 10)
    )
    try:
        _write_pending(label, description, stability)
    except OSError as exc:
        warnings.append(
            f"could not write the watchdog pending marker ({exc}); an unattended "
            "revert will NOT happen if this config hangs the box"
        )
    return label, warnings


def _write_pending(label: str, description: str, stability_minutes: int) -> None:
    """Write the watchdog's pending marker.

    Imported lazily from the watchdog module so there is one implementation of
    the marker format, rather than a second copy here that could drift.
    """
    for candidate in (
        Path(__file__).resolve().parent.parent.parent / "watchdog",
        Path("/opt/bc250-autotune/watchdog"),
        Path.home() / "bc250-autotune/watchdog",
    ):
        if (candidate / "watchdog.py").exists():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            import watchdog

            watchdog.write_pending(label, description, stability_minutes)
            return
    raise OSError("watchdog.py not found; cannot write pending marker")


# --------------------------------------------------------------------------
# GPU governor
# --------------------------------------------------------------------------

def set_gpu_range(
    min_mhz: int, max_mhz: int, confirm: bool = False
) -> dict[str, Any]:
    """Set the GPU frequency window. The main GPU tuning knob."""
    action = "set_gpu_range"
    doc = envelope.load()
    state = governor.get_state()

    if min_mhz > max_mhz:
        return ApplyResult(
            action, False, dry_run_enabled(),
            detail=f"min ({min_mhz}) exceeds max ({max_mhz})",
            refused=True,
        ).to_dict()

    checks = [
        envelope.check_upper(
            "max_frequency_mhz", max_mhz, section="gpu",
            hard_key="hard_max_frequency_mhz", safe_key="safe_max_frequency_mhz",
            doc=doc,
        ),
        envelope.check_lower(
            "min_frequency_mhz", min_mhz, section="gpu",
            hard_key="hard_min_frequency_mhz", doc=doc,
        ),
    ]

    # One step per call. The current ceiling is the reference point.
    step = int(doc.get("optimizer", {}).get("gpu_frequency_step_mhz", 50))
    current_max = state.max_freq_mhz
    if current_max is not None and abs(max_mhz - current_max) > step:
        return ApplyResult(
            action, False, dry_run_enabled(),
            detail=(
                f"requested max {max_mhz} MHz moves {abs(max_mhz - current_max)} MHz "
                f"from the current {current_max} MHz; the limit is one "
                f"{step} MHz step per call. Step there gradually."
            ),
            checks=[c.to_dict() for c in checks],
            refused=True,
        ).to_dict()

    blocked = _gate(action, checks, confirm)
    if blocked:
        return blocked.to_dict()

    description = f"set GPU range to {min_mhz}-{max_mhz} MHz ({state.backend})"
    try:
        label, warnings = _prepare(action, description, doc)
    except snapshots.SnapshotError as exc:
        return ApplyResult(
            action, False, dry_run_enabled(),
            detail=f"refusing to apply: cannot snapshot current state ({exc})",
            refused=True,
        ).to_dict()

    result = ApplyResult(
        action=action, applied=False, dry_run=dry_run_enabled(),
        checks=[c.to_dict() for c in checks], snapshot=label, warnings=warnings,
    )

    if result.dry_run:
        result.detail = f"DRY RUN: would {description}"
        return result.to_dict()

    if state.backend == "cyan-skillfish" and state.supports_live_control:
        ok, detail = _set_range_cyan(min_mhz, max_mhz)
    elif state.backend == "oberon":
        ok, detail = _set_range_oberon(min_mhz, max_mhz, state)
    else:
        ok, detail = False, f"no writable governor backend (detected {state.backend!r})"

    result.applied = ok
    result.detail = detail
    if not ok:
        result.warnings.append(
            "apply failed; the pending marker is still set, so the watchdog will "
            "revert on the next boot. Call rollback() to revert now."
        )
    return result.to_dict()


def _set_range_cyan(min_mhz: int, max_mhz: int) -> tuple[bool, str]:
    """Set the range live over D-Bus. Volatile, so a power cycle undoes it."""
    if shutil.which("busctl") is None:
        return False, "busctl not available"
    rc, out = _run([
        "busctl", "call", governor.DBUS_NAME, governor.DBUS_ROOT,
        governor.DBUS_PERF_IFACE, "SetRange", "uu", str(min_mhz), str(max_mhz),
    ])
    if rc != 0:
        return False, f"SetRange failed: {out}"
    return True, (
        f"set live range {min_mhz}-{max_mhz} MHz over D-Bus (volatile; a power "
        "cycle reverts it)"
    )


def _set_range_oberon(
    min_mhz: int, max_mhz: int, state: governor.GovernorState
) -> tuple[bool, str]:
    """Rewrite /etc/oberon-config.yaml and restart the daemon.

    oberon has no IPC, so this is the only route -- and unlike the D-Bus path it
    is persistent, which is exactly why the watchdog marker matters more here.
    Voltages are carried over from the existing config unchanged: this call
    tunes frequency only.
    """
    if not state.curve:
        return False, "cannot read the current oberon config; refusing to rewrite it"

    min_mv = state.curve[0].voltage_mv
    max_mv = state.curve[-1].voltage_mv

    content = (
        "opps:\n"
        "  - frequency:\n"
        f"    - min: {min_mhz}\n"
        f"    - max: {max_mhz}\n"
        "  - voltage:\n"
        f"    - min: {min_mv}\n"
        f"    - max: {max_mv}\n"
    )

    ok, detail = restore._write_privileged(governor.OBERON_CONFIG, content, 0o644)
    if not ok:
        return False, detail

    rc, out = _run(restore._sudo(["systemctl", "restart", governor.OBERON_UNIT]))
    if rc != 0:
        return False, f"config written but restart failed: {out}"
    return True, (
        f"wrote {min_mhz}-{max_mhz} MHz at {min_mv}/{max_mv} mV to "
        f"{governor.OBERON_CONFIG} and restarted {governor.OBERON_UNIT} "
        "(persistent across reboot)"
    )


# --------------------------------------------------------------------------
# rollback
# --------------------------------------------------------------------------

def rollback(
    label: str = snapshots.LAST_GOOD_LABEL, dry_run: bool | None = None
) -> dict[str, Any]:
    """Restore a snapshot and clear the watchdog's pending marker.

    Always available and never gated on confirm: getting back to a known-good
    state must not be harder than leaving it.
    """
    effective_dry_run = dry_run_enabled() if dry_run is None else dry_run
    report = restore.restore_dict(label, dry_run=effective_dry_run)

    if report.get("ok") and not effective_dry_run:
        try:
            _clear_pending()
            report["pending_cleared"] = True
        except OSError:
            report["pending_cleared"] = False
    return report


def _clear_pending() -> None:
    for candidate in (
        Path(__file__).resolve().parent.parent.parent / "watchdog",
        Path("/opt/bc250-autotune/watchdog"),
        Path.home() / "bc250-autotune/watchdog",
    ):
        if (candidate / "watchdog.py").exists():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            import watchdog

            watchdog.clear_pending()
            return
    raise OSError("watchdog.py not found")


def mark_stable() -> dict[str, Any]:
    """Promote the pending config to last_good after it has proven itself."""
    current = telemetry.collect()
    if current.is_thermally_throttling:
        return {
            "promoted": False,
            "detail": (
                "refusing to mark stable while thermally throttling: "
                f"{current.thermal_throttle_flags}"
            ),
        }

    try:
        result = snapshots.capture_and_save(
            snapshots.LAST_GOOD_LABEL,
            notes=["Marked stable via mark_stable()."],
        )
        _clear_pending()
    except (snapshots.SnapshotError, OSError) as exc:
        return {"promoted": False, "detail": str(exc)}

    return {"promoted": True, "detail": "promoted current config to last_good", **result}
