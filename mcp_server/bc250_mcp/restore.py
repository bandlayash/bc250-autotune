"""Snapshot restore -- the rollback half of Phase 2.

Deliberately separate from ``snapshots.py`` so that capturing (pure reads) and
restoring (privileged writes) cannot be confused for one another at a glance,
and so the watchdog can import this one module without pulling in the MCP
server.

Restore is designed to be **survivable when partly failed**. Each step is
independent and reports its own outcome; one failing step does not abort the
rest, because a half-restored config that got the governor back to safe values
is better than one that gave up before touching the governor at all. The caller
receives a per-step report and decides what to do.

Ordering matters and is not arbitrary:

1. Files first, so a later service restart picks up restored contents.
2. Governor live state next, since on cyan-skillfish the running daemon holds
   values that no file restore would correct.
3. Fans last, and always -- if everything else failed, the fan should still end
   up back under whatever control it was under before.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import governor, snapshots, sysfs
from .snapshots import Snapshot

_TIMEOUT = 30


@dataclass
class StepResult:
    step: str
    ok: bool
    detail: str
    changed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RestoreReport:
    label: str
    dry_run: bool
    steps: list[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps)

    @property
    def changed(self) -> bool:
        return any(step.changed for step in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "dry_run": self.dry_run,
            "ok": self.ok,
            "changed": self.changed,
            "failed_steps": [s.step for s in self.steps if not s.ok],
            "steps": [s.to_dict() for s in self.steps],
        }


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _is_root() -> bool:
    """True when running as uid 0.

    ``os.geteuid`` is POSIX-only. The product targets Linux, but the test suite
    is meant to run anywhere with no hardware -- including CI and a Windows dev
    box -- so fall back to "not root" rather than raising on import.
    """
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


def _sudo(cmd: list[str]) -> list[str]:
    """Prefix with non-interactive sudo when not already root.

    ``-n`` so a missing sudo rule fails immediately rather than blocking on a
    password prompt that nothing will ever answer -- this runs unattended.
    """
    return cmd if _is_root() else ["sudo", "-n", *cmd]


def _write_privileged(path: Path, content: str, mode: int | None) -> tuple[bool, str]:
    """Write a root-owned config file atomically.

    Written to a temp file then moved into place with ``install``, so a reader
    (or a daemon restarting at the wrong moment) never observes a partial file.
    """
    try:
        with tempfile.NamedTemporaryFile(
            "w", delete=False, prefix="bc250-restore-", suffix=".tmp"
        ) as handle:
            handle.write(content)
            temp_path = handle.name
    except OSError as exc:
        return False, f"cannot stage temp file: {exc}"

    try:
        octal = f"{mode:o}" if mode is not None else "644"
        rc, out = _run(_sudo(["install", "-m", octal, temp_path, str(path)]))
        if rc != 0:
            return False, f"install failed: {out}"
        return True, f"restored {path} ({len(content)} bytes, mode {octal})"
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def _restore_files(snap: Snapshot, dry_run: bool) -> list[StepResult]:
    results: list[StepResult] = []
    for capture in snap.files:
        path = Path(capture.path)
        step = f"file:{path.name}"

        if not capture.existed:
            # The file did not exist when the snapshot was taken. If something
            # created it since, removing it is what "restore" means -- leaving
            # it would keep a config the snapshot says was not there.
            if not path.exists():
                results.append(StepResult(step, True, f"{path} absent, as captured"))
                continue
            if dry_run:
                results.append(
                    StepResult(step, True, f"WOULD REMOVE {path} (absent at capture)")
                )
                continue
            rc, out = _run(_sudo(["rm", "-f", str(path)]))
            results.append(
                StepResult(step, rc == 0, out or f"removed {path}", changed=rc == 0)
            )
            continue

        current = None
        try:
            current = path.read_text()
        except OSError:
            pass

        if current == capture.content:
            results.append(StepResult(step, True, f"{path} already matches snapshot"))
            continue

        if dry_run:
            results.append(StepResult(step, True, f"WOULD RESTORE {path}"))
            continue

        ok, detail = _write_privileged(path, capture.content or "", capture.mode)
        results.append(StepResult(step, ok, detail, changed=ok))
    return results


def _restore_units(
    snap: Snapshot, dry_run: bool, files_changed: bool
) -> list[StepResult]:
    """Restart units whose config we just rewrote, and re-apply enabled state.

    Only units that were active at capture are restarted. Starting a unit that
    was deliberately stopped would be a change, not a restoration -- and with
    two mutually-exclusive governors, starting the wrong one is actively
    harmful.

    An active unit is restarted only when a config file actually changed.
    Restarting otherwise is pure churn: the governor drops out for its
    ``RestartSec`` window and re-applies the same values it already held.
    """
    results: list[StepResult] = []
    for unit, state in snap.units.items():
        was_active = state.get("active") == "active"
        _, now_active_raw = _run(["systemctl", "is-active", unit])
        now_active = now_active_raw.strip() == "active"

        if was_active and not now_active:
            action, verb = "start", "start"
        elif not was_active and now_active:
            action, verb = "stop", "stop"
        elif was_active and files_changed:
            action, verb = "restart", "restart (to reload restored config)"
        elif was_active:
            results.append(
                StepResult(
                    f"unit:{unit}",
                    True,
                    f"{unit} active and config unchanged; no restart needed",
                )
            )
            continue
        else:
            results.append(
                StepResult(f"unit:{unit}", True, f"{unit} inactive, as captured")
            )
            continue

        if dry_run:
            results.append(StepResult(f"unit:{unit}", True, f"WOULD {verb} {unit}"))
            continue

        rc, out = _run(_sudo(["systemctl", action, unit]))
        results.append(
            StepResult(
                f"unit:{unit}", rc == 0, out or f"{action} {unit}", changed=rc == 0
            )
        )
    return results


def _restore_governor_live(snap: Snapshot, dry_run: bool) -> list[StepResult]:
    """Re-apply the live D-Bus range on cyan-skillfish.

    Only meaningful for that backend. oberon has no IPC, so its state is fully
    described by the config file plus the service restart already performed.
    """
    step = "governor:live-range"
    if snap.governor_backend != "cyan-skillfish":
        return [
            StepResult(
                step, True, f"backend is {snap.governor_backend}; no live state to restore"
            )
        ]

    wanted = snap.governor_live_range_mhz or {}
    minimum, maximum = wanted.get("min"), wanted.get("max")
    if minimum is None or maximum is None:
        return [StepResult(step, True, "no live range captured; nothing to restore")]

    if dry_run:
        return [StepResult(step, True, f"WOULD SetRange({minimum}, {maximum})")]

    if shutil.which("busctl") is None:
        return [StepResult(step, False, "busctl not available; cannot restore live range")]

    rc, out = _run(
        [
            "busctl", "call", governor.DBUS_NAME, governor.DBUS_ROOT,
            governor.DBUS_PERF_IFACE, "SetRange", "uu", str(minimum), str(maximum),
        ]
    )
    return [
        StepResult(
            step, rc == 0, out or f"SetRange({minimum}, {maximum})", changed=rc == 0
        )
    ]


def _restore_fans(snap: Snapshot, dry_run: bool) -> list[StepResult]:
    """Restore PWM duty and, more importantly, control mode.

    ``pwm_enable`` is written before ``pwm``: switching mode can reset the duty,
    so setting duty first would have it immediately overwritten.
    """
    results: list[StepResult] = []
    if not snap.fans:
        return [StepResult("fans", True, "no fan channels captured")]

    superio = sysfs.find_superio_hwmon()
    if superio is None:
        return [
            StepResult("fans", False, "Super I/O hwmon not present; cannot restore fans")
        ]

    for fan in snap.fans:
        step = f"fan:pwm{fan.channel}"

        if not fan.writable:
            # Read-only PWM (in-tree nct6683). Nothing could have changed it, so
            # there is nothing to roll back -- report that rather than failing.
            results.append(
                StepResult(
                    step,
                    True,
                    f"pwm{fan.channel} is read-only (in-tree nct6683 exposes no "
                    "fan control); nothing to restore",
                )
            )
            continue

        writes: list[tuple[Path, int]] = []
        if fan.enable is not None:
            writes.append((superio / f"pwm{fan.channel}_enable", fan.enable))
        if fan.pwm is not None:
            writes.append((superio / f"pwm{fan.channel}", fan.pwm))

        if not writes:
            results.append(StepResult(step, True, "nothing captured for this channel"))
            continue

        if dry_run:
            # Compare against live values so a preview does not claim writes it
            # would not actually perform. This report is what an operator reads
            # before authorising a rollback; overstating it erodes its value.
            pending = [(p, v) for p, v in writes if sysfs.read_int(p) != v]
            if not pending:
                results.append(
                    StepResult(step, True, f"pwm{fan.channel} already matches snapshot")
                )
                continue
            detail = ", ".join(f"{p.name}={v}" for p, v in pending)
            results.append(StepResult(step, True, f"WOULD WRITE {detail}"))
            continue

        ok, details, changed = True, [], False
        for path, value in writes:
            if sysfs.read_int(path) == value:
                details.append(f"{path.name} already {value}")
                continue
            succeeded, detail = _write_sysfs(path, value)
            ok = ok and succeeded
            changed = changed or succeeded
            details.append(detail)
        results.append(StepResult(step, ok, "; ".join(details), changed=changed))
    return results


def _write_sysfs(path: Path, value: int) -> tuple[bool, str]:
    """Write an integer to a root-owned sysfs attribute."""
    try:
        with open(path, "w") as handle:
            handle.write(str(value))
        return True, f"{path.name}={value}"
    except OSError:
        pass

    rc, out = _run(_sudo(["sh", "-c", f"printf '%s' {value} > {path}"]))
    if rc != 0:
        return False, f"{path.name}: {out or 'write failed'}"
    return True, f"{path.name}={value}"


def restore(
    label: str = snapshots.LAST_GOOD_LABEL,
    dry_run: bool = False,
    restart_units: bool = True,
) -> RestoreReport:
    """Restore a saved snapshot. Returns a per-step report; never raises on
    a step failure, only on the snapshot itself being unusable.

    ``restart_units=False`` is for the **boot** path, and is not an optimisation
    -- it avoids a systemd deadlock. The watchdog unit is ordered
    ``Before=oberon-governor.service``, so calling ``systemctl restart
    oberon-governor`` from inside it blocks forever: the restart waits for the
    watchdog to finish, and the watchdog waits for the restart. Observed live --
    the watchdog hung in ``activating`` and the governor never started at all.

    Skipping restarts at boot is correct as well as necessary: the governors
    have not started yet, so they read the restored file when they do. Restarts
    are only needed for a rollback performed while the system is already up.
    """
    snap = snapshots.load(label)
    report = RestoreReport(label=label, dry_run=dry_run)

    file_steps = _restore_files(snap, dry_run)
    report.steps.extend(file_steps)

    # In a dry run nothing is marked changed, so infer intent from the report
    # text; otherwise a preview would always claim "no restart needed".
    files_changed = any(s.changed for s in file_steps) or (
        dry_run and any("WOULD" in s.detail for s in file_steps)
    )

    if restart_units:
        report.steps.extend(_restore_units(snap, dry_run, files_changed))
        report.steps.extend(_restore_governor_live(snap, dry_run))
    else:
        report.steps.append(
            StepResult(
                "units",
                True,
                "skipped at boot: services start after this unit and will read "
                "the restored config themselves (restarting here deadlocks)",
            )
        )
        # The governor daemon is not up yet, so there is no D-Bus name to call.
        # It will read the restored config.toml when it starts, which reaches
        # the same end state.
        report.steps.append(
            StepResult(
                "governor:live-range",
                True,
                "skipped at boot: the governor daemon has not started, so it "
                "will pick up the restored config file directly",
            )
        )
    report.steps.extend(_restore_fans(snap, dry_run))
    return report


def restore_dict(
    label: str = snapshots.LAST_GOOD_LABEL, dry_run: bool = False
) -> dict[str, Any]:
    try:
        return restore(label, dry_run).to_dict()
    except snapshots.SnapshotError as exc:
        return {"label": label, "ok": False, "error": str(exc), "steps": []}
