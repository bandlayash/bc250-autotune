"""CPU overclock/undervolt state.

This module is read-only and deliberately avoids the SMU mailbox. Reading
Vid through ``Bc250Smu`` needs root and contends with the governor daemon for
the same mailbox, which is too high a price for a status call that may be
polled during a benchmark. Everything here comes from the config file, systemd,
and /proc.

Applying an overclock delegates to upstream ``bc250-detect`` / ``bc250-apply``
rather than driving the SMU directly -- see docs/DESIGN.md.
"""

from __future__ import annotations

import configparser
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

OC_CONFIG = Path("/etc/bc250-smu-oc.conf")
OC_UNIT = "bc250-smu-oc.service"

# Source: bc250_smu_oc/bc250_detect.py revert_defaults(). These are what the
# tool restores when reverting, so they define "stock" for our purposes.
STOCK_FREQUENCY_MHZ = 3500
STOCK_CURVE_SCALE = 0
STOCK_MAX_TEMPERATURE_C = 100


@dataclass
class CpuState:
    stock_frequency_mhz: int = STOCK_FREQUENCY_MHZ
    stock_curve_scale: int = STOCK_CURVE_SCALE
    stock_max_temperature_c: int = STOCK_MAX_TEMPERATURE_C

    configured_frequency_mhz: int | None = None
    configured_curve_scale: int | None = None
    configured_max_temperature_c: int | None = None
    config_path: str | None = None
    persistent: bool = False

    cores_online: int | None = None
    cores_present: int | None = None
    cores_unlocked: bool | None = None

    live_clocks_mhz: list[float] = field(default_factory=list)
    max_live_clock_mhz: float | None = None

    tools_available: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_overclocked(self) -> bool:
        return (
            self.configured_frequency_mhz is not None
            and self.configured_frequency_mhz > STOCK_FREQUENCY_MHZ
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["is_overclocked"] = self.is_overclocked
        return data


def _read_config(state: CpuState) -> None:
    if not OC_CONFIG.exists():
        return
    state.config_path = str(OC_CONFIG)
    parser = configparser.ConfigParser()
    try:
        parser.read(OC_CONFIG)
        state.configured_frequency_mhz = parser.getint("overclock", "frequency")
        state.configured_curve_scale = parser.getint("overclock", "scale")
        state.configured_max_temperature_c = parser.getint(
            "overclock", "max_temperature"
        )
    except (configparser.Error, ValueError) as exc:
        state.warnings.append(f"cannot parse {OC_CONFIG}: {exc}")


def _read_cores(state: CpuState) -> None:
    """Report core counts and whether the 2 harvested cores are unlocked.

    The BC-250 ships with a core presence mask of 0x77 -- 6c/12t, core 3 of each
    CCX disabled. Unlocked is 8c/16t. Counting online CPUs is enough to tell
    them apart without touching the SMU.
    """
    try:
        online = int(
            subprocess.run(
                ["nproc"], capture_output=True, text=True, timeout=5, check=False
            ).stdout.strip()
        )
        state.cores_online = online
    except (OSError, ValueError, subprocess.SubprocessError):
        return

    try:
        present = len(
            [
                line
                for line in Path("/proc/cpuinfo").read_text().splitlines()
                if line.startswith("processor")
            ]
        )
        state.cores_present = present
    except OSError:
        present = state.cores_online

    # 16 threads means both harvested cores came online; 12 is stock.
    if present >= 16:
        state.cores_unlocked = True
    elif present <= 12:
        state.cores_unlocked = False


def _read_live_clocks(state: CpuState) -> None:
    try:
        raw = Path("/proc/cpuinfo").read_text()
    except OSError:
        return
    clocks: list[float] = []
    for line in raw.splitlines():
        if line.lower().startswith("cpu mhz"):
            _, _, value = line.partition(":")
            try:
                clocks.append(round(float(value.strip()), 1))
            except ValueError:
                continue
    state.live_clocks_mhz = clocks
    if clocks:
        state.max_live_clock_mhz = max(clocks)


def get_state() -> CpuState:
    state = CpuState()

    _read_config(state)
    _read_cores(state)
    _read_live_clocks(state)

    state.tools_available = {
        name: shutil.which(name) is not None
        for name in ("bc250-detect", "bc250-apply", "stress", "stress-ng")
    }

    # bc250-detect shells out to `stress` specifically (stress_helper.py); it
    # does not fall back to stress-ng, so having only stress-ng is not enough.
    if state.tools_available.get("bc250-detect") and not state.tools_available.get(
        "stress"
    ):
        state.warnings.append(
            "bc250-detect is installed but `stress` is not; detection will fail "
            "(stress-ng is not a substitute -- upstream invokes `stress` by name)"
        )

    try:
        proc = subprocess.run(
            ["systemctl", "is-enabled", OC_UNIT],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        state.persistent = proc.stdout.strip() == "enabled"
    except (OSError, subprocess.SubprocessError):
        pass

    if state.configured_frequency_mhz is None:
        state.warnings.append(
            "no CPU overclock configured; running stock (3500 MHz, scale 0)"
        )

    return state


def get_state_dict() -> dict[str, Any]:
    return get_state().to_dict()
