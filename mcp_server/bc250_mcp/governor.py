"""GPU governor abstraction over the two daemons found in the wild.

Two different governors manage BC-250 GPU voltage/frequency, and a given box
runs exactly one of them -- their systemd units declare a mutual ``Conflicts``.
They differ enough that a single code path would misrepresent both:

``oberon-governor``
    Exactly two operating points, read from ``/etc/oberon-config.yaml`` at
    startup into a ``static const`` array. Under load it jumps straight to the
    top point and steps down one at a time. There is no IPC of any kind, so
    changing anything means rewriting the file and restarting the unit -- and
    because the file is the state, a bad value survives reboot.

``cyan-skillfish-governor-smu``
    An arbitrary-length V/F curve plus a live D-Bus interface on the system bus.
    Frequency ramps continuously between curve points. Changes made over D-Bus
    are volatile, which makes a power cycle a guaranteed rollback and is why the
    optimizer prefers this backend when it is available.

This module is read-only (Phase 1). Writes land in Phase 2 on the same seam.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

OBERON_CONFIG = Path("/etc/oberon-config.yaml")
OBERON_UNIT = "oberon-governor.service"

CYAN_CONFIG = Path("/etc/cyan-skillfish-governor-smu/config.toml")
CYAN_UNIT = "cyan-skillfish-governor-smu.service"

DBUS_NAME = "com.cyanskillfish.Governor"
DBUS_ROOT = "/com/cyanskillfish/Governor"
DBUS_PERF_IFACE = f"{DBUS_NAME}.PerformanceMode"
DBUS_RANGE_IFACE = f"{DBUS_NAME}.Range"

_SUBPROCESS_TIMEOUT = 10


@dataclass
class OperatingPoint:
    frequency_mhz: int
    voltage_mv: int


@dataclass
class GovernorState:
    """A backend-neutral view of what the GPU governor is currently doing."""

    backend: str
    service: str
    active: bool
    enabled: bool
    config_path: str | None = None
    curve: list[OperatingPoint] = field(default_factory=list)
    min_freq_mhz: int | None = None
    max_freq_mhz: int | None = None
    allowed_min_mhz: int | None = None
    allowed_max_mhz: int | None = None
    temperature_throttle_c: int | None = None
    temperature_recovery_c: int | None = None
    load_target: tuple[float, float] | None = None
    supports_live_control: bool = False
    persistence: str = "config-file"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["curve"] = [asdict(point) for point in self.curve]
        return data


def _run(cmd: list[str]) -> tuple[int, str]:
    """Run a command, returning (rc, stdout). Never raises."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return proc.returncode, proc.stdout.strip()


def _unit_is(unit: str, check: str) -> bool:
    # `systemctl is-active` exits non-zero for inactive units, so the exit code
    # cannot distinguish "inactive" from "systemctl missing" -- match on stdout.
    _, out = _run(["systemctl", check, unit])
    return out.strip() in ("active", "enabled", "enabled-runtime", "static")


def _busctl_get(path: str, iface: str, prop: str) -> Any | None:
    """Read one D-Bus property via busctl, returning None if unavailable.

    busctl is used rather than a Python D-Bus binding to keep the server free of
    a native dependency; ``--json=short`` gives a stable machine-readable shape.
    """
    if shutil.which("busctl") is None:
        return None
    rc, out = _run(
        ["busctl", "--json=short", "get-property", DBUS_NAME, path, iface, prop]
    )
    if rc != 0 or not out:
        return None
    try:
        return json.loads(out).get("data")
    except (json.JSONDecodeError, AttributeError):
        return None


def _dbus_available() -> bool:
    if shutil.which("busctl") is None:
        return False
    rc, out = _run(["busctl", "list", "--no-pager"])
    return rc == 0 and DBUS_NAME in out


# --------------------------------------------------------------------------
# oberon-governor
# --------------------------------------------------------------------------

