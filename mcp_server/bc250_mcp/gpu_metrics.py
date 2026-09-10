"""Decoder for the amdgpu ``gpu_metrics`` binary SMU struct (v2_2).

This is the richest telemetry source on the BC-250 and the only one that
reports **throttle status**, which is what turns "tune until it gets hot" into
a precise stopping condition: the SMU tells us directly which limiter engaged,
rather than us inferring it from a temperature curve.

It also supplies two things plain sysfs cannot:

``gfx_activity``  GPU load. ``gpu_busy_percent`` is unimplemented on gfx1013,
                  which is why both governors derive load by other means.
``cur_gfxclk``    the real current GFX clock, rather than the ``pp_dpm_sclk``
                  table's live-readout row.

The struct layout and throttle bit map here are adapted from ``bc250-metrics.py``
by the owner of the test unit, whose offsets were verified on-device by
correlating ``system_clock_counter`` against uptime and ``average_socket_power``
against the hwmon PPT reading. That verification is the reason this decoder is
trusted; header-derived offsets alone would not be.

The node is world-readable (mode 444), so no root is required.
"""

from __future__ import annotations

import struct
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import sysfs

METRICS_ATTR = "gpu_metrics"
STRUCT_MIN_SIZE = 128
SUPPORTED_VERSION = (2, 2)

# Sentinels the SMU writes for "this field is not populated".
INVALID_U16 = 0xFFFF
INVALID_U32 = 0xFFFFFFFF
INVALID_U64 = 0xFFFFFFFFFFFFFFFF

# Bit -> limiter name for indep_throttle_status. Bits 32+ are the thermal
# limiters, which are the ones the optimizer treats as the wall.
THROTTLER_BITS: dict[int, str] = {
    0: "PPT0", 1: "PPT1", 2: "PPT2", 3: "PPT3",
    4: "SPL", 5: "FPPT", 6: "SPPT", 7: "SPPT_APU",
    16: "TDC_GFX", 17: "TDC_SOC", 18: "TDC_MEM", 19: "TDC_VDD", 20: "TDC_CVIP",
    21: "EDC_CPU", 22: "EDC_GFX", 23: "APCC",
    32: "TEMP_GPU", 33: "TEMP_CORE", 34: "TEMP_MEM", 35: "TEMP_EDGE",
    36: "TEMP_HOTSPOT", 37: "TEMP_SOC", 38: "TEMP_VR_GFX", 39: "TEMP_VR_SOC",
    40: "TEMP_VR_MEM0", 41: "TEMP_VR_MEM1",
    44: "VRHOT0", 45: "VRHOT1", 46: "PROCHOT_CPU", 47: "PROCHOT_GFX",
    48: "PPM", 49: "FIT",
}

# Limiters that mean "too hot". Hitting one of these is the optimizer's signal
# to stop climbing and back off a step.
THERMAL_THROTTLERS = frozenset(
    {
        "TEMP_GPU", "TEMP_CORE", "TEMP_MEM", "TEMP_EDGE", "TEMP_HOTSPOT",
        "TEMP_SOC", "TEMP_VR_GFX", "TEMP_VR_SOC", "TEMP_VR_MEM0",
        "TEMP_VR_MEM1", "VRHOT0", "VRHOT1", "PROCHOT_CPU", "PROCHOT_GFX",
    }
)

# Limiters that mean "out of power/current budget" rather than "too hot".
# Distinguishing them matters: a power limit is not fixed by better cooling,
# so the optimizer should report it differently.
POWER_THROTTLERS = frozenset(
    {
        "PPT0", "PPT1", "PPT2", "PPT3", "SPL", "FPPT", "SPPT", "SPPT_APU",
        "TDC_GFX", "TDC_SOC", "TDC_MEM", "TDC_VDD", "TDC_CVIP",
        "EDC_CPU", "EDC_GFX", "APCC", "PPM", "FIT",
    }
)

