"""FurMark benchmark harness.

Runs a timed FurMark pass, samples telemetry throughout, captures a screenshot
of the score box, and records one row per run to ``results.jsonl``.

Three things about FurMark on this platform are worked around here rather than
left for the caller to rediscover, each verified on real hardware:

**It segfaults without an X resource-manager string.** A freshly started
XWayland display has an empty RM property, and FurMark's DPI probe passes it
straight to ``strlen``::

    X11_GetMonitorDPI -> XrmGetStringDatabase -> GetDatabase -> __strlen_avx2

so ``xrdb -merge`` is seeded before every launch. Unrelated to ``--no-score-box``;
it crashes identically either way.

**It does not reliably exit at ``--max-time``.** A 30 s request overshot an 80 s
wall clock. The harness imposes its own deadline and kills the process, which is
also what the operator's own script does.

**Its results land in ``_scores_maxtime.csv``** inside the FurMark directory,
already parsed and complete -- no ``--export-dir`` needed. The row is appended
when the timed demo finishes, before the process gets around to exiting, so a
killed run still records its result. New rows are detected by counting, since
the file accumulates across runs.

Safety: telemetry is sampled continuously and the run is **aborted** if the GPU
crosses ``benchmark.abort_gpu_temp_c``. The test unit reaches 90-95 C under
FurMark while the SMU reports no throttle flags at all, so this ceiling is the
only thing that stops a run cooking the board.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import envelope, governor, telemetry

BENCH_ROOT_ENV = "BC250_BENCH_ROOT"
FURMARK_DIR_ENV = "BC250_FURMARK_DIR"
DISPLAY_ENV = "BC250_BENCH_DISPLAY"

SCORES_CSV = "_scores_maxtime.csv"
RESULTS_FILE = "results.jsonl"

DEFAULT_DEMO = "furmark-gl"
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080

# FurMark overshoots its own --max-time; kill it this long after the deadline.
KILL_GRACE_S = 25

# Start every run from a comparable thermal state.
#
# This is not politeness to the hardware, it is measurement validity. Runs
# started back-to-back are not comparable: the test unit's first run began at
# 55 C and took 38 s to reach 92 C, while the next began already hot and
# tripped the abort far sooner. The operator's own historical runs, started
# from cold, completed full 600 s passes peaking at 78-86 C -- the same box,
# the same config, a completely different outcome purely from starting
# temperature.
#
# Without this gate, a tuning session would attribute thermal drift to the
# config change it just made.
COOLDOWN_TARGET_C = 65.0
COOLDOWN_TIMEOUT_S = 600
COOLDOWN_POLL_S = 5.0
# Seconds before the end to grab the screenshot, so the score box is populated
# but the window has not torn down.
SCREENSHOT_LEAD_S = 2.0
SAMPLE_INTERVAL_S = 1.0

CANDIDATE_FURMARK_DIRS = (
    Path.home() / "furmark/FurMark_linux64",
    Path("/opt/furmark/FurMark_linux64"),
    Path.home() / "FurMark_linux64",
)


class BenchmarkError(RuntimeError):
    """Raised when a benchmark cannot be started at all."""


def bench_root() -> Path:
    override = os.environ.get(BENCH_ROOT_ENV)
    if override:
        return Path(override)
    # Repo layout: mcp_server/bc250_mcp/benchmark.py -> <repo>/benchmarks
    return Path(__file__).resolve().parent.parent.parent / "benchmarks"


def results_path() -> Path:
    return bench_root() / RESULTS_FILE


def find_furmark() -> Path | None:
    override = os.environ.get(FURMARK_DIR_ENV)
    if override:
        path = Path(override)
        return path if (path / "furmark").exists() else None
    for candidate in CANDIDATE_FURMARK_DIRS:
        if (candidate / "furmark").exists():
            return candidate
    which = shutil.which("furmark")
    return Path(which).parent if which else None


def detect_display() -> str | None:
    """Find an X display to render on.

    SSH sessions land on a tty with no DISPLAY, but the box runs a gamescope
    session whose XWayland servers are reachable. Probe them rather than
    assuming :0, since gamescope is launched with --xwayland-count 2 and which
    one carries the session varies.
    """
    override = os.environ.get(DISPLAY_ENV)
    if override:
        return override
    if os.environ.get("DISPLAY"):
        return os.environ["DISPLAY"]
    if shutil.which("xdpyinfo") is None:
        return ":0"
    for display in (":1", ":0", ":2"):
        try:
            proc = subprocess.run(
                ["xdpyinfo"],
                env={**os.environ, "DISPLAY": display},
                capture_output=True,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return display
    return None


def _launch_env(display: str) -> dict[str, str]:
    env = dict(os.environ)
    env["DISPLAY"] = display
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}" if hasattr(os, "getuid") else "")
    return env


def seed_xresources(display: str) -> tuple[bool, str]:
    """Populate the X resource-manager string so FurMark's DPI probe survives.

    Without this the RM string is empty on a fresh XWayland display and FurMark
    dereferences NULL in strlen. This is the single most important line in the
    harness; skipping it turns every run into a SIGSEGV.
    """
    if shutil.which("xrdb") is None:
        return False, "xrdb not available; FurMark will likely segfault"
    try:
        proc = subprocess.run(
            ["xrdb", "-merge"],
            input="Xft.dpi: 96\n",
            text=True,
            env=_launch_env(display),
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"xrdb failed: {exc}"
    if proc.returncode != 0:
        return False, f"xrdb returned {proc.returncode}: {proc.stderr.strip()}"
    return True, "seeded Xft.dpi into the X resource database"


@dataclass
class Sample:
    t: float
    gpu_temp_c: float | None
    gpu_power_w: float | None
    socket_power_w: float | None
    gpu_clock_mhz: int | None
    cpu_temp_c: float | None
    cpu_vid_mv: int | None
    fan_rpm: int | None
    throttle_flags: list[str]


@dataclass
class BenchmarkResult:
    run_id: str
    config_label: str
    timestamp: float = 0.0
    iso_time: str = ""
    duration_s: int = 0
    demo: str = DEFAULT_DEMO
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT

    completed: bool = False
    aborted: bool = False
    start_temp_c: float | None = None
    cooldown_waited_s: float = 0.0
    cold_start: bool = False
    abort_reason: str | None = None
    furmark_exit_code: int | None = None

    # From FurMark's own results CSV.
    score_frames: int | None = None
    avg_fps: float | None = None
    min_fps: float | None = None
    max_fps: float | None = None
    furmark_max_gpu_temp_c: float | None = None

    # Derived from our telemetry sampling.
    sample_count: int = 0
    avg_temp_c: float | None = None
    max_temp_c: float | None = None
    avg_power_w: float | None = None
    max_power_w: float | None = None
    avg_clock_mhz: float | None = None
    max_cpu_vid_mv: int | None = None
    max_fan_rpm: int | None = None
    throttle_flags_seen: list[str] = field(default_factory=list)
    thermally_throttled: bool = False

    # Config this run measured.
    governor_backend: str | None = None
    gpu_range_mhz: dict[str, int | None] = field(default_factory=dict)

    screenshot_path: str | None = None
    samples_path: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _summarise(samples: list[Sample], result: BenchmarkResult) -> None:
    """Fold telemetry samples into the result.

    Ignores the first two samples: FurMark spends the opening moments creating
    its window and compiling shaders, and including that idle period drags the
    averages down in a way that varies with how fast the machine happens to
    start up.
    """
    result.sample_count = len(samples)
    body = samples[2:] if len(samples) > 4 else samples
    if not body:
        return

    def values(attr: str) -> list[float]:
        return [
            getattr(s, attr) for s in body if getattr(s, attr) is not None
        ]

    temps = values("gpu_temp_c")
    if temps:
        result.avg_temp_c = round(sum(temps) / len(temps), 2)
        result.max_temp_c = round(max(temps), 2)

    # hwmon power1_average, NOT the SMU's average_socket_power.
    #
    # Measured across a 60 s FurMark run on the test unit:
    #
    #     socket_power_w (gpu_metrics @40):  49 W at idle, then 0.88-9.65 W
    #                                        for the entire loaded portion
    #     gpu_power_w    (hwmon):            46 W at idle, 129-135 W loaded
    #
    # The SMU field looks right at idle and then collapses under load, the same
    # failure mode as cur_gfxclk. 130 W at 92 C is the believable figure, so
    # hwmon is authoritative and the SMU value is only a fallback.
    powers = [p for p in values("gpu_power_w") if p > 0]
    if not powers:
        powers = [p for p in values("socket_power_w") if p > 0]
    if powers:
        result.avg_power_w = round(sum(powers) / len(powers), 2)
        result.max_power_w = round(max(powers), 2)

    clocks = [c for c in values("gpu_clock_mhz") if c > 0]
    if clocks:
        result.avg_clock_mhz = round(sum(clocks) / len(clocks), 1)

    vids = values("cpu_vid_mv")
    if vids:
        result.max_cpu_vid_mv = int(max(vids))

    fans = [f for f in values("fan_rpm") if f > 0]
    if fans:
        result.max_fan_rpm = int(max(fans))

    seen = sorted({flag for s in body for flag in s.throttle_flags})
    result.throttle_flags_seen = seen
    from .gpu_metrics import THERMAL_THROTTLERS

    result.thermally_throttled = any(f in THERMAL_THROTTLERS for f in seen)


class _Sampler(threading.Thread):
    """Samples telemetry during a run and signals an abort if it gets too hot."""

    def __init__(self, abort_gpu_c: float, abort_cpu_c: float) -> None:
        super().__init__(daemon=True)
        self.samples: list[Sample] = []
        self.abort_gpu_c = abort_gpu_c
        self.abort_cpu_c = abort_cpu_c
        self.abort_reason: str | None = None
        self._stop = threading.Event()

    def run(self) -> None:
        start = time.time()
        while not self._stop.is_set():
            reading = telemetry.collect()
            self.samples.append(
                Sample(
                    t=round(time.time() - start, 2),
                    gpu_temp_c=reading.gpu_temp_c,
                    gpu_power_w=reading.gpu_power_w,
                    socket_power_w=reading.socket_power_w,
                    gpu_clock_mhz=reading.gpu_clock_mhz,
                    cpu_temp_c=reading.cpu_temp_c,
                    cpu_vid_mv=reading.cpu_vid_mv,
                    fan_rpm=reading.fans[0].rpm if reading.fans else None,
                    throttle_flags=list(reading.throttle_flags),
                )
            )

            if reading.gpu_temp_c is not None and reading.gpu_temp_c >= self.abort_gpu_c:
                self.abort_reason = (
                    f"GPU reached {reading.gpu_temp_c} C, at or above the "
                    f"{self.abort_gpu_c} C abort threshold"
                )
                return
            if reading.cpu_temp_c is not None and reading.cpu_temp_c >= self.abort_cpu_c:
                self.abort_reason = (
                    f"CPU reached {reading.cpu_temp_c} C, at or above the "
                    f"{self.abort_cpu_c} C abort threshold"
                )
                return

            self._stop.wait(SAMPLE_INTERVAL_S)

    def stop(self) -> None:
        self._stop.set()


def capture_screenshot(display: str, target: Path) -> tuple[bool, str]:
    """Grab the screen, preferring whatever tool the session actually has.

    The test box is a gamescope session reachable through XWayland, where
    ``grim`` and ``scrot`` are absent but ImageMagick ``import`` is present, so
    the order below is not arbitrary.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    env = _launch_env(display)

    attempts: list[tuple[str, list[str]]] = []
    if shutil.which("import"):
        attempts.append(("import", ["import", "-window", "root", str(target)]))
    if shutil.which("grim") and os.environ.get("WAYLAND_DISPLAY"):
        attempts.append(("grim", ["grim", str(target)]))
    if shutil.which("scrot"):
        attempts.append(("scrot", ["scrot", "-o", str(target)]))
    if shutil.which("spectacle"):
        attempts.append(("spectacle", ["spectacle", "-b", "-n", "-o", str(target)]))

    if not attempts:
        return False, "no screenshot tool found (tried import, grim, scrot, spectacle)"

    errors = []
    for name, cmd in attempts:
        try:
            proc = subprocess.run(
                cmd, env=env, capture_output=True, timeout=30, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"{name}: {exc}")
            continue
        if proc.returncode == 0 and target.exists() and target.stat().st_size > 0:
            return True, f"captured with {name} ({target.stat().st_size} bytes)"
        errors.append(f"{name}: rc={proc.returncode} {proc.stderr.decode()[:80]}")
    return False, "; ".join(errors)


