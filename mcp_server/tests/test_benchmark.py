"""Tests for the benchmark harness. No FurMark, no GPU, no display required."""

from __future__ import annotations

import json

import pytest

from bc250_mcp import benchmark
from bc250_mcp.benchmark import BenchmarkResult, Sample

# Verbatim header written by FurMark 2.10.2, plus a real row from the test unit.
SCORES_HEADER = (
    "date,demo,platform,vendor,renderer,api_version,width,height,fullscreen,"
    "antialiasing,max_time,frames,max_gpu_temp,avg_fps,min_fps,max_fps"
)
# Verbatim from the test unit -- note the unquoted commas inside the renderer.
REAL_ROW = (
    "2026.08.28@07:14:23,furmark-gl,Linux 6.17.7-ba29.fc43.bc250cu.x86_64 64-bit,"
    "AMD,AMD BC-250 (radeonsi, gfx1013, ACO, DRM 3.64, "
    "6.17.7-ba29.fc43.bc250cu.x86_64),OpenGL 4.6 (Core Profile) Mesa 26.0.4,"
    "1920,1080,NO,Off,600,73126,83,121,112,125"
)


@pytest.fixture
def bench_root(tmp_path, monkeypatch):
    monkeypatch.setenv(benchmark.BENCH_ROOT_ENV, str(tmp_path))
    return tmp_path


class TestFurmarkRowParsing:
    """FurMark writes this file UNQUOTED, with commas inside the renderer field.

    A real row therefore has 20 comma-separated fields against a 16-column
    header, and any header-position parse mis-maps every number. Parsing from
    the end is what makes it reliable.
    """

    def test_real_row_with_commas_in_the_renderer(self):
        """Verbatim from the test unit. Header-position parsing read max_time as
        1920 (the width) and frames as 1080 (the height)."""
        row = benchmark.parse_scores_row(REAL_ROW)
        result = BenchmarkResult(run_id="r", config_label="c")
        benchmark._apply_furmark_row(row, result)

        assert row["width"] == "1920"
        assert row["height"] == "1080"
        assert row["max_time"] == "600"
        assert result.score_frames == 73126
        assert result.avg_fps == 121
        assert result.min_fps == 112
        assert result.max_fps == 125
        assert result.furmark_max_gpu_temp_c == 83

    def test_extra_commas_do_not_shift_the_numeric_tail(self):
        """However many commas the driver adds, the last ten fields are fixed."""
        noisy = REAL_ROW.replace(
            "AMD BC-250 (radeonsi", "AMD BC-250 (radeonsi, extra, more, fields"
        )
        row = benchmark.parse_scores_row(noisy)
        assert row["max_time"] == "600"
        assert row["frames"] == "73126"
        assert row["max_fps"] == "125"

    def test_header_line_is_not_treated_as_data(self, tmp_path):
        path = tmp_path / "_scores_maxtime.csv"
        path.write_text(SCORES_HEADER + "\n" + REAL_ROW + "\n")
        rows = benchmark._read_scores_rows(path)
        assert len(rows) == 1
        assert benchmark.parse_scores_row(rows[0])["frames"] == "73126"

    def test_short_row_yields_nothing_rather_than_wrong_numbers(self):
        assert benchmark.parse_scores_row("a,b,c") == {}

    def test_blank_and_malformed_fields_become_none(self):
        result = BenchmarkResult(run_id="r", config_label="c")
        benchmark._apply_furmark_row(
            {"frames": "", "avg_fps": "n/a", "min_fps": None}, result
        )
        assert result.score_frames is None
        assert result.avg_fps is None
        assert result.min_fps is None


