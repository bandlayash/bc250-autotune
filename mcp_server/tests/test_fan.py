"""Tests for fan control. No hardware required."""

from __future__ import annotations

import pytest

from bc250_mcp import fan


@pytest.fixture
def envelope_doc():
    from bc250_mcp import envelope

    return envelope.load()


class TestCurveClamping:
    """A too-slow fan curve is a safety problem to correct, not a request to
    reject: refusing would leave the previous curve in place, possibly worse."""

    def test_below_the_floor_is_raised_not_refused(self, envelope_doc):
        curve, notes = fan.validate_curve([(40, 0), (60, 10)], envelope_doc)
        floor = envelope_doc["fan"]["hard_min_pwm_percent"]
        assert all(pct >= floor for _, pct in curve)
        assert any("floor" in n for n in notes)

    def test_a_stopped_fan_is_impossible(self, envelope_doc):
        curve, _ = fan.validate_curve([(30, 0)], envelope_doc)
        assert all(pct > 0 for _, pct in curve)

    def test_above_the_ceiling_is_capped(self, envelope_doc):
        curve, notes = fan.validate_curve([(70, 150)], envelope_doc)
        assert all(pct <= 100 for _, pct in curve)
        assert any("capped" in n for n in notes)

    def test_full_speed_is_forced_above_the_threshold(self, envelope_doc):
        threshold = envelope_doc["fan"]["force_full_speed_above_c"]
        curve, notes = fan.validate_curve([(40, 30), (threshold + 5, 50)], envelope_doc)
        hot = [pct for temp, pct in curve if temp >= threshold]
        assert hot and all(pct == 100.0 for pct in hot)
        assert any("full-speed" in n for n in notes)

    def test_a_curve_ending_too_low_is_extended(self, envelope_doc):
        """A curve that never reaches the threshold leaves the top end
        unprotected, so a 100% point is appended."""
        curve, notes = fan.validate_curve([(40, 30), (60, 50)], envelope_doc)
        threshold = envelope_doc["fan"]["force_full_speed_above_c"]
        assert max(t for t, _ in curve) >= threshold
        assert curve[-1][1] == 100.0
        assert any("appended" in n for n in notes)

    def test_points_are_sorted_by_temperature(self, envelope_doc):
        curve, _ = fan.validate_curve([(80, 90), (40, 30), (60, 50)], envelope_doc)
        temps = [t for t, _ in curve]
        assert temps == sorted(temps)

    def test_a_valid_curve_is_left_alone(self, envelope_doc):
        threshold = envelope_doc["fan"]["force_full_speed_above_c"]
        curve, notes = fan.validate_curve([(40, 40), (70, 70), (threshold, 100)],
                                          envelope_doc)
        assert (40, 40) in curve and (70, 70) in curve
        assert notes == []


CURVE = [(40, 30.0), (60, 60.0), (80, 100.0)]


class TestInterpolation:

    def test_interpolates_between_points(self):
        assert fan.pwm_for_temperature(CURVE, 50) == pytest.approx(45.0)
        assert fan.pwm_for_temperature(CURVE, 70) == pytest.approx(80.0)

    def test_is_flat_outside_the_endpoints(self):
        assert fan.pwm_for_temperature(CURVE, 10) == 30.0
        assert fan.pwm_for_temperature(CURVE, 200) == 100.0

    def test_exact_points_return_exactly(self):
        for temp, pct in CURVE:
            assert fan.pwm_for_temperature(CURVE, temp) == pct

    def test_an_empty_curve_fails_safe_at_full_speed(self):
        """No curve must never mean no cooling."""
        assert fan.pwm_for_temperature([], 50) == 100.0

    def test_duplicate_temperatures_do_not_divide_by_zero(self):
        assert fan.pwm_for_temperature([(50, 40.0), (50, 80.0)], 50) in (40.0, 80.0)


class TestPercentConversion:
    def test_round_trips_within_quantisation(self):
        for pct in (25, 50, 75, 100):
            assert fan._raw_to_pct(fan._pct_to_raw(pct)) == pytest.approx(pct, abs=0.5)

    def test_bounds_map_to_the_sysfs_range(self):
        assert fan._pct_to_raw(0) == 0
        assert fan._pct_to_raw(100) == 255

    def test_out_of_range_input_is_clamped(self):
        assert fan._pct_to_raw(-50) == 0
        assert fan._pct_to_raw(500) == 255


class TestControllability:
    def test_readonly_pwm_reports_uncontrollable_with_the_reason(
        self, tmp_path, monkeypatch
    ):
        """The in-tree nct6683 case: pwm exists but has no store handler."""
        (tmp_path / "name").write_text("nct6686")
        pwm = tmp_path / "pwm1"
        pwm.write_text("189")
        pwm.chmod(0o444)
        monkeypatch.setattr(fan.sysfs, "find_superio_hwmon", lambda: tmp_path)

        state = fan.get_state()
        assert not state.controllable
        assert any("nct6687d" in w for w in state.warnings)

    def test_writes_are_refused_when_uncontrollable(self, tmp_path, monkeypatch):
        (tmp_path / "name").write_text("nct6686")
        pwm = tmp_path / "pwm1"
        pwm.write_text("189")
        pwm.chmod(0o444)
        monkeypatch.setattr(fan.sysfs, "find_superio_hwmon", lambda: tmp_path)

        assert not fan.set_manual(80)["applied"]
        assert not fan.set_automatic()["applied"]

    def test_no_superio_at_all_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(fan.sysfs, "find_superio_hwmon", lambda: None)
        state = fan.get_state()
        assert not state.controllable
        assert state.warnings
