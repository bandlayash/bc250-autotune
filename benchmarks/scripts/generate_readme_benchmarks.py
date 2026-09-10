#!/usr/bin/env python3
"""Regenerate the README's benchmark section from results.jsonl.

Idempotent: rewrites everything between the marker comments, so it is safe to
run after every tuning session. Running it twice in a row produces an identical
file, which is what makes it safe to wire into an automated loop.

Only **completed** runs are tabulated. An aborted run has no FurMark score --
it was killed before the demo finished -- so publishing it as a benchmark
result would be presenting a thermal failure as a performance measurement.
Aborted runs are still summarised separately, because "this config could not
finish a run" is a real and useful finding.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

START_MARKER = "<!-- BENCHMARKS:START -->"
END_MARKER = "<!-- BENCHMARKS:END -->"

BASELINE_LABELS = ("stock", "baseline")


def load_results(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def pick_rows(rows: list[dict[str, Any]]) -> tuple[dict | None, dict | None]:
    """Return (baseline, tuned): the latest completed run of each kind.

    "Latest" rather than "best" on purpose. The README should reflect the
    configuration the machine is actually running now, not the best number ever
    recorded -- a table showing a tune that was later rolled back would be
    quietly false.
    """
    completed = [r for r in rows if r.get("completed") and r.get("score_frames")]
    baseline = next(
        (
            r for r in reversed(completed)
            if str(r.get("config_label", "")).lower().startswith(BASELINE_LABELS)
        ),
        None,
    )
    tuned = next(
        (
            r for r in reversed(completed)
            if not str(r.get("config_label", "")).lower().startswith(BASELINE_LABELS)
        ),
        None,
    )
    return baseline, tuned


def _fmt(value: Any, suffix: str = "", precision: int | None = None) -> str:
    if value is None:
        return "—"
    if precision is not None and isinstance(value, (int, float)):
        return f"{value:.{precision}f}{suffix}"
    return f"{value}{suffix}"


def _delta(tuned: Any, base: Any, suffix: str = "", invert: bool = False) -> str:
    """Format a change, signed and as a percentage.

    ``invert`` marks metrics where lower is better (temperature, power), so the
    arrow reflects whether the change was an improvement rather than merely
    whether the number went up.
    """
    if tuned is None or base is None or base == 0:
        return "—"
    diff = tuned - base
    pct = diff / base * 100.0
    better = (diff < 0) if invert else (diff > 0)
    arrow = "▲" if diff > 0 else ("▼" if diff < 0 else "—")
    mark = "" if diff == 0 else (" ✓" if better else "")
    return f"{arrow} {diff:+.1f}{suffix} ({pct:+.1f}%){mark}"


def _row_config(row: dict[str, Any]) -> str:
    rng = row.get("gpu_range_mhz") or {}
    low, high = rng.get("min"), rng.get("max")
    if low is None or high is None:
        return "—"
    return f"{low}–{high} MHz"


def build_section(rows: list[dict[str, Any]]) -> str:
    baseline, tuned = pick_rows(rows)
    completed = [r for r in rows if r.get("completed")]
    aborted = [r for r in rows if r.get("aborted")]

    if not completed:
        lines = [
            "_No completed benchmark runs recorded yet._",
            "",
        ]
        if aborted:
            lines += [
                f"{len(aborted)} run(s) were aborted before completing. The most "
                "recent:",
                "",
                f"> {aborted[-1].get('abort_reason', 'unknown reason')}",
                "",
                "An aborted run has no score, so nothing is tabulated above.",
            ]
        return "\n".join(lines)

    parts: list[str] = []

    if baseline and tuned:
        parts += [
            "| Metric | Stock | Tuned | Change |",
            "|---|---:|---:|---|",
            f"| GPU range | {_row_config(baseline)} | {_row_config(tuned)} | |",
            f"| Score (frames) | {_fmt(baseline.get('score_frames'))} | "
            f"{_fmt(tuned.get('score_frames'))} | "
            f"{_delta(tuned.get('score_frames'), baseline.get('score_frames'))} |",
            f"| Avg FPS | {_fmt(baseline.get('avg_fps'))} | {_fmt(tuned.get('avg_fps'))} | "
            f"{_delta(tuned.get('avg_fps'), baseline.get('avg_fps'))} |",
            f"| Min FPS | {_fmt(baseline.get('min_fps'))} | {_fmt(tuned.get('min_fps'))} | "
            f"{_delta(tuned.get('min_fps'), baseline.get('min_fps'))} |",
            f"| Avg GPU temp | {_fmt(baseline.get('avg_temp_c'), ' °C', 1)} | "
            f"{_fmt(tuned.get('avg_temp_c'), ' °C', 1)} | "
            f"{_delta(tuned.get('avg_temp_c'), baseline.get('avg_temp_c'), ' °C', invert=True)} |",
            f"| Max GPU temp | {_fmt(baseline.get('max_temp_c'), ' °C', 1)} | "
            f"{_fmt(tuned.get('max_temp_c'), ' °C', 1)} | "
            f"{_delta(tuned.get('max_temp_c'), baseline.get('max_temp_c'), ' °C', invert=True)} |",
            f"| Avg power | {_fmt(baseline.get('avg_power_w'), ' W', 1)} | "
            f"{_fmt(tuned.get('avg_power_w'), ' W', 1)} | "
            f"{_delta(tuned.get('avg_power_w'), baseline.get('avg_power_w'), ' W', invert=True)} |",
            "",
        ]

        base_fps, tuned_fps = baseline.get("avg_fps"), tuned.get("avg_fps")
        base_w, tuned_w = baseline.get("avg_power_w"), tuned.get("avg_power_w")
        if all(v for v in (base_fps, tuned_fps, base_w, tuned_w)):
            base_eff = base_fps / base_w
            tuned_eff = tuned_fps / tuned_w
            parts += [
                f"Efficiency: **{base_eff:.3f} → {tuned_eff:.3f} FPS/W** "
                f"({(tuned_eff - base_eff) / base_eff * 100:+.1f}%).",
                "",
            ]

        shots = [
            (label, row.get("screenshot_path"))
            for label, row in (("Stock", baseline), ("Tuned", tuned))
            if row.get("screenshot_path")
        ]
        if len(shots) == 2:
            parts += [
                "| " + " | ".join(label for label, _ in shots) + " |",
                "|" + "---|" * len(shots),
                "| " + " | ".join(f"![{label}]({path})" for label, path in shots) + " |",
                "",
            ]

    else:
        missing = "tuned" if baseline else "stock"
        parts += [
            f"_Only one side of the comparison has been recorded so far "
            f"(no completed **{missing}** run)._",
            "",
        ]

    parts += ["<details>", "<summary>All completed runs</summary>", ""]
    parts += [
        "| Run | Config | GPU range | Duration | Frames | Avg FPS | Max temp | Avg power |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in completed[-15:]:
        parts.append(
            f"| {row.get('iso_time', '—')} | {row.get('config_label', '—')} | "
            f"{_row_config(row)} | {_fmt(row.get('duration_s'), ' s')} | "
            f"{_fmt(row.get('score_frames'))} | {_fmt(row.get('avg_fps'))} | "
            f"{_fmt(row.get('max_temp_c'), ' °C', 1)} | "
            f"{_fmt(row.get('avg_power_w'), ' W', 1)} |"
        )
    parts += ["", "</details>", ""]

    if aborted:
        parts += [
            f"> **{len(aborted)} run(s) aborted on a thermal limit** and are "
            "excluded from the table above, since an aborted run produces no "
            "score. Most recent: "
            f"{aborted[-1].get('abort_reason', 'unknown reason')}",
            "",
        ]

    parts.append(
        "<sub>Generated by `benchmarks/scripts/generate_readme_benchmarks.py`. "
        "FurMark runs start from a cooled GPU so results are comparable.</sub>"
    )
    return "\n".join(parts)


def inject(readme: Path, section: str) -> tuple[bool, str]:
    """Replace the marked region of the README. Returns (changed, message)."""
    try:
        original = readme.read_text()
    except OSError as exc:
        return False, f"cannot read {readme}: {exc}"

    if START_MARKER not in original or END_MARKER not in original:
        return False, (
            f"{readme} is missing the {START_MARKER} / {END_MARKER} markers; "
            "add them where the benchmark table should go"
        )

    head, _, rest = original.partition(START_MARKER)
    _, _, tail = rest.partition(END_MARKER)
    updated = f"{head}{START_MARKER}\n\n{section}\n\n{END_MARKER}{tail}"

    if updated == original:
        return False, "README benchmark section already up to date"

    try:
        readme.write_text(updated)
    except OSError as exc:
        return False, f"cannot write {readme}: {exc}"
    return True, f"updated the benchmark section of {readme}"


def main() -> int:
    repo = Path(__file__).resolve().parent.parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=repo / "benchmarks/results.jsonl")
    parser.add_argument("--readme", type=Path, default=repo / "README.md")
    parser.add_argument(
        "--print", action="store_true", help="write the section to stdout instead"
    )
    args = parser.parse_args()

    rows = load_results(args.results)
    section = build_section(rows)

    if args.print:
        print(section)
        return 0

    changed, message = inject(args.readme, section)
    print(message)
    # "Already up to date" is success, not failure -- this runs in a loop.
    return 0 if changed or "up to date" in message else 1


if __name__ == "__main__":
    sys.exit(main())