class TestSummarise:
    def _samples(self, count=10, **overrides):
        base = dict(
            gpu_temp_c=80.0, gpu_power_w=60.0, socket_power_w=130.0,
            gpu_clock_mhz=772, cpu_temp_c=70.0, cpu_vid_mv=1100,
            fan_rpm=2700, throttle_flags=[],
        )
        base.update(overrides)
        return [Sample(t=float(i), **base) for i in range(count)]

    def test_computes_averages_and_peaks(self):
        result = BenchmarkResult(run_id="r", config_label="c")
        benchmark._summarise(self._samples(), result)
        assert result.avg_temp_c == 80.0
        assert result.max_temp_c == 80.0
        assert result.avg_power_w == 60.0    # hwmon preferred over SMU socket
        assert result.avg_clock_mhz == 772.0
        assert result.sample_count == 10

    def test_warmup_samples_are_excluded(self):
        """FurMark spends its opening moments creating a window and compiling
        shaders; counting that idle period drags averages down by a variable
        amount depending on how fast the machine starts up."""
        samples = self._samples(count=10)
        samples[0].gpu_temp_c = 40.0   # idle
        samples[1].gpu_temp_c = 45.0   # idle
        result = BenchmarkResult(run_id="r", config_label="c")
        benchmark._summarise(samples, result)
        assert result.avg_temp_c == 80.0   # warmup dropped

    def test_short_runs_keep_every_sample(self):
        """With too few samples, dropping two would leave nothing to average."""
        result = BenchmarkResult(run_id="r", config_label="c")
        benchmark._summarise(self._samples(count=3), result)
        assert result.avg_temp_c == 80.0
        assert result.sample_count == 3

    def test_zero_power_readings_are_ignored(self):
        """A 0 W reading is a dropout, not a measurement; averaging it in would
        drag the result toward zero."""
        samples = self._samples(count=10)
        for i in (3, 5, 7):
            samples[i].gpu_power_w = 0.0
        result = BenchmarkResult(run_id="r", config_label="c")
        benchmark._summarise(samples, result)
        assert result.avg_power_w == 60.0

    def test_hwmon_power_is_preferred_over_the_smu_socket_field(self):
        """Regression: the SMU field reads 0.88-9.65 W under load while hwmon
        reads a believable 129-135 W. Preferring the SMU reported avg 8.71 W
        for a run that actually drew ~130 W."""
        samples = self._samples(count=10, gpu_power_w=130.0, socket_power_w=3.2)
        result = BenchmarkResult(run_id="r", config_label="c")
        benchmark._summarise(samples, result)
        assert result.avg_power_w == 130.0

    def test_smu_socket_power_is_used_when_hwmon_is_absent(self):
        samples = self._samples(count=10, gpu_power_w=None, socket_power_w=48.0)
        result = BenchmarkResult(run_id="r", config_label="c")
        benchmark._summarise(samples, result)
        assert result.avg_power_w == 48.0

    def test_thermal_throttling_is_distinguished_from_power_throttling(self):
        samples = self._samples(count=8)
        samples[4].throttle_flags = ["TEMP_EDGE"]
        result = BenchmarkResult(run_id="r", config_label="c")
        benchmark._summarise(samples, result)
        assert result.thermally_throttled
        assert result.throttle_flags_seen == ["TEMP_EDGE"]

        samples2 = self._samples(count=8)
        samples2[4].throttle_flags = ["PPT0"]
        result2 = BenchmarkResult(run_id="r", config_label="c")
        benchmark._summarise(samples2, result2)
        assert not result2.thermally_throttled
        assert result2.throttle_flags_seen == ["PPT0"]

    def test_all_none_sensors_do_not_raise(self):
        samples = [
            Sample(t=float(i), gpu_temp_c=None, gpu_power_w=None,
                   socket_power_w=None, gpu_clock_mhz=None, cpu_temp_c=None,
                   cpu_vid_mv=None, fan_rpm=None, throttle_flags=[])
            for i in range(6)
        ]
        result = BenchmarkResult(run_id="r", config_label="c")
        benchmark._summarise(samples, result)
        assert result.avg_temp_c is None
        assert result.sample_count == 6

    def test_empty_sample_list_is_safe(self):
        result = BenchmarkResult(run_id="r", config_label="c")
        benchmark._summarise([], result)
        assert result.sample_count == 0
        assert result.avg_temp_c is None