def _parse_oberon_config(raw: str) -> tuple[list[OperatingPoint], list[str]]:
    """Parse /etc/oberon-config.yaml into its two operating points.

    The schema is an oddity worth spelling out. oberon indexes the document
    positionally in C++::

        opps[0]["frequency"][0]["min"]   opps[1]["voltage"][0]["min"]
        opps[0]["frequency"][1]["max"]   opps[1]["voltage"][1]["max"]

    so ``opps`` is a two-element list whose first element carries frequency and
    second carries voltage, each a list of single-key mappings in a fixed
    order. Keys are read by position, not by name -- swapping min and max in the
    file silently swaps their meaning. We parse by name and validate ordering,
    which catches a hand-edited file that oberon itself would accept and then
    act on backwards.
    """
    warnings: list[str] = []
    try:
        import yaml
    except ImportError:
        return [], ["PyYAML not installed; cannot read oberon config"]

    try:
        doc = yaml.safe_load(raw)
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller as a warning
        return [], [f"oberon config is not valid YAML: {exc}"]

    if not isinstance(doc, dict) or "opps" not in doc:
        return [], ["oberon config has no 'opps' key"]

    def flatten(section: Any) -> dict[str, int]:
        """Collapse a list of single-key mappings into one dict."""
        out: dict[str, int] = {}
        if isinstance(section, dict):
            section = [section]
        if not isinstance(section, list):
            return out
        for item in section:
            if isinstance(item, dict):
                for key, value in item.items():
                    if isinstance(value, int):
                        out[key] = value
        return out

    freq: dict[str, int] = {}
    volt: dict[str, int] = {}
    entries = doc.get("opps")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if "frequency" in entry:
                freq = flatten(entry["frequency"])
            if "voltage" in entry:
                volt = flatten(entry["voltage"])

    if not freq or not volt:
        return [], ["oberon config missing frequency or voltage section"]

    missing = [
        name
        for name, table in (("frequency", freq), ("voltage", volt))
        for key in ("min", "max")
        if key not in table
    ]
    if missing:
        return [], [f"oberon config missing keys: {', '.join(sorted(set(missing)))}"]

    if freq["min"] > freq["max"]:
        warnings.append(
            f"oberon frequency min ({freq['min']}) exceeds max ({freq['max']}); "
            "oberon reads these positionally and would apply them as written"
        )
    if volt["min"] > volt["max"]:
        warnings.append(
            f"oberon voltage min ({volt['min']}) exceeds max ({volt['max']})"
        )

    return (
        [
            OperatingPoint(frequency_mhz=freq["min"], voltage_mv=volt["min"]),
            OperatingPoint(frequency_mhz=freq["max"], voltage_mv=volt["max"]),
        ],
        warnings,
    )


def _read_oberon() -> GovernorState:
    state = GovernorState(
        backend="oberon",
        service=OBERON_UNIT,
        active=_unit_is(OBERON_UNIT, "is-active"),
        enabled=_unit_is(OBERON_UNIT, "is-enabled"),
        config_path=str(OBERON_CONFIG),
        supports_live_control=False,
        persistence="config-file (survives reboot)",
    )

    try:
        raw = OBERON_CONFIG.read_text()
    except OSError as exc:
        state.warnings.append(f"cannot read {OBERON_CONFIG}: {exc}")
        return state

    curve, warnings = _parse_oberon_config(raw)
    state.curve = curve
    state.warnings.extend(warnings)
    if curve:
        state.min_freq_mhz = curve[0].frequency_mhz
        state.max_freq_mhz = curve[-1].frequency_mhz
    return state


# --------------------------------------------------------------------------
# cyan-skillfish-governor-smu
# --------------------------------------------------------------------------

