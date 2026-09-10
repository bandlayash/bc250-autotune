"""Tests for telemetry assembly against a faked sysfs tree. No hardware needed."""

from __future__ import annotations

import pytest

from bc250_mcp import telemetry


@pytest.fixture
def fake_box(tmp_path, monkeypatch):
    """Build a sysfs-shaped tree matching the test unit, and point the code at it."""
    gpu = tmp_path / "hwmon1"
    gpu.mkdir()
    (gpu / "name").write_text("amdgpu")
    (gpu / "temp1_input").write_text("55000")
    (gpu / "power1_average").write_text("48249000")
    (gpu / "in0_input").write_text("868")   # GPU rail
    (gpu / "in1_input").write_text("1162")  # CPU Vid

    superio = tmp_path / "hwmon2"
    superio.mkdir()
    (superio / "name").write_text("nct6686")
    (superio / "temp1_input").write_text("57500")
    (superio / "fan1_input").write_text("0")     # unpopulated header
    (superio / "fan2_input").write_text("2285")
    (superio / "pwm2").write_text("189")

    device = tmp_path / "card1" / "device"
    device.mkdir(parents=True)
    (device / "uevent").write_text("DRIVER=amdgpu\nPCI_ID=1002:13FE\n")
    (device / "pp_dpm_sclk").write_text("0: 350Mhz \n1: 89Mhz *\n2: 2230Mhz ")
    (device / "power_dpm_force_performance_level").write_text("auto")

    monkeypatch.setattr(telemetry.sysfs, "find_amdgpu_hwmon", lambda: gpu)
    monkeypatch.setattr(telemetry.sysfs, "find_superio_hwmon", lambda: superio)
    monkeypatch.setattr(telemetry.sysfs, "find_gpu_device", lambda: device)
    monkeypatch.setattr(telemetry.sysfs, "find_hwmon", lambda *_n: None)  # no k10temp
    return {"gpu": gpu, "superio": superio, "device": device}


class TestVoltageRails:
    def test_rails_map_to_the_correct_names(self, fake_box):
        """in0 is the GPU rail; in1 is CPU Vid.

        Established by correlating both against the SMU on real hardware. If
        these are ever swapped, the most safety-critical value in the system
        would be reported under a harmless name.
        """
        reading = telemetry.collect()
        assert reading.gpu_voltage_mv == 868
        assert reading.cpu_vid_mv == 1162

    def test_normal_vid_raises_no_warning(self, fake_box):
        assert not any("DANGER" in w for w in telemetry.collect().warnings)

    def test_vid_above_the_brick_threshold_warns_loudly(self, fake_box):
        """The tripwire must fire on the value upstream says destroyed a board."""
        (fake_box["gpu"] / "in1_input").write_text("1400")
        warnings = telemetry.collect().warnings
        assert any("DANGER" in w and "1400" in w for w in warnings)

    def test_tripwire_is_exclusive_at_the_boundary(self, fake_box):
        """Exactly 1325 is the documented limit, not yet past it."""
        (fake_box["gpu"] / "in1_input").write_text(str(telemetry.VID_DANGER_MV))
        assert not any("DANGER" in w for w in telemetry.collect().warnings)

        (fake_box["gpu"] / "in1_input").write_text(str(telemetry.VID_DANGER_MV + 1))
        assert any("DANGER" in w for w in telemetry.collect().warnings)

    def test_tripwire_does_not_depend_on_the_envelope(self, monkeypatch, fake_box):
        """It must still fire if safety_envelope.yaml is unreadable.

        The envelope governs what we ask for; this observes what happened.
        """
        from bc250_mcp import envelope

        def boom(*_a, **_k):
            raise envelope.EnvelopeError("envelope gone")

        monkeypatch.setattr(envelope, "load", boom)
        (fake_box["gpu"] / "in1_input").write_text("1500")
        assert any("DANGER" in w for w in telemetry.collect().warnings)


class TestSensorAssembly:
    def test_units_are_converted(self, fake_box):
        reading = telemetry.collect()
        assert reading.gpu_temp_c == 55.0          # from millidegrees
        assert reading.gpu_power_w == 48.249       # from microwatts
        assert reading.cpu_temp_c == 57.5

    def test_clock_bounds_exclude_the_live_readout_row(self, fake_box):
        reading = telemetry.collect()
        assert reading.gpu_clock_mhz == 89
        assert (reading.gpu_clock_floor_mhz, reading.gpu_clock_ceiling_mhz) == (350, 2230)

    def test_unpopulated_fan_headers_are_omitted(self, fake_box):
        """The board has five headers and one fan; do not report four zeros."""
        fans = telemetry.collect().fans
        assert [f.channel for f in fans] == [2]
        assert fans[0].rpm == 2285

    def test_pwm_is_converted_from_0_255_to_percent(self, fake_box):
        assert telemetry.collect().fans[0].pwm_percent == 74.1

    def test_unsupported_counter_is_null_not_an_error(self, fake_box):
        """gfx1013 does not implement gpu_busy_percent."""
        reading = telemetry.collect()
        assert reading.gpu_busy_percent is None
        assert not any("busy" in w.lower() for w in reading.warnings)


