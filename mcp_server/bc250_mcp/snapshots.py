"""Configuration snapshot and rollback.

A snapshot captures the **verbatim bytes** of every config file that affects
tuning, plus the live state that is not in any file. Rollback writes those bytes
back rather than re-deriving a config from parsed values -- reconstruction would
silently drop comments, key order, and any field this tool does not model, and a
rollback that returns something subtly different from what was there is worse
than no rollback at all.

Snapshots live under a root that the **watchdog can read as root at boot**
(``/var/lib/bc250-autotune`` by default), because the watchdog's whole job is
to restore ``last_good`` before a bad config gets a second chance to hang the
box. A user-writable fallback is used when /var/lib is not writable, but the
watchdog cannot see that location, so the server warns when it happens.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import cu_config, governor, sysfs, telemetry

SYSTEM_STATE_ROOT = Path("/var/lib/bc250-autotune")
USER_STATE_ROOT = Path.home() / ".local/state/bc250-autotune"
STATE_ROOT_ENV = "BC250_STATE_ROOT"

SNAPSHOT_VERSION = 2
LAST_GOOD_LABEL = "last_good"

# Every file whose contents change tuning behaviour. Captured verbatim.
TRACKED_CONFIGS = (
    governor.OBERON_CONFIG,
    governor.CYAN_CONFIG,
    Path("/etc/bc250-smu-oc.conf"),
)

# Units whose enabled/active state is part of the config.
TRACKED_UNITS = (
    governor.OBERON_UNIT,
    governor.CYAN_UNIT,
    "bc250-smu-oc.service",
)

_TIMEOUT = 10


class SnapshotError(RuntimeError):
    """Raised when a snapshot cannot be written or read.

    Fatal on purpose: a write path that cannot snapshot must not proceed to
    change hardware, because it would have no way back.
    """


def state_root() -> Path:
    """Where snapshots live, preferring the watchdog-readable system location."""
    override = os.environ.get(STATE_ROOT_ENV)
    if override:
        return Path(override)
    return SYSTEM_STATE_ROOT if _writable(SYSTEM_STATE_ROOT) else USER_STATE_ROOT


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-probe"
        probe.write_text("")
        probe.unlink()
        return True
    except OSError:
        return False


def snapshots_dir() -> Path:
    return state_root() / "snapshots"


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return proc.returncode, proc.stdout.strip()


@dataclass
class FileCapture:
    """One config file, captured byte-for-byte."""

    path: str
    existed: bool
    content: str | None = None
    mode: int | None = None

    @classmethod
    def capture(cls, path: Path) -> FileCapture:
        try:
            stat = path.stat()
            return cls(
                path=str(path),
                existed=True,
                content=path.read_text(),
                mode=stat.st_mode & 0o777,
            )
        except OSError:
            return cls(path=str(path), existed=False)


@dataclass
class FanCapture:
    """PWM channel state.

    ``enable`` is the control mode and matters more than the duty value: mode 1
    is manual, and restoring a duty without restoring the mode can leave a fan
    pinned at whatever this tool last set, with nothing managing it.

    ``writable`` records whether the attribute can be written back at all. The
    in-tree ``nct6683`` driver -- which is what binds the BC-250's NCT6686 --
    creates ``pwmN`` mode 0444 with no store handler and no ``pwmN_enable`` at
    all, so writes fail with EACCES even as root. Only the out-of-tree
    ``nct6687d`` module provides fan control. Recording this at capture time
    lets restore skip channels it could never have changed, instead of
    reporting failures for a rollback that was never possible.
    """

    channel: int
    pwm: int | None = None
    enable: int | None = None
    writable: bool = False


@dataclass
class Snapshot:
    label: str
    version: int = SNAPSHOT_VERSION
    timestamp: float = 0.0
    iso_time: str = ""

    hostname: str | None = None
    kernel: str | None = None

    governor_backend: str | None = None
    governor_live_range_mhz: dict[str, int | None] = field(default_factory=dict)
    files: list[FileCapture] = field(default_factory=list)
    units: dict[str, dict[str, str]] = field(default_factory=dict)
    fans: list[FanCapture] = field(default_factory=list)

    cu_active_count: int | None = None
    cu_mechanism: str | None = None

    telemetry_at_capture: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Snapshot:
        files = [FileCapture(**f) for f in data.pop("files", [])]
        fans = [FanCapture(**f) for f in data.pop("fans", [])]
        known = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in known}
        snap = cls(**filtered)
        snap.files = files
        snap.fans = fans
        return snap


def _capture_fans() -> list[FanCapture]:
    superio = sysfs.find_superio_hwmon()
    if superio is None:
        return []
    captured: list[FanCapture] = []
    for channel in range(1, 8):
        pwm_path = superio / f"pwm{channel}"
        if not pwm_path.exists():
            continue
        # Owner-write bit on the sysfs attribute is the honest test: the driver
        # decides this when it registers the attribute, and no chmod changes it.
        try:
            writable = bool(pwm_path.stat().st_mode & 0o200)
        except OSError:
            writable = False

        captured.append(
            FanCapture(
                channel=channel,
                pwm=sysfs.read_int(pwm_path),
                enable=sysfs.read_int(superio / f"pwm{channel}_enable"),
                writable=writable,
            )
        )
    return captured


def capture(label: str, notes: list[str] | None = None) -> Snapshot:
    """Capture the current tuning configuration. Read-only; touches no hardware."""
    now = time.time()
    snap = Snapshot(
        label=label,
        timestamp=round(now, 3),
        iso_time=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
        notes=list(notes or []),
    )

    snap.hostname = sysfs.read_text("/proc/sys/kernel/hostname")
    snap.kernel = sysfs.read_text("/proc/sys/kernel/osrelease")

    state = governor.get_state()
    snap.governor_backend = state.backend
    # For cyan-skillfish the live D-Bus range can differ from config.toml, and
    # it is the value actually in force. Capture it separately so a rollback
    # can restore the live state as well as the file.
    snap.governor_live_range_mhz = {
        "min": state.min_freq_mhz,
        "max": state.max_freq_mhz,
    }

    snap.files = [FileCapture.capture(path) for path in TRACKED_CONFIGS]

    for unit in TRACKED_UNITS:
        _, active = _run(["systemctl", "is-active", unit])
        _, enabled = _run(["systemctl", "is-enabled", unit])
        snap.units[unit] = {"active": active or "unknown", "enabled": enabled or "unknown"}

    snap.fans = _capture_fans()

    cu = cu_config.get_config()
    snap.cu_active_count = cu.active_cu_count
    snap.cu_mechanism = cu.mechanism

    snap.telemetry_at_capture = telemetry.collect_dict()
    return snap


def save(snap: Snapshot) -> Path:
    """Persist a snapshot atomically.

    Written to a temp file and renamed, so an interrupted write cannot leave a
    truncated snapshot that rollback would later trust.
    """
    directory = snapshots_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SnapshotError(f"cannot create {directory}: {exc}") from exc

    target = directory / f"{snap.label}.json"
    temp = directory / f".{snap.label}.json.tmp"
    try:
        temp.write_text(json.dumps(snap.to_dict(), indent=2, default=str))
        os.replace(temp, target)
    except OSError as exc:
        raise SnapshotError(f"cannot write {target}: {exc}") from exc
    return target


def load(label: str) -> Snapshot:
    path = snapshots_dir() / f"{label}.json"
    try:
        data = json.loads(path.read_text())
    except OSError as exc:
        raise SnapshotError(f"no snapshot named {label!r} at {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"snapshot {path} is corrupt: {exc}") from exc

    version = data.get("version")
    if version != SNAPSHOT_VERSION:
        raise SnapshotError(
            f"snapshot {label!r} is version {version}, this build writes "
            f"version {SNAPSHOT_VERSION}; refusing to restore a layout we may "
            "not understand"
        )
    return Snapshot.from_dict(data)


def list_snapshots() -> list[dict[str, Any]]:
    directory = snapshots_dir()
    if not directory.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            out.append({"label": path.stem, "readable": False, "path": str(path)})
            continue
        out.append(
            {
                "label": data.get("label", path.stem),
                "readable": True,
                "iso_time": data.get("iso_time"),
                "governor_backend": data.get("governor_backend"),
                "range_mhz": data.get("governor_live_range_mhz"),
                "notes": data.get("notes", []),
                "path": str(path),
            }
        )
    return out


def capture_and_save(label: str, notes: list[str] | None = None) -> dict[str, Any]:
    snap = capture(label, notes)
    path = save(snap)

    result = {
        "label": snap.label,
        "path": str(path),
        "iso_time": snap.iso_time,
        "governor_backend": snap.governor_backend,
        "range_mhz": snap.governor_live_range_mhz,
        "files_captured": [f.path for f in snap.files if f.existed],
        "files_absent": [f.path for f in snap.files if not f.existed],
        "fan_channels": len(snap.fans),
        "warnings": [],
    }

    if state_root() != SYSTEM_STATE_ROOT:
        result["warnings"].append(
            f"snapshots are in {state_root()}, which the boot watchdog (running "
            f"as root) does not read. Create {SYSTEM_STATE_ROOT} writable by "
            "this user for unattended rollback to work."
        )
    return result
