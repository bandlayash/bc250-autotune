"""Fan control through the Super I/O hwmon interface.

**This only works with the out-of-tree ``nct6687d`` driver.** The in-tree
``nct6683`` driver that otherwise binds the BC-250's NCT6686 creates ``pwmN``
as mode 0444 with no store handler and no ``pwmN_enable`` at all, so writes
fail with ``EACCES`` even as root. Every function here checks writability first
and reports honestly rather than failing obscurely.

What this is good for, and what it is not. The obvious hope is that more
airflow buys thermal headroom on a board that is thermally bound. **On the
reference unit it does not**: the NCT6686's own automatic mode already holds
the fan at 100% (pwm 255, ~2800 RPM) continuously, so pinning it manually to
100% changes nothing measurable. There is no airflow left to gain, and the
cooler itself is the limit.

That makes this primarily a **noise and thermal-profile** control rather than a
performance one -- useful for running the board quieter at the cost of
temperature, which is the opposite trade. Check `get_state()` on your own
board before assuming either way; a unit with a different cooler or a less
aggressive default curve may well have headroom this one does not.

Two safety rules are enforced as clamps, not warnings:

* No curve point may fall below ``fan.hard_min_pwm_percent``. A BC-250 draws
  ~50 W at idle; a curve that stops the fan is never acceptable.
* Above ``fan.force_full_speed_above_c`` the fan is pinned to 100% regardless
  of what the curve asked for.
"""

from __future__ import annotations

import itertools
import os
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import envelope, sysfs

# pwmN_enable modes, as the hwmon ABI defines them.
PWM_DISABLED = 0   # full speed, no control
PWM_MANUAL = 1     # duty follows pwmN
PWM_AUTOMATIC = 2  # the chip's own thermal cruise

PWM_MAX_RAW = 255


def _pct_to_raw(percent: float) -> int:
    """Percent to the 0-255 duty sysfs expects, clamped to the valid range."""
    return max(0, min(PWM_MAX_RAW, round(percent * PWM_MAX_RAW / 100.0)))


def _raw_to_pct(raw: int) -> float:
    return round(raw * 100.0 / PWM_MAX_RAW, 1)


@dataclass
class FanChannel:
    channel: int
    pwm_raw: int | None = None
    pwm_percent: float | None = None
    enable_mode: int | None = None
    rpm: int | None = None
    writable: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["enable_mode_name"] = {
            PWM_DISABLED: "disabled (full speed)",
            PWM_MANUAL: "manual",
            PWM_AUTOMATIC: "automatic",
        }.get(self.enable_mode, "unknown")
        return data


@dataclass
class FanState:
    driver: str | None = None
    hwmon_path: str | None = None
    controllable: bool = False
    channels: list[FanChannel] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "driver": self.driver,
            "hwmon_path": self.hwmon_path,
            "controllable": self.controllable,
            "channels": [c.to_dict() for c in self.channels],
            "warnings": self.warnings,
        }


def get_state() -> FanState:
    """Report every PWM channel and whether fan control is possible at all."""
    state = FanState()
    hwmon = sysfs.find_superio_hwmon()
    if hwmon is None:
        state.warnings.append(
            "no Super I/O hwmon found; this board exposes no fan interface"
        )
        return state

    state.hwmon_path = str(hwmon)
    state.driver = sysfs.read_text(hwmon / "name")

    for channel in range(1, 8):
        pwm_path = hwmon / f"pwm{channel}"
        if not pwm_path.exists():
            continue
        try:
            writable = bool(pwm_path.stat().st_mode & 0o200)
        except OSError:
            writable = False

        raw = sysfs.read_int(pwm_path)
        state.channels.append(
            FanChannel(
                channel=channel,
                pwm_raw=raw,
                pwm_percent=_raw_to_pct(raw) if raw is not None else None,
                enable_mode=sysfs.read_int(hwmon / f"pwm{channel}_enable"),
                rpm=sysfs.read_int(hwmon / f"fan{channel}_input"),
                writable=writable,
            )
        )

    state.controllable = any(c.writable for c in state.channels)
    if state.channels and not state.controllable:
        state.warnings.append(
            f"PWM channels are read-only under the '{state.driver}' driver. Fan "
            "control needs the out-of-tree nct6687d module; the in-tree nct6683 "
            "registers no store handler, so writes fail with EACCES even as root."
        )
    return state


def _write_sysfs(path: Path, value: int) -> tuple[bool, str]:
    """Write an integer to a root-owned sysfs attribute."""
    try:
        with open(path, "w") as handle:
            handle.write(str(value))
        return True, f"{path.name}={value}"
    except OSError:
        pass

    cmd = ["sh", "-c", f"printf '%s' {value} > {path}"]
    if getattr(os, "geteuid", lambda: 1)() != 0:
        cmd = ["sudo", "-n", *cmd]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{path.name}: {exc}"
    if proc.returncode != 0:
        return False, f"{path.name}: {proc.stderr.strip() or 'write failed'}"
    return True, f"{path.name}={value}"