# The trailing columns of a FurMark results row, in order. Everything before
# these is descriptive text of unpredictable width -- see parse_scores_row.
TRAILING_COLUMNS = (
    "width", "height", "fullscreen", "antialiasing", "max_time",
    "frames", "max_gpu_temp", "avg_fps", "min_fps", "max_fps",
)


def wait_for_cooldown(
    target_c: float = COOLDOWN_TARGET_C,
    timeout_s: float = COOLDOWN_TIMEOUT_S,
) -> dict[str, Any]:
    """Block until the GPU cools to ``target_c``, or the timeout elapses.

    Returns a report rather than raising on timeout: a box whose idle
    temperature is simply above the target (poor case airflow, hot room) should
    still be benchmarkable, just with a recorded caveat so the numbers are not
    silently compared against runs that started cooler.
    """
    started = time.time()
    first = telemetry.collect().gpu_temp_c
    if first is None:
        return {
            "waited_s": 0.0,
            "start_temp_c": None,
            "reached_target": False,
            "note": "no GPU temperature available; cannot verify a cold start",
        }
    if first <= target_c:
        return {
            "waited_s": 0.0,
            "start_temp_c": first,
            "reached_target": True,
            "note": f"already at {first} C",
        }

    current = first
    while time.time() - started < timeout_s:
        time.sleep(COOLDOWN_POLL_S)
        current = telemetry.collect().gpu_temp_c
        if current is None:
            break
        if current <= target_c:
            return {
                "waited_s": round(time.time() - started, 1),
                "start_temp_c": current,
                "reached_target": True,
                "note": f"cooled from {first} C to {current} C",
            }

    return {
        "waited_s": round(time.time() - started, 1),
        "start_temp_c": current,
        "reached_target": False,
        "note": (
            f"did not reach {target_c} C within {timeout_s:.0f}s (still "
            f"{current} C); results are not comparable with runs started colder"
        ),
    }


