#!/usr/bin/env python3
"""BC-250 AutoTune boot watchdog.

Runs as a systemd service, in a **separate process from the MCP server**, which
is the entire point: if the server hangs or the box locks up mid-tune, this
still runs on the next boot and puts the machine back to ``last_good``.

The contract is deliberately small and file-based, because in-memory state is
exactly what a hang destroys:

``pending.json``  written by the MCP server *before* it applies a config.
                  Its existence means "a config was applied and has not yet
                  been proven stable".
``last_good.json`` the snapshot to fall back to.

Flow:

*On boot* (``--boot``)
    If ``pending.json`` exists, the previous session applied a config and never
    marked it stable -- either it hung, or the box was reset. Restore
    ``last_good`` and clear the pending marker.

    Note this is intentionally conservative. A clean shutdown mid-session also
    leaves a pending marker, so an orderly reboot during tuning reverts too.
    That is the right trade: a spurious revert costs one re-apply, a missed
    revert costs a box that boots into a config that hangs it.

*While running* (``--monitor``)
    After the stability window elapses with the machine still up, promote the
    pending config to ``last_good`` and clear the marker.

The watchdog never *applies* a tuning config of its own. It only restores a
snapshot the server captured, so it cannot invent a state the operator never
approved.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Import the server's own snapshot/restore logic so there is exactly one
# implementation of "put the box back". A second implementation here would
# drift from the one that captured the snapshot.
for candidate in (
    Path(__file__).resolve().parent.parent / "mcp_server",
    Path("/opt/bc250-autotune/mcp_server"),
    Path.home() / "bc250-autotune/mcp_server",
):
    if (candidate / "bc250_mcp").is_dir():
        sys.path.insert(0, str(candidate))
        break

from bc250_mcp import restore, snapshots  # noqa: E402

PENDING_NAME = "pending.json"
LOG_NAME = "watchdog.log"
DEFAULT_STABILITY_MINUTES = 10


def state_root() -> Path:
    return snapshots.state_root()


def pending_path() -> Path:
    return state_root() / PENDING_NAME


def log(message: str) -> None:
    """Log to stdout (captured by the journal) and to a file.

    The file copy matters: after an unattended revert the operator needs to
    know it happened, and the journal from the *previous* boot is easy to miss.
    """
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    line = f"{stamp} bc250-watchdog: {message}"
    print(line, flush=True)
    try:
        root = state_root()
        root.mkdir(parents=True, exist_ok=True)
        with open(root / LOG_NAME, "a") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def read_pending() -> dict | None:
    try:
        return json.loads(pending_path().read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_pending(label: str, description: str, stability_minutes: int) -> None:
    """Record that a config has been applied and is not yet proven stable.

    Written before the config is applied, and flushed to disk with fsync: if the
    box dies during the apply itself, the marker must already be on disk or the
    watchdog will not know to revert.
    """
    root = state_root()
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "label": label,
        "description": description,
        "applied_at": time.time(),
        "applied_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "stability_minutes": stability_minutes,
        "boot_id": _boot_id(),
    }
    target = root / PENDING_NAME
    temp = root / f".{PENDING_NAME}.tmp"
    with open(temp, "w") as handle:
        json.dump(payload, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, target)


def clear_pending() -> None:
    try:
        pending_path().unlink()
    except OSError:
        pass


def _boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


def on_boot() -> int:
    """Revert an unproven config left behind by the previous session."""
    pending = read_pending()
    if pending is None:
        log("no pending config; nothing to revert")
        return 0

    previous_boot = pending.get("boot_id")
    current_boot = _boot_id()
    if previous_boot and current_boot and previous_boot == current_boot:
        # Same boot: --boot ran twice without a reboot in between. The config
        # has not had its chance to fail yet, so reverting would be wrong.
        log("pending config belongs to the current boot; leaving it alone")
        return 0

    label = pending.get("label", "<unknown>")
    log(
        f"pending config {label!r} was never marked stable "
        f"(applied {pending.get('applied_iso')}); reverting to "
        f"{snapshots.LAST_GOOD_LABEL}"
    )

    try:
        # restart_units=False: this unit is ordered Before= the governors,
        # so restarting them from here deadlocks (observed: watchdog hung
        # in 'activating', governor never started). They start after us and
        # read the restored config themselves.
        report = restore.restore(snapshots.LAST_GOOD_LABEL, restart_units=False)
    except snapshots.SnapshotError as exc:
        log(f"CANNOT REVERT: {exc}")
        log("leaving pending marker in place so the next boot retries")
        return 1

    for step in report.steps:
        log(f"  [{'ok' if step.ok else 'FAIL'}] {step.step}: {step.detail}")

    if report.ok:
        clear_pending()
        log("revert complete; pending marker cleared")
        return 0

    # Keep the marker: a partial revert must be retried, not forgotten.
    log("revert INCOMPLETE; pending marker kept for the next boot")
    return 1


def monitor(stability_minutes: int | None = None) -> int:
    """Wait out the stability window, then promote the pending config."""
    pending = read_pending()
    if pending is None:
        log("no pending config to monitor")
        return 0

    window = stability_minutes or pending.get(
        "stability_minutes", DEFAULT_STABILITY_MINUTES
    )
    label = pending.get("label", "<unknown>")
    applied_at = pending.get("applied_at", time.time())
    deadline = applied_at + window * 60

    remaining = deadline - time.time()
    log(f"monitoring {label!r}; {max(0, remaining):.0f}s until it is marked stable")

    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(30.0, remaining))

    if read_pending() is None:
        log("pending marker disappeared during the window; nothing to promote")
        return 0

    try:
        snapshots.capture_and_save(
            snapshots.LAST_GOOD_LABEL,
            notes=[
                f"Promoted from pending config {label!r} after {window} minutes stable.",
            ],
        )
    except snapshots.SnapshotError as exc:
        log(f"could not promote to last_good: {exc}")
        return 1

    clear_pending()
    log(f"{label!r} survived {window} minutes; promoted to last_good")
    return 0


def status() -> int:
    pending = read_pending()
    print(json.dumps({
        "state_root": str(state_root()),
        "pending": pending,
        "snapshots": [s["label"] for s in snapshots.list_snapshots()],
        "boot_id": _boot_id(),
    }, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="BC-250 AutoTune watchdog")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--boot", action="store_true",
                       help="revert an unproven config (run at boot)")
    group.add_argument("--monitor", action="store_true",
                       help="wait out the stability window, then promote")
    group.add_argument("--status", action="store_true", help="print current state")
    parser.add_argument("--stability-minutes", type=int, default=None)
    args = parser.parse_args()

    if args.boot:
        return on_boot()
    if args.monitor:
        return monitor(args.stability_minutes)
    return status()


if __name__ == "__main__":
    sys.exit(main())
