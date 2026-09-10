"""Read-only telemetry for the BC-250.

No writes, no root, no SMU mailbox traffic. Everything is plain sysfs, so this
module is safe to poll during a benchmark without perturbing what is being
measured or contending with the governor daemon for the SMU.

Every field is optional. On a box without the Super I/O driver loaded there are
no fan readings at all, and gfx1013 does not implement ``gpu_busy_percent``.
Callers get ``None`` for those rather than an exception, so a partial sensor
set still produces a usable reading.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import sysfs

# amdgpu's hwmon exposes two unlabelled voltage rails, in0 and in1. Which is
# which was established empirically against the SMU on the test unit rather
# than assumed, because getting it backwards would hide the most dangerous
# number in the system behind a harmless-looking name:
#
#   in0_input  GPU/GFX rail. Held steady at 868 mV while the SMU's
#              q3_0x37_get_current_gpu_voltage() read 874 mV -- a small constant
#              offset from separate sampling, tracking the same rail.
#   in1_input  CPU core voltage (Vid). Swung 1175 -> 806 mV as CPU load fell,
#              matching q3_0x36_get_current_cpu_voltage() exactly sample for
#              sample. This is NOT the SoC rail.
#
# That in1 is CPU Vid matters twice over. It is the value upstream warns must
# never exceed 1325 mV -- they bricked a board that way -- and having it in
# sysfs means the optimizer can watch it continuously during a benchmark
# without root and without contending with the governor for the SMU mailbox.
GPU_VOLTAGE_ATTR = "in0_input"
CPU_VID_ATTR = "in1_input"

# Upstream bc250_smu_oc: "Always make sure that CPU core voltage (Vid) does not
# exceed 1.325 V under any circumstances". Deliberately duplicated here rather
# than read from safety_envelope.yaml: this is a tripwire on what the hardware
# is *actually doing*, and it must still fire if the envelope is missing,
# misconfigured, or loosened. The envelope governs what we ask for; this
# observes what happened.
VID_DANGER_MV = 1325


@dataclass
class FanReading:
    """One fan channel. ``rpm`` of 0 means a header with nothing plugged in."""

    channel: int
    rpm: int
    pwm: int | None = None

    @property
    def pwm_percent(self) -> float | None:
        """PWM duty as a percentage. sysfs stores it as 0-255, not 0-100."""
        return None if self.pwm is None else round(self.pwm * 100.0 / 255.0, 1)


@dataclass
class Telemetry:
    timestamp: float
    iso_time: str

    gpu_temp_c: float | None = None
    gpu_power_w: float | None = None
    gpu_clock_mhz: int | None = None
    gpu_dpm_states_mhz: list[int] = field(default_factory=list)
    gpu_clock_floor_mhz: int | None = None
    gpu_clock_ceiling_mhz: int | None = None
    gpu_busy_percent: int | None = None
    gpu_voltage_mv: int | None = None
    performance_level: str | None = None

    cpu_temp_c: float | None = None
    cpu_vid_mv: int | None = None
    cpu_clocks_mhz: list[float] = field(default_factory=list)

    fans: list[FanReading] = field(default_factory=list)
    superio_driver: str | None = None

    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["fans"] = [
            {**asdict(fan), "pwm_percent": fan.pwm_percent} for fan in self.fans
        ]
        return data


def _read_cpu_clocks() -> list[float]:
    """Per-core MHz from /proc/cpuinfo.

    Compared against the commanded clock this is the clock-stretching signal --
    the same one bc250-detect uses to spot thermal throttling, and the stopping
    condition for the optimizer.
    """
    clocks: list[float] = []
    raw = sysfs.read_text("/proc/cpuinfo")
    if not raw:
        return clocks
    for line in raw.splitlines():
        if line.lower().startswith("cpu mhz"):
            _, _, value = line.partition(":")
            try:
                clocks.append(round(float(value.strip()), 1))
            except ValueError:
                continue
    return clocks


def _read_fans(superio: Path | None) -> list[FanReading]:
    """Collect fan channels, skipping headers that report no tachometer.

    The BC-250 board exposes five fan headers but populates only one, so
    reporting all five would bury the single real reading in zeros.
    """
    fans: list[FanReading] = []
    if superio is None:
        return fans
    for channel in range(1, 8):
        rpm = sysfs.read_int(superio / f"fan{channel}_input")
        if rpm is None:
            continue
        if rpm == 0:
            continue
        fans.append(
            FanReading(
                channel=channel,
                rpm=rpm,
                pwm=sysfs.read_int(superio / f"pwm{channel}"),
            )
        )
    return fans


def _cpu_temp(superio: Path | None) -> float | None:
    """CPU package temperature.

    Preferred source is k10temp. Failing that, the Super I/O chip reads the same
    die over AMD TSI -- on the test box that is temp1 of the nct6686, labelled
    "AMD TSI Addr 98h". The thermistor channels next to it read board ambient,
    not the die, so they are deliberately not used as a fallback.
    """
    k10 = sysfs.find_hwmon("k10temp")
    if k10 is not None:
        value = sysfs.hwmon_millicelsius(k10, "temp1_input")
        if value is not None:
            return value
    return sysfs.hwmon_millicelsius(superio, "temp1_input")


def collect() -> Telemetry:
    """Take a single telemetry sample. Never raises on missing hardware."""
    now = time.time()
    reading = Telemetry(
        timestamp=round(now, 3),
        iso_time=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
    )

    gpu_hwmon = sysfs.find_amdgpu_hwmon()
    superio = sysfs.find_superio_hwmon()
    device = sysfs.find_gpu_device()

    if gpu_hwmon is None:
        reading.warnings.append("amdgpu hwmon not found; GPU sensors unavailable")
    else:
        reading.gpu_temp_c = sysfs.hwmon_millicelsius(gpu_hwmon, "temp1_input")
        reading.gpu_power_w = sysfs.hwmon_microwatts(gpu_hwmon, "power1_average")
        reading.gpu_voltage_mv = sysfs.read_int(gpu_hwmon / GPU_VOLTAGE_ATTR)
        reading.cpu_vid_mv = sysfs.read_int(gpu_hwmon / CPU_VID_ATTR)

        if reading.cpu_vid_mv is not None and reading.cpu_vid_mv > VID_DANGER_MV:
            # Nothing this software applies should be able to reach here, so if
            # it does, something outside our control is driving Vid past the
            # level upstream documents as having destroyed hardware. Say so
            # loudly; the caller is expected to stop and revert.
            reading.warnings.append(
                f"DANGER: CPU Vid is {reading.cpu_vid_mv} mV, above the "
                f"{VID_DANGER_MV} mV level upstream documents as destroying a "
                "BC-250. Revert to stock and investigate before continuing."
            )

    if device is None:
        reading.warnings.append("no amdgpu DRM device found; is this a BC-250?")
    else:
        dpm = sysfs.parse_dpm_clock(sysfs.read_text(device / "pp_dpm_sclk"))
        reading.gpu_clock_mhz = dpm.current_mhz
        reading.gpu_dpm_states_mhz = dpm.states_mhz
        # The floor/ceiling are what the optimizer may range between. They come
        # from the selectable states only, never the live readout row.
        reading.gpu_clock_floor_mhz = dpm.min_mhz
        reading.gpu_clock_ceiling_mhz = dpm.max_mhz
        reading.performance_level = sysfs.read_text(
            device / "power_dpm_force_performance_level"
        )
        # gfx1013 does not implement this counter. Its absence is why both
        # governors derive load by other means, and why we do not treat it as
        # an error.
        reading.gpu_busy_percent = sysfs.read_int(device / "gpu_busy_percent")

    if superio is None:
        reading.warnings.append(
            "no Super I/O hwmon (nct6686/nct6687); fan RPM and PWM unavailable"
        )
    else:
        reading.superio_driver = sysfs.read_text(superio / "name")

    reading.fans = _read_fans(superio)
    reading.cpu_temp_c = _cpu_temp(superio)
    reading.cpu_clocks_mhz = _read_cpu_clocks()

    return reading


def collect_dict() -> dict[str, Any]:
    return collect().to_dict()