# Field offsets within gpu_metrics_v2_2.
_OFF_GFX_TEMP = 4
_OFF_SOC_TEMP = 6
_OFF_CORE_TEMP = 8       # 8 x u16
_OFF_L3_TEMP = 24        # 2 x u16
_OFF_GFX_ACTIVITY = 28
_OFF_SOCKET_POWER = 40
_OFF_CPU_POWER = 42
_OFF_SOC_POWER = 44
_OFF_GFX_POWER = 46
# Two clock fields, and they are NOT equally trustworthy on this ASIC.
#
# Measured under sustained FurMark load, with the GPU hot (90 C) and drawing
# ~137 W, the two read:
#
#     offset 76 (cur_gfxclk):  20, 2, 5, 3, 8, 5 MHz
#     offset 64 (avg_gfxclk):  415, 0, 773, 772, 776, 772 MHz
#
# offset 76 tracked the pp_dpm_sclk live-readout row *exactly*, sample for
# sample -- the same unreliable counter behind a second name, reading near zero
# while the GPU was plainly saturated. offset 64 gives a plausible sustained
# clock. So avg_gfxclk is the clock to reason about, and cur_gfxclk is reported
# but never used as the authoritative value. Both occasionally return 0.
_OFF_AVG_GFXCLK = 64
_OFF_CUR_GFXCLK = 76
_OFF_CORE_CLK = 88       # 8 x u16
_OFF_THROTTLE_U32 = 108  # legacy throttle_status
_OFF_THROTTLE_U64 = 120  # indep_throttle_status

# Core temperatures below this are the SMU reporting a parked/absent core
# rather than a real reading.
_CORE_TEMP_FLOOR_C = 5.0


@dataclass
class GpuMetrics:
    available: bool = False
    version: str | None = None

    gfx_temp_c: float | None = None
    soc_temp_c: float | None = None
    core_temps_c: list[float | None] = field(default_factory=list)
    l3_temps_c: list[float | None] = field(default_factory=list)

    gfx_activity_percent: float | None = None

    socket_power_w: float | None = None
    cpu_power_w: float | None = None
    soc_power_w: float | None = None
    gfx_power_w: float | None = None

    avg_gfxclk_mhz: float | None = None
    cur_gfxclk_mhz: float | None = None
    core_clocks_mhz: list[float | None] = field(default_factory=list)

    throttle_raw: int | None = None
    throttle_flags: list[str] = field(default_factory=list)
    throttle_reported: bool = False

    warnings: list[str] = field(default_factory=list)

    @property
    def is_throttling(self) -> bool:
        return bool(self.throttle_flags)

    @property
    def thermal_throttle_flags(self) -> list[str]:
        return [f for f in self.throttle_flags if f in THERMAL_THROTTLERS]

    @property
    def power_throttle_flags(self) -> list[str]:
        return [f for f in self.throttle_flags if f in POWER_THROTTLERS]

    @property
    def is_thermally_throttling(self) -> bool:
        """The optimizer's stopping condition for a max-performance objective."""
        return bool(self.thermal_throttle_flags)

    @property
    def is_power_throttling(self) -> bool:
        return bool(self.power_throttle_flags)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.update(
            is_throttling=self.is_throttling,
            is_thermally_throttling=self.is_thermally_throttling,
            is_power_throttling=self.is_power_throttling,
            thermal_throttle_flags=self.thermal_throttle_flags,
            power_throttle_flags=self.power_throttle_flags,
        )
        return data


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _scaled_u16(
    data: bytes, offset: int, divisor: float = 1.0
) -> float | None:
    raw = _u16(data, offset)
    if raw == INVALID_U16:
        return None
    return round(raw / divisor, 2)