def _read_scores_rows(path: Path) -> list[str]:
    """Return the data lines of the scores CSV, header excluded."""
    try:
        lines = [
            line.strip()
            for line in path.read_text(errors="replace").splitlines()
            if line.strip()
        ]
    except OSError:
        return []
    # Drop the header if present.
    return [line for line in lines if not line.startswith("date,")]


def parse_scores_row(line: str) -> dict[str, str]:
    """Parse one FurMark results line, counting from the END.

    FurMark writes this file **unquoted**, and its ``renderer`` field contains
    commas of its own::

        ...,AMD,AMD BC-250 (radeonsi, gfx1013, ACO, DRM 3.64, 6.17.7-...),...

    so a real row has 20 comma-separated fields against a 16-column header.
    Parsing by header position therefore mis-maps every numeric field -- it read
    ``max_time`` as 1920 (the width) and ``frames`` as 1080 (the height).
    ``csv.DictReader`` does not help: the file is not quoted, so there is
    nothing for it to disambiguate.

    The descriptive prefix is variable-width but the numeric tail is not, so the
    last ten fields are mapped positionally. That holds no matter how many
    commas the driver decides to put in its renderer string.
    """
    fields = [field.strip() for field in line.split(",")]
    if len(fields) < len(TRAILING_COLUMNS):
        return {}
    tail = fields[-len(TRAILING_COLUMNS):]
    return dict(zip(TRAILING_COLUMNS, tail))


