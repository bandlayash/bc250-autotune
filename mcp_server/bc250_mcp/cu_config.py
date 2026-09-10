"""Compute-unit (CU/WGP) configuration -- read-only.

The BC-250 boots with a factory topology of 24 CUs (12 WGPs of 2 CUs each) and
can route all 20 WGPs for 40 CUs. There are two very different ways that
happens, and which one a box uses changes whether tuning has to care:

Kernel-side
    A BC-250-patched amdgpu (kernels tagged ``bc250cu``) takes
    ``amdgpu.bc250_cc_write_mode=`` and rewrites the CC/SPI masks during probe.
    This is persistent, applied before userspace, and needs no tooling. A box
    configured this way reports ``active_cu_number 40`` at boot and there is
    nothing for us to do.

Userspace
    ``bc250-cu-live-manager`` routes WGPs at runtime through ``umr``. Volatile
    unless the table is saved and its boot service installed.

We only report. Changing CU topology mid-session would invalidate every
benchmark taken before it, so it is out of scope for automated tuning -- see
docs/DECISIONS.md.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

FACTORY_CU_COUNT = 24
FULL_CU_COUNT = 40
CUS_PER_WGP = 2

_CU_LINE = re.compile(
    r"SE\s+(\d+),\s*SH per SE\s+(\d+),\s*CU per SH\s+(\d+),\s*active_cu_number\s+(\d+)"
)
_KARG = re.compile(r"amdgpu\.bc250_cc_write_mode=(\S+)")


@dataclass
class CuConfig:
    active_cu_count: int | None = None
    shader_engines: int | None = None
    sh_per_se: int | None = None
    cu_per_sh: int | None = None

    mechanism: str = "unknown"
    kernel_write_mode: str | None = None
    kernel_release: str | None = None
    live_manager_available: bool = False
    live_manager_config_present: bool = False

    warnings: list[str] = field(default_factory=list)

    @property
    def layout(self) -> str:
        if self.active_cu_count is None:
            return "unknown"
        if self.active_cu_count >= FULL_CU_COUNT:
            return "full (40 CU)"
        if self.active_cu_count <= FACTORY_CU_COUNT:
            return "factory (24 CU)"
        return f"custom ({self.active_cu_count} CU)"

    @property
    def active_wgp_count(self) -> int | None:
        if self.active_cu_count is None:
            return None
        return self.active_cu_count // CUS_PER_WGP

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["layout"] = self.layout
        data["active_wgp_count"] = self.active_wgp_count
        return data


def _read_kernel_log() -> str:
    """Read the kernel ring buffer, escalating to sudo only if needed.

    Many distros set kernel.dmesg_restrict=1, which makes an unprivileged dmesg
    return nothing rather than fail, so an empty result is retried with sudo -n.
    """
    for cmd in (["dmesg"], ["sudo", "-n", "dmesg"]):
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10, check=False
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout
    return ""


def get_config() -> CuConfig:
    config = CuConfig()

    try:
        config.kernel_release = Path("/proc/sys/kernel/osrelease").read_text().strip()
    except OSError:
        pass

    try:
        cmdline = Path("/proc/cmdline").read_text()
        match = _KARG.search(cmdline)
        if match:
            config.kernel_write_mode = match.group(1)
    except OSError:
        pass

    log = _read_kernel_log()
    if log:
        # Take the last match: the driver can rebind, and the most recent line
        # reflects the topology actually in force.
        for match in _CU_LINE.finditer(log):
            config.shader_engines = int(match.group(1))
            config.sh_per_se = int(match.group(2))
            config.cu_per_sh = int(match.group(3))
            config.active_cu_count = int(match.group(4))
    else:
        config.warnings.append(
            "could not read the kernel log (dmesg_restrict?); CU count unknown"
        )

    config.live_manager_available = (
        shutil.which("bc250-cu-live-manager") is not None
        or Path("/usr/local/bin/bc250-cu-live-manager").exists()
        or Path("/var/usrlocal/bin/bc250-cu-live-manager").exists()
    )
    config.live_manager_config_present = Path(
        "/etc/bc250-cu-live-manager.conf"
    ).exists()

    if config.kernel_write_mode is not None:
        config.mechanism = "kernel (amdgpu.bc250_cc_write_mode)"
    elif config.live_manager_config_present:
        config.mechanism = "userspace (bc250-cu-live-manager boot service)"
    elif config.active_cu_count and config.active_cu_count > FACTORY_CU_COUNT:
        config.mechanism = "userspace (live routing, volatile)"
        config.warnings.append(
            "more than 24 CUs are active but no persistence mechanism was found; "
            "this routing will be lost on reboot"
        )
    elif config.active_cu_count:
        config.mechanism = "factory (no unlock applied)"

    return config


def get_config_dict() -> dict[str, Any]:
    return get_config().to_dict()