class TestAbortThresholds:
    def test_sampler_aborts_above_the_gpu_ceiling(self, monkeypatch):
        from bc250_mcp import telemetry as telemetry_module

        reading = telemetry_module.Telemetry(timestamp=0, iso_time="")
        reading.gpu_temp_c = 95.0
        monkeypatch.setattr(telemetry_module, "collect", lambda: reading)

        sampler = benchmark._Sampler(abort_gpu_c=92.0, abort_cpu_c=95.0)
        sampler.run()   # synchronous: returns as soon as it trips
        assert sampler.abort_reason is not None
        assert "95.0" in sampler.abort_reason

    def test_sampler_aborts_above_the_cpu_ceiling(self, monkeypatch):
        from bc250_mcp import telemetry as telemetry_module

        reading = telemetry_module.Telemetry(timestamp=0, iso_time="")
        reading.gpu_temp_c = 70.0
        reading.cpu_temp_c = 96.0
        monkeypatch.setattr(telemetry_module, "collect", lambda: reading)

        sampler = benchmark._Sampler(abort_gpu_c=92.0, abort_cpu_c=95.0)
        sampler.run()
        assert sampler.abort_reason is not None
        assert "CPU" in sampler.abort_reason

    def test_threshold_is_inclusive(self, monkeypatch):
        """At the threshold counts as reaching it; do not wait for one more degree."""
        from bc250_mcp import telemetry as telemetry_module

        reading = telemetry_module.Telemetry(timestamp=0, iso_time="")
        reading.gpu_temp_c = 92.0
        monkeypatch.setattr(telemetry_module, "collect", lambda: reading)

        sampler = benchmark._Sampler(abort_gpu_c=92.0, abort_cpu_c=95.0)
        sampler.run()
        assert sampler.abort_reason is not None


class TestDurationGuard:
    def test_duration_beyond_the_envelope_is_refused(self, bench_root, monkeypatch):
        """A malformed duration must not pin the GPU at full load indefinitely."""
        monkeypatch.setattr(benchmark, "find_furmark", lambda: bench_root)
        with pytest.raises(benchmark.BenchmarkError) as exc:
            benchmark.run("x", 999_999)
        assert "max_duration_s" in str(exc.value)

    def test_non_positive_duration_is_refused(self, bench_root):
        for bad in (0, -30):
            with pytest.raises(benchmark.BenchmarkError):
                benchmark.run("x", bad)

    def test_missing_furmark_is_a_clear_error(self, bench_root, monkeypatch):
        monkeypatch.setattr(benchmark, "find_furmark", lambda: None)
        with pytest.raises(benchmark.BenchmarkError) as exc:
            benchmark.run("x", 30)
        assert "BC250_FURMARK_DIR" in str(exc.value)

    def test_missing_display_is_a_clear_error(self, bench_root, monkeypatch):
        monkeypatch.setattr(benchmark, "find_furmark", lambda: bench_root)
        monkeypatch.setattr(benchmark, "detect_display", lambda: None)
        with pytest.raises(benchmark.BenchmarkError) as exc:
            benchmark.run("x", 30)
        assert "display" in str(exc.value)


class TestResultsStore:
    def test_append_and_read_back(self, bench_root):
        result = BenchmarkResult(run_id="r1", config_label="stock", score_frames=14602)
        benchmark._append_result(result)
        rows = benchmark.load_results()
        assert len(rows) == 1
        assert rows[0]["run_id"] == "r1"
        assert rows[0]["score_frames"] == 14602

    def test_get_result_by_id(self, bench_root):
        benchmark._append_result(BenchmarkResult(run_id="a", config_label="stock"))
        benchmark._append_result(BenchmarkResult(run_id="b", config_label="tuned"))
        assert benchmark.get_result("b")["config_label"] == "tuned"
        assert benchmark.get_result("missing") is None

    def test_corrupt_lines_are_skipped_not_fatal(self, bench_root):
        path = benchmark.results_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"run_id": "good", "config_label": "stock"})
            + "\n{ this is not json\n"
            + json.dumps({"run_id": "good2", "config_label": "tuned"})
            + "\n"
        )
        rows = benchmark.load_results()
        assert [r["run_id"] for r in rows] == ["good", "good2"]

    def test_missing_results_file_reads_as_empty(self, bench_root):
        assert benchmark.load_results() == []

    def test_aborted_runs_are_still_recorded(self, bench_root):
        """A run that cooked the box is data, not something to discard."""
        result = BenchmarkResult(
            run_id="hot", config_label="tuned", aborted=True,
            abort_reason="GPU reached 95 C",
        )
        benchmark._append_result(result)
        assert benchmark.load_results()[0]["aborted"] is True
