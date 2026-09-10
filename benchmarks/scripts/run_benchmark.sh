#!/usr/bin/env bash
# run_benchmark.sh <config_label> <duration_s> [width] [height]
#
# Standalone CLI wrapper around bc250_mcp.benchmark. The real implementation
# lives in Python so the MCP server and this script cannot drift apart -- the
# harness samples telemetry through the run and aborts on an over-temperature,
# which a shell loop cannot do as reliably.
#
# Appends one row to benchmarks/results.jsonl and writes
# benchmarks/raw/<run_id>/ containing the FurMark log, per-second telemetry
# samples, and a screenshot of the score box.
set -euo pipefail

LABEL="${1:?usage: run_benchmark.sh <config_label> <duration_s> [width] [height]}"
DURATION="${2:?usage: run_benchmark.sh <config_label> <duration_s> [width] [height]}"
WIDTH="${3:-1920}"
HEIGHT="${4:-1080}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

# Prefer the project venv; the MCP SDK and PyYAML live there.
PYTHON="${BC250_PYTHON:-}"
if [ -z "$PYTHON" ]; then
    for candidate in "$REPO/.venv/bin/python" "$HOME/bc250-autotune/.venv/bin/python" python3; do
        if command -v "$candidate" >/dev/null 2>&1 || [ -x "$candidate" ]; then
            PYTHON="$candidate"; break
        fi
    done
fi

export PYTHONPATH="$REPO/mcp_server${PYTHONPATH:+:$PYTHONPATH}"

exec "$PYTHON" - "$LABEL" "$DURATION" "$WIDTH" "$HEIGHT" <<'PYEOF'
import json
import sys

from bc250_mcp import benchmark

label, duration, width, height = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])

report = benchmark.environment_report()
if not report["ready"]:
    print("cannot run a benchmark:", file=sys.stderr)
    for problem in report["problems"]:
        print(f"  - {problem}", file=sys.stderr)
    sys.exit(1)

print(f"FurMark : {report['furmark_dir']}")
print(f"display : {report['display']}")
print(f"running : {label} for {duration}s at {width}x{height}\n")

try:
    result = benchmark.run(label, duration, width=width, height=height)
except benchmark.BenchmarkError as exc:
    print(f"benchmark failed: {exc}", file=sys.stderr)
    sys.exit(1)

print(f"run_id        {result.run_id}")
print(f"completed     {result.completed}   aborted={result.aborted}")
if result.abort_reason:
    print(f"abort reason  {result.abort_reason}")
print(f"score(frames) {result.score_frames}")
print(f"fps           avg={result.avg_fps} min={result.min_fps} max={result.max_fps}")
print(f"gpu temp      avg={result.avg_temp_c}C max={result.max_temp_c}C")
print(f"power         avg={result.avg_power_w}W max={result.max_power_w}W")
print(f"gpu clock     avg={result.avg_clock_mhz}MHz")
print(f"throttling    {result.throttle_flags_seen or 'none observed'}")
print(f"screenshot    {result.screenshot_path}")
for warning in result.warnings:
    print(f"WARNING       {warning}")

sys.exit(2 if result.aborted else 0)
PYEOF