class TestDegradedHardware:
    def test_missing_superio_warns_but_still_returns_gpu_sensors(
        self, fake_box, monkeypatch
    ):
        monkeypatch.setattr(telemetry.sysfs, "find_superio_hwmon", lambda: None)
        reading = telemetry.collect()
        assert reading.gpu_temp_c == 55.0
        assert reading.fans == []
        assert any("Super I/O" in w for w in reading.warnings)

    def test_missing_amdgpu_hwmon_does_not_raise(self, fake_box, monkeypatch):
        monkeypatch.setattr(telemetry.sysfs, "find_amdgpu_hwmon", lambda: None)
        reading = telemetry.collect()
        assert reading.gpu_temp_c is None
        assert any("amdgpu hwmon" in w for w in reading.warnings)

    def test_no_gpu_at_all_still_produces_a_reading(self, monkeypatch):
        for name in ("find_amdgpu_hwmon", "find_superio_hwmon", "find_gpu_device"):
            monkeypatch.setattr(telemetry.sysfs, name, lambda: None)
        monkeypatch.setattr(telemetry.sysfs, "find_hwmon", lambda *_n: None)
        reading = telemetry.collect()
        assert reading.timestamp > 0
        assert len(reading.warnings) >= 2

    def test_output_is_json_serialisable(self, fake_box):
        import json

        json.dumps(telemetry.collect_dict())


class TestGpuMetricsMerge:
    """gpu_metrics folding, including the clock field that cannot be trusted."""

    def _blob(self, *, avg_gfxclk=772, cur_gfxclk=5, gfx_temp=9000, throttle=0):
        import struct

        data = bytearray(256)
        struct.pack_into("<HBB", data, 0, 128, 2, 2)      # v2_2 header
        struct.pack_into("<H", data, 4, gfx_temp)         # gfx temp, centi-C
        struct.pack_into("<H", data, 6, 8125)             # soc temp
        struct.pack_into("<H", data, 28, 0xFFFF)          # gfx_activity absent
        struct.pack_into("<H", data, 40, 65430)           # socket power mW
        struct.pack_into("<H", data, 64, avg_gfxclk)
        struct.pack_into("<H", data, 76, cur_gfxclk)
        struct.pack_into("<I", data, 108, 0xFFFFFFFF)     # legacy: not reported
        struct.pack_into("<Q", data, 120, throttle)
        return bytes(data)

    def test_clock_comes_from_avg_not_cur(self, fake_box, monkeypatch):
        """Regression: cur_gfxclk read ~5 MHz while the GPU was saturated.

        It mirrors the pp_dpm_sclk live-readout row exactly, so using it made
        the reported clock actively wrong under load. avg_gfxclk is the field
        that tracks reality.
        """
        from bc250_mcp import gpu_metrics

        monkeypatch.setattr(
            gpu_metrics, "read", lambda _d=None: gpu_metrics.decode(self._blob())
        )
        reading = telemetry.collect()
        assert reading.gpu_clock_mhz == 772
        assert reading.cur_gfxclk_mhz == 5  # still reported, never authoritative

    def test_zero_avg_clock_does_not_overwrite_with_idle(self, fake_box, monkeypatch):
        """Both clock fields intermittently return 0 mid-run; ignore those."""
        from bc250_mcp import gpu_metrics

        monkeypatch.setattr(
            gpu_metrics,
            "read",
            lambda _d=None: gpu_metrics.decode(self._blob(avg_gfxclk=0)),
        )
        # Falls back to the sysfs-derived value rather than claiming 0 MHz.
        assert telemetry.collect().gpu_clock_mhz == 89

    def test_thermal_throttle_is_distinguished_from_power_throttle(self):
        from bc250_mcp import gpu_metrics

        thermal = gpu_metrics.decode(self._blob(throttle=1 << 35))  # TEMP_EDGE
        assert thermal.is_thermally_throttling
        assert not thermal.is_power_throttling
        assert thermal.thermal_throttle_flags == ["TEMP_EDGE"]

        power = gpu_metrics.decode(self._blob(throttle=1 << 0))     # PPT0
        assert power.is_power_throttling
        assert not power.is_thermally_throttling

    def test_all_ones_throttle_is_unknown_not_everything_engaged(self):
        """0xFFFF... means 'not reported', not 'all 50 limiters active'."""
        import struct

        from bc250_mcp import gpu_metrics

        data = bytearray(self._blob())
        struct.pack_into("<Q", data, 120, 0xFFFFFFFFFFFFFFFF)
        struct.pack_into("<I", data, 108, 0xFFFFFFFF)
        metrics = gpu_metrics.decode(bytes(data))
        assert metrics.throttle_flags == []
        assert not metrics.throttle_reported
        assert any("no throttle status" in w for w in metrics.warnings)

    def test_unsupported_struct_version_refuses_to_decode(self):
        """Offsets differ per revision; guessing would produce confident nonsense."""
        import struct

        from bc250_mcp import gpu_metrics

        data = bytearray(self._blob())
        struct.pack_into("<HBB", data, 0, 128, 2, 4)  # v2_4
        metrics = gpu_metrics.decode(bytes(data))
        assert not metrics.available
        assert metrics.version == "v2_4"
        assert any("not decoding" in w for w in metrics.warnings)

    def test_truncated_blob_is_handled(self):
        from bc250_mcp import gpu_metrics

        metrics = gpu_metrics.decode(b"\x00" * 16)
        assert not metrics.available
        assert metrics.warnings