def _apply_furmark_row(row: dict[str, str], result: BenchmarkResult) -> None:
    """Fold FurMark's own result row into ours.

    Note ``frames`` is the headline "score"; ``avg_fps`` is a separate column
    and is not simply score/duration.
    """
    def number(key: str, cast=float):
        raw = (row.get(key) or "").strip()
        try:
            return cast(raw)
        except (TypeError, ValueError):
            return None

    result.score_frames = number("frames", int)
    result.avg_fps = number("avg_fps")
    result.min_fps = number("min_fps")
    result.max_fps = number("max_fps")
    result.furmark_max_gpu_temp_c = number("max_gpu_temp")


def run(
    config_label: str,
    duration_s: int,
    demo: str = DEFAULT_DEMO,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    screenshot: bool = True,
    cooldown: bool = True,
) -> BenchmarkResult:
    """Run one benchmark pass and record it. Blocks for roughly ``duration_s``."""
    doc = envelope.load()
    bench = doc.get("benchmark", {})
    max_duration = int(bench.get("max_duration_s", 1800))
    if duration_s <= 0:
        raise BenchmarkError("duration_s must be positive")
    if duration_s > max_duration:
        raise BenchmarkError(
            f"duration_s {duration_s} exceeds the envelope's max_duration_s "
            f"({max_duration}); a malformed duration must not pin the GPU at "
            "full load indefinitely"
        )

    furmark_dir = find_furmark()
    if furmark_dir is None:
        raise BenchmarkError(
            "FurMark not found. Set BC250_FURMARK_DIR to the directory "
            "containing the `furmark` binary."
        )

    display = detect_display()
    if display is None:
        raise BenchmarkError(
            "no reachable X display; FurMark needs one even when driven over SSH"
        )

    now = time.time()
    run_id = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-{config_label}"
    raw_dir = bench_root() / "raw" / run_id
    raw_dir.mkdir(parents=True, exist_ok=True)

    state = governor.get_state()
    result = BenchmarkResult(
        run_id=run_id,
        config_label=config_label,
        timestamp=round(now, 3),
        iso_time=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
        duration_s=duration_s,
        demo=demo,
        width=width,
        height=height,
        governor_backend=state.backend,
        gpu_range_mhz={"min": state.min_freq_mhz, "max": state.max_freq_mhz},
    )

    if cooldown:
        report = wait_for_cooldown()
        result.start_temp_c = report["start_temp_c"]
        result.cooldown_waited_s = report["waited_s"]
        result.cold_start = report["reached_target"]
        if not report["reached_target"]:
            result.warnings.append(f"cooldown: {report['note']}")
    else:
        result.start_temp_c = telemetry.collect().gpu_temp_c
        result.warnings.append(
            "cooldown skipped; this run is not comparable with cold-start runs"
        )

    seeded, detail = seed_xresources(display)
    if not seeded:
        result.warnings.append(detail)

    scores_csv = furmark_dir / SCORES_CSV
    rows_before = len(_read_scores_rows(scores_csv))

    sampler = _Sampler(
        abort_gpu_c=float(bench.get("abort_gpu_temp_c", 92)),
        abort_cpu_c=float(bench.get("abort_cpu_temp_c", 95)),
    )
    sampler.start()

    # Score box left enabled on purpose: it is what the screenshot captures.
    cmd = [
        "./furmark",
        "--demo", demo,
        "--width", str(width),
        "--height", str(height),
        "--max-time", str(duration_s * 1000),
    ]
    log_path = raw_dir / "furmark.log"

    try:
        with open(log_path, "wb") as log_handle:
            proc = subprocess.Popen(
                cmd,
                cwd=str(furmark_dir),
                env=_launch_env(display),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
    except OSError as exc:
        sampler.stop()
        raise BenchmarkError(f"could not launch FurMark: {exc}") from exc

    deadline = time.time() + duration_s
    shot_at = deadline - SCREENSHOT_LEAD_S
    shot_taken = not screenshot

    while True:
        if proc.poll() is not None:
            break
        if sampler.abort_reason:
            result.aborted = True
            result.abort_reason = sampler.abort_reason
            proc.terminate()
            break
        if not shot_taken and time.time() >= shot_at:
            ok, detail = capture_screenshot(display, raw_dir / "screenshot.png")
            shot_taken = True
            if ok:
                result.screenshot_path = str(
                    (raw_dir / "screenshot.png").relative_to(bench_root().parent)
                )
            else:
                result.warnings.append(f"screenshot failed: {detail}")
        if time.time() >= deadline + KILL_GRACE_S:
            # Expected: FurMark routinely overshoots its own --max-time.
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            break
        time.sleep(0.25)

    try:
        result.furmark_exit_code = proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        result.furmark_exit_code = None

    sampler.stop()
    sampler.join(timeout=5)

    _summarise(sampler.samples, result)

    samples_path = raw_dir / "samples.jsonl"
    try:
        with open(samples_path, "w") as handle:
            for sample in sampler.samples:
                handle.write(json.dumps(asdict(sample)) + "\n")
        result.samples_path = str(samples_path.relative_to(bench_root().parent))
    except OSError as exc:
        result.warnings.append(f"could not write samples: {exc}")

    # FurMark appends its row when the timed demo finishes, which happens before
    # the process gets around to exiting -- so a killed run still has a result.
    rows_after = _read_scores_rows(scores_csv)
    if len(rows_after) > rows_before:
        _apply_furmark_row(parse_scores_row(rows_after[-1]), result)
        result.completed = not result.aborted
    else:
        result.warnings.append(
            f"FurMark wrote no new row to {SCORES_CSV}; the run did not complete "
            "(check furmark.log -- a SIGSEGV here usually means the X resource "
            "database was empty)"
        )

    if result.aborted:
        result.warnings.append(f"ABORTED: {result.abort_reason}")

    _append_result(result)
    return result


def _append_result(result: BenchmarkResult) -> None:
    path = results_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as handle:
            handle.write(json.dumps(result.to_dict()) + "\n")
    except OSError as exc:
        result.warnings.append(f"could not append to {path}: {exc}")


def load_results() -> list[dict[str, Any]]:
    path = results_path()
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return out
    return out


def get_result(run_id: str) -> dict[str, Any] | None:
    for row in reversed(load_results()):
        if row.get("run_id") == run_id:
            return row
    return None


def environment_report() -> dict[str, Any]:
    """Report whether a benchmark could run right now, and why not if not."""
    furmark_dir = find_furmark()
    display = detect_display()
    problems: list[str] = []
    if furmark_dir is None:
        problems.append("FurMark binary not found (set BC250_FURMARK_DIR)")
    if display is None:
        problems.append("no reachable X display")
    if shutil.which("xrdb") is None:
        problems.append("xrdb missing; FurMark will segfault on a fresh display")
    if not any(shutil.which(t) for t in ("import", "grim", "scrot", "spectacle")):
        problems.append("no screenshot tool available")

    return {
        "furmark_dir": str(furmark_dir) if furmark_dir else None,
        "display": display,
        "bench_root": str(bench_root()),
        "results_file": str(results_path()),
        "ready": not problems,
        "problems": problems,
    }