def decode(data: bytes) -> GpuMetrics:
    """Decode a gpu_metrics blob. Never raises on malformed input."""
    metrics = GpuMetrics()

    if len(data) < STRUCT_MIN_SIZE:
        metrics.warnings.append(
            f"gpu_metrics is {len(data)} bytes, expected at least {STRUCT_MIN_SIZE}"
        )
        return metrics

    _size, fmt_rev, content_rev = struct.unpack_from("<HBB", data, 0)
    metrics.version = f"v{fmt_rev}_{content_rev}"

    if (fmt_rev, content_rev) != SUPPORTED_VERSION:
        # Offsets differ between revisions, so decoding anyway would produce
        # confident nonsense. Refuse rather than guess.
        metrics.warnings.append(
            f"gpu_metrics is {metrics.version}, decoder supports "
            f"v{SUPPORTED_VERSION[0]}_{SUPPORTED_VERSION[1]}; not decoding"
        )
        return metrics

    metrics.available = True
    metrics.gfx_temp_c = _scaled_u16(data, _OFF_GFX_TEMP, 100)
    metrics.soc_temp_c = _scaled_u16(data, _OFF_SOC_TEMP, 100)
    metrics.gfx_activity_percent = _scaled_u16(data, _OFF_GFX_ACTIVITY)

    metrics.socket_power_w = _scaled_u16(data, _OFF_SOCKET_POWER, 1000)
    metrics.cpu_power_w = _scaled_u16(data, _OFF_CPU_POWER, 1000)
    metrics.soc_power_w = _scaled_u16(data, _OFF_SOC_POWER, 1000)
    metrics.gfx_power_w = _scaled_u16(data, _OFF_GFX_POWER, 1000)

    metrics.avg_gfxclk_mhz = _scaled_u16(data, _OFF_AVG_GFXCLK)
    metrics.cur_gfxclk_mhz = _scaled_u16(data, _OFF_CUR_GFXCLK)

    metrics.l3_temps_c = [
        _scaled_u16(data, _OFF_L3_TEMP + 2 * i, 100) for i in range(2)
    ]

    core_temps: list[float | None] = []
    core_clocks: list[float | None] = []
    for index in range(8):
        temp = _scaled_u16(data, _OFF_CORE_TEMP + 2 * index, 100)
        # A parked or harvested core reports a near-zero temperature; treat
        # that as "no reading" rather than a real sub-ambient measurement.
        core_temps.append(temp if temp is not None and temp > _CORE_TEMP_FLOOR_C else None)
        core_clocks.append(_scaled_u16(data, _OFF_CORE_CLK + 2 * index))
    metrics.core_temps_c = core_temps
    metrics.core_clocks_mhz = core_clocks

    _decode_throttle(data, metrics)
    return metrics


def _decode_throttle(data: bytes, metrics: GpuMetrics) -> None:
    """Resolve throttle status, preferring the 64-bit independent field.

    On this SMU ``indep_throttle_status`` is frequently all-ones, meaning "not
    reported" rather than "every limiter engaged" -- decoding it literally would
    claim the board is throttling on all 50 limiters at once. Fall back to the
    legacy 32-bit field, and if that is also all-ones, report the status as
    unknown instead of inventing one.
    """
    throttle_64 = struct.unpack_from("<Q", data, _OFF_THROTTLE_U64)[0]
    throttle_32 = struct.unpack_from("<I", data, _OFF_THROTTLE_U32)[0]

    if throttle_64 != INVALID_U64:
        raw, bit_map = throttle_64, THROTTLER_BITS
    elif throttle_32 != INVALID_U32:
        raw = throttle_32
        bit_map = {bit: name for bit, name in THROTTLER_BITS.items() if bit < 32}
    else:
        metrics.throttle_reported = False
        metrics.warnings.append(
            "SMU reports no throttle status; thermal-limit detection must fall "
            "back to temperature thresholds"
        )
        return

    metrics.throttle_reported = True
    metrics.throttle_raw = raw
    metrics.throttle_flags = [
        name for bit, name in sorted(bit_map.items()) if raw >> bit & 1
    ]


def read(device: Path | None = None) -> GpuMetrics:
    """Read and decode gpu_metrics for the BC-250's amdgpu device."""
    device = device or sysfs.find_gpu_device()
    if device is None:
        metrics = GpuMetrics()
        metrics.warnings.append("no amdgpu device found; gpu_metrics unavailable")
        return metrics

    try:
        # Must be read in one shot: the driver regenerates the buffer per open,
        # so a partial or re-seeked read can straddle two samples.
        data = (device / METRICS_ATTR).read_bytes()
    except OSError as exc:
        metrics = GpuMetrics()
        metrics.warnings.append(f"cannot read {device / METRICS_ATTR}: {exc}")
        return metrics

    return decode(data)


def read_dict(device: Path | None = None) -> dict[str, Any]:
    return read(device).to_dict()