def _read_cyan() -> GovernorState:
    live = _dbus_available()
    state = GovernorState(
        backend="cyan-skillfish",
        service=CYAN_UNIT,
        active=_unit_is(CYAN_UNIT, "is-active"),
        enabled=_unit_is(CYAN_UNIT, "is-enabled"),
        config_path=str(CYAN_CONFIG),
        supports_live_control=live,
        persistence="D-Bus changes are volatile; config.toml survives reboot",
    )

    if CYAN_CONFIG.exists():
        try:
            import tomllib

            doc = tomllib.loads(CYAN_CONFIG.read_text())
        except Exception as exc:  # noqa: BLE001
            state.warnings.append(f"cannot parse {CYAN_CONFIG}: {exc}")
            doc = {}

        points = doc.get("safe-points")
        if isinstance(points, list):
            parsed = [
                OperatingPoint(
                    frequency_mhz=int(p["frequency"]), voltage_mv=int(p["voltage"])
                )
                for p in points
                if isinstance(p, dict) and "frequency" in p and "voltage" in p
            ]
            # The daemon stores these in a BTreeMap keyed by frequency, so the
            # effective curve is sorted regardless of file order.
            state.curve = sorted(parsed, key=lambda p: p.frequency_mhz)

        temperature = doc.get("temperature", {})
        if isinstance(temperature, dict):
            state.temperature_throttle_c = temperature.get("throttling")
            state.temperature_recovery_c = temperature.get("throttling_recovery")

        freq_range = doc.get("frequency-range", {})
        if isinstance(freq_range, dict):
            state.min_freq_mhz = freq_range.get("min")
            state.max_freq_mhz = freq_range.get("max")
    else:
        state.warnings.append(f"{CYAN_CONFIG} not present")

    if not live:
        state.warnings.append(
            f"{DBUS_NAME} not on the system bus; live values unavailable "
            "(is the service running with dbus.enabled = true?)"
        )
        return state

    # Live values win over the config file: the config is only the startup
    # state, and anything set over D-Bus since boot will differ.
    current_min = _busctl_get(f"{DBUS_ROOT}/Range/Current", DBUS_RANGE_IFACE, "Min")
    current_max = _busctl_get(f"{DBUS_ROOT}/Range/Current", DBUS_RANGE_IFACE, "Max")
    if current_min is not None:
        state.min_freq_mhz = current_min
    if current_max is not None:
        state.max_freq_mhz = current_max

    state.allowed_min_mhz = _busctl_get(
        f"{DBUS_ROOT}/Range/Allowed", DBUS_RANGE_IFACE, "Min"
    )
    state.allowed_max_mhz = _busctl_get(
        f"{DBUS_ROOT}/Range/Allowed", DBUS_RANGE_IFACE, "Max"
    )

    throttle = _busctl_get(DBUS_ROOT, DBUS_PERF_IFACE, "TemperatureThrottling")
    recovery = _busctl_get(DBUS_ROOT, DBUS_PERF_IFACE, "TemperatureRecovery")
    if throttle is not None:
        state.temperature_throttle_c = throttle
    if recovery is not None:
        state.temperature_recovery_c = recovery

    load_min = _busctl_get(DBUS_ROOT, DBUS_PERF_IFACE, "LoadTargetMin")
    load_max = _busctl_get(DBUS_ROOT, DBUS_PERF_IFACE, "LoadTargetMax")
    if load_min is not None and load_max is not None:
        state.load_target = (load_min, load_max)

    return state


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

def detect_backend() -> str | None:
    """Identify the governor in charge.

    A running service wins over a merely installed one, since both can be
    present at once on a box mid-migration between them.
    """
    if _unit_is(CYAN_UNIT, "is-active"):
        return "cyan-skillfish"
    if _unit_is(OBERON_UNIT, "is-active"):
        return "oberon"
    if CYAN_CONFIG.exists():
        return "cyan-skillfish"
    if OBERON_CONFIG.exists():
        return "oberon"
    return None


def get_state(backend: str | None = None) -> GovernorState:
    """Read the current governor state, auto-detecting the backend."""
    backend = backend or detect_backend()
    if backend == "cyan-skillfish":
        return _read_cyan()
    if backend == "oberon":
        return _read_oberon()
    return GovernorState(
        backend="none",
        service="-",
        active=False,
        enabled=False,
        warnings=[
            (
                "no GPU governor detected (neither oberon-governor nor "
                "cyan-skillfish-governor-smu is installed or running)"
            )
        ],
    )


def get_state_dict(backend: str | None = None) -> dict[str, Any]:
    return get_state(backend).to_dict()
