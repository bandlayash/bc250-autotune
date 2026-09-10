"""Low-level sysfs access helpers.

Everything here is read-only and total: a missing or unreadable file yields
``None`` rather than raising. A BC-250 box legitimately lacks some of these
nodes -- ``gpu_busy_percent`` is unsupported on gfx1013, and ``nct6686`` may
not be loaded at all -- so absence is normal operation, not an error.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path
from typing import NamedTuple

# The amdgpu hwmon exposes these; the Super I/O chip is a separate instance.
# Accept every spelling of the Nuvoton driver: the in-tree nct6683 driver binds
# NCT6686 as ``nct6686``, while the out-of-tree Fred78290/nct6687d module
# registers as ``nct6687``. Both expose the same pwm/fan attributes.
SUPERIO_NAMES = ("nct6686", "nct6687", "nct6683")
AMDGPU_NAME = "amdgpu"


def read_text(path: str | os.PathLike[str]) -> str | None:
    """Read a sysfs file, returning None if it is absent or unreadable.

    Unreadable covers more than "missing" in sysfs: attributes an ASIC does not
    implement return EINVAL/EOPNOTSUPP on read even though the file exists.
    """
    try:
        with open(path, "r") as fh:
            return fh.read().strip()
    except (OSError, ValueError):
        return None


def read_int(path: str | os.PathLike[str]) -> int | None:
    raw = read_text(path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _scaled(path: str | os.PathLike[str], divisor: float) -> float | None:
    """Read an integer sysfs value and convert units, rounded to 3 decimals."""
    value = read_int(path)
    return None if value is None else round(value / divisor, 3)


def find_hwmon(*names: str) -> Path | None:
    """Return the first /sys/class/hwmon/hwmonN whose ``name`` matches.

    hwmon indices are assigned in probe order and are NOT stable across boots,
    so callers must resolve by name every time. Never cache the resulting path
    across a reboot.
    """
    for entry in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        path = Path(entry)
        if read_text(path / "name") in names:
            return path
    return None


def find_amdgpu_hwmon() -> Path | None:
    return find_hwmon(AMDGPU_NAME)


def find_superio_hwmon() -> Path | None:
    return find_hwmon(*SUPERIO_NAMES)


def find_gpu_device() -> Path | None:
    """Return /sys/class/drm/cardN/device for the BC-250's amdgpu instance.

    Card numbering is not fixed either -- on the test box the BC-250 is card1,
    not card0 -- so match on the PCI ID of Cyan Skillfish (1002:13FE) and fall
    back to any amdgpu card if the ID ever changes across kernel revisions.
    """
    fallback: Path | None = None
    for entry in sorted(glob.glob("/sys/class/drm/card[0-9]*/device")):
        path = Path(entry)
        uevent = read_text(path / "uevent") or ""
        if "DRIVER=amdgpu" not in uevent:
            continue
        if "PCI_ID=1002:13FE" in uevent.upper():
            return path
        fallback = fallback or path
    return fallback


class DpmTable(NamedTuple):
    """A parsed pp_dpm_* table.

    ``current_mhz``     the frequency in force right now.
    ``states_mhz``      every distinct value in the table, sorted. Truthful, so
                        on gfx1013 it still contains the live-readout value.
    ``entries``         every row as (index, mhz, is_current), unmodified.
    ``min_mhz``/``max_mhz``
                        the tuning bounds, with a detected live-readout row
                        excluded. Use these, not ``min(states_mhz)``.
    """

    current_mhz: int | None
    states_mhz: list[int]
    entries: list[tuple[int, int, bool]]
    min_mhz: int | None
    max_mhz: int | None


def parse_dpm_clock(raw: str | None) -> DpmTable:
    """Parse a pp_dpm_* table, separating selectable states from the live readout.

    The format is one DPM state per line, with an asterisk marking the entry in
    force right now::

        0: 350Mhz
        1: 89Mhz *
        2: 2230Mhz

    gfx1013 has a quirk that matters: the middle entry is not a selectable state
    at all, it is a live readout of the actual clock, and its value changes
    between reads (350/89/2230 one moment, 350/12/2230 the next). Treating it as
    a DPM state makes ``min(states)`` report something like 12 MHz as the
    hardware floor, when the real floor is 350 MHz.

    A marked row is treated as a live readout, and excluded from ``min_mhz`` /
    ``max_mhz``, only when all three hold:

    1. there are at least two unmarked rows to fall back on,
    2. its value appears nowhere else in the table, and
    3. its value is below the highest unmarked row.

    Condition 3 is the important one. Without it, a normal ASIC sitting at its
    top state (``2000Mhz *`` alongside 500 and 1000) would have that state
    dropped and report a 1000 MHz ceiling -- worse than the bug being fixed.
    Because the gfx1013 readout row is always below the 2230 MHz top state, it
    is still caught.

    ``states_mhz`` and ``entries`` stay truthful and include every row; only the
    bounds apply the exclusion.
    """
    empty = DpmTable(None, [], [], None, None)
    if not raw:
        return empty

    entries: list[tuple[int, int, bool]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        index_text, _, value = line.partition(":")
        marked = value.rstrip().endswith("*")
        digits = "".join(ch for ch in value if ch.isdigit())
        if not digits:
            continue
        try:
            index = int(index_text.strip())
        except ValueError:
            index = len(entries)
        entries.append((index, int(digits), marked))

    if not entries:
        return empty

    current = next((mhz for _, mhz, marked in entries if marked), None)
    all_values = sorted({mhz for _, mhz, _ in entries})
    unmarked = sorted({mhz for _, mhz, marked in entries if not marked})

    looks_like_live_readout = (
        current is not None
        and len(unmarked) >= 2
        and current not in unmarked
        and current < max(unmarked)
    )
    bounds_source = unmarked if looks_like_live_readout else all_values

    return DpmTable(
        current_mhz=current,
        states_mhz=all_values,
        entries=entries,
        min_mhz=min(bounds_source) if bounds_source else None,
        max_mhz=max(bounds_source) if bounds_source else None,
    )


def hwmon_millicelsius(hwmon: Path | None, attr: str) -> float | None:
    """Read a hwmon temperature attribute, converting millidegrees to degrees C."""
    return None if hwmon is None else _scaled(hwmon / attr, 1000.0)


def hwmon_microwatts(hwmon: Path | None, attr: str) -> float | None:
    """Read a hwmon power attribute, converting microwatts to watts."""
    return None if hwmon is None else _scaled(hwmon / attr, 1_000_000.0)