def validate_curve(
    curve: list[tuple[float, float]], doc: dict[str, Any] | None = None
) -> tuple[list[tuple[float, float]], list[str]]:
    """Clamp a (temperature, pwm-percent) curve into the safety envelope.

    Returns the clamped curve and a note for every adjustment made, so the
    caller can see that what it asked for is not what will be applied. Unlike
    the frequency tools this **clamps rather than refuses**: a fan curve that is
    too slow is a safety problem to be corrected, not a request to reject, and
    refusing outright would leave the previous curve in place, which may be
    worse.
    """
    doc = doc if doc is not None else envelope.load()
    fan = doc.get("fan", {})
    floor = float(fan.get("hard_min_pwm_percent", 25))
    ceiling = float(fan.get("hard_max_pwm_percent", 100))
    full_speed_above = float(fan.get("force_full_speed_above_c", 88))

    notes: list[str] = []
    clamped: list[tuple[float, float]] = []

    for temp, percent in sorted(curve, key=lambda point: point[0]):
        adjusted = percent
        if adjusted < floor:
            notes.append(
                f"{temp:g} C: {percent:g}% raised to the {floor:g}% floor "
                "(a stopped fan is never acceptable on a board that idles ~50 W)"
            )
            adjusted = floor
        if adjusted > ceiling:
            notes.append(f"{temp:g} C: {percent:g}% capped at {ceiling:g}%")
            adjusted = ceiling
        if temp >= full_speed_above and adjusted < 100.0:
            notes.append(
                f"{temp:g} C is at or above the {full_speed_above:g} C "
                f"full-speed threshold; {adjusted:g}% forced to 100%"
            )
            adjusted = 100.0
        clamped.append((temp, adjusted))

    # A curve that never reaches the full-speed threshold cannot protect the
    # board at the top end, so extend it.
    if clamped and max(t for t, _ in clamped) < full_speed_above:
        clamped.append((full_speed_above, 100.0))
        notes.append(
            f"appended a {full_speed_above:g} C -> 100% point; the curve ended "
            "below the full-speed threshold and would have left the top end "
            "uncovered"
        )
    return clamped, notes


def pwm_for_temperature(curve: list[tuple[float, float]], temp_c: float) -> float:
    """Interpolate a curve linearly. Flat outside its endpoints."""
    if not curve:
        return 100.0
    points = sorted(curve, key=lambda point: point[0])
    if temp_c <= points[0][0]:
        return points[0][1]
    if temp_c >= points[-1][0]:
        return points[-1][1]
    for (t0, p0), (t1, p1) in itertools.pairwise(points):
        if t0 <= temp_c <= t1:
            if t1 == t0:
                return p1
            span = (temp_c - t0) / (t1 - t0)
            return p0 + (p1 - p0) * span
    return points[-1][1]


def set_manual(percent: float, channels: list[int] | None = None) -> dict[str, Any]:
    """Pin the fan to a fixed duty cycle, clamped into the envelope.

    Applied by writing ``pwmN_enable`` first and ``pwmN`` second: switching
    control mode can reset the duty, so the reverse order would have the value
    immediately overwritten.
    """
    doc = envelope.load()
    state = get_state()
    if not state.controllable:
        return {
            "applied": False,
            "detail": "fan control unavailable",
            "warnings": state.warnings,
        }

    clamped, notes = validate_curve([(0.0, percent)], doc)
    target_pct = clamped[0][1]
    raw = _pct_to_raw(target_pct)

    hwmon = Path(state.hwmon_path or "")
    targets = channels or [c.channel for c in state.channels if c.writable]
    results: list[str] = []
    ok = True
    for channel in targets:
        good, detail = _write_sysfs(hwmon / f"pwm{channel}_enable", PWM_MANUAL)
        ok = ok and good
        results.append(detail)
        good, detail = _write_sysfs(hwmon / f"pwm{channel}", raw)
        ok = ok and good
        results.append(detail)

    return {
        "applied": ok,
        "requested_percent": percent,
        "applied_percent": target_pct,
        "pwm_raw": raw,
        "channels": targets,
        "detail": "; ".join(results),
        "clamp_notes": notes,
        "warnings": state.warnings,
    }


def set_automatic(channels: list[int] | None = None) -> dict[str, Any]:
    """Hand the fan back to the chip's own thermal cruise.

    This is the safe resting state and what rollback restores: whatever else
    fails, the board's built-in curve should be in charge rather than a duty
    this software pinned and then forgot about.
    """
    state = get_state()
    if not state.controllable:
        return {
            "applied": False,
            "detail": "fan control unavailable",
            "warnings": state.warnings,
        }

    hwmon = Path(state.hwmon_path or "")
    targets = channels or [c.channel for c in state.channels if c.writable]
    results, ok = [], True
    for channel in targets:
        good, detail = _write_sysfs(hwmon / f"pwm{channel}_enable", PWM_AUTOMATIC)
        ok = ok and good
        results.append(detail)

    return {
        "applied": ok,
        "mode": "automatic",
        "channels": targets,
        "detail": "; ".join(results),
        "warnings": state.warnings,
    }


def get_state_dict() -> dict[str, Any]:
    return get_state().to_dict()
