"""Tests for the guarded write path.

These are the highest-stakes tests in the project: they assert that no argument
combination reaches hardware with a value the envelope forbids, and that a
failure to snapshot blocks the write rather than proceeding blind.
"""

from __future__ import annotations

import pytest

from bc250_mcp import apply, governor, snapshots


@pytest.fixture
def stub_box(monkeypatch, tmp_path):
    """Neutralise every side effect, and record what would have been applied."""
    applied: list[tuple] = []
    prepared: list[str] = []

    state = governor.GovernorState(
        backend="oberon",
        service=governor.OBERON_UNIT,
        active=True,
        enabled=True,
        curve=[
            governor.OperatingPoint(1000, 875),
            governor.OperatingPoint(1600, 875),
        ],
        min_freq_mhz=1000,
        max_freq_mhz=1600,
    )
    monkeypatch.setattr(governor, "get_state", lambda *_a, **_k: state)
    monkeypatch.setattr(apply.governor, "get_state", lambda *_a, **_k: state)

    def fake_prepare(action, description, doc):
        prepared.append(action)
        return f"pre-{action}-test", []

    monkeypatch.setattr(apply, "_prepare", fake_prepare)
    monkeypatch.setattr(
        apply, "_set_range_oberon",
        lambda lo, hi, _s: (applied.append(("oberon", lo, hi)) or (True, "applied")),
    )
    monkeypatch.setattr(
        apply, "_set_range_cyan",
        lambda lo, hi: (applied.append(("cyan", lo, hi)) or (True, "applied")),
    )
    monkeypatch.setattr(apply, "dry_run_enabled", lambda: False)
    return {"applied": applied, "prepared": prepared, "state": state}


class TestEnvelopeGate:
    def test_value_beyond_hard_ceiling_is_refused_even_with_confirm(self, stub_box):
        """2400 MHz exceeds the ASIC's own 2230 MHz ceiling."""
        result = apply.set_gpu_range(1000, 2400, confirm=True)
        assert result["refused"]
        assert not result["applied"]
        assert stub_box["applied"] == []

    def test_nothing_is_snapshotted_for_a_refused_write(self, stub_box):
        """A refusal must not churn state: no snapshot, no pending marker."""
        apply.set_gpu_range(1000, 2400, confirm=True)
        assert stub_box["prepared"] == []

    def test_inverted_range_is_refused(self, stub_box):
        result = apply.set_gpu_range(1800, 1200)
        assert result["refused"]
        assert stub_box["applied"] == []

    def test_below_hard_floor_is_refused(self, stub_box):
        result = apply.set_gpu_range(100, 1600, confirm=True)
        assert result["refused"]
        assert stub_box["applied"] == []


class TestStepSizeLimit:
    def test_jump_larger_than_one_step_is_refused(self, stub_box):
        """Current max is 1600; 1900 is six 50 MHz steps away."""
        result = apply.set_gpu_range(1000, 1900)
        assert result["refused"]
        assert "one" in result["detail"] and "step" in result["detail"]
        assert stub_box["applied"] == []

    def test_single_step_is_allowed(self, stub_box):
        result = apply.set_gpu_range(1000, 1650)
        assert result["applied"]
        assert stub_box["applied"] == [("oberon", 1000, 1650)]

    def test_stepping_down_is_also_limited(self, stub_box):
        assert apply.set_gpu_range(1000, 1400)["refused"]
        assert apply.set_gpu_range(1000, 1550)["applied"]

    def test_step_limit_applies_before_confirm_can_override_it(self, stub_box):
        """confirm=True raises the envelope tier; it does not license a jump."""
        assert apply.set_gpu_range(1000, 1900, confirm=True)["refused"]
        assert stub_box["applied"] == []


class TestConfirmTier:
    def test_above_safe_bound_requires_confirmation(self, stub_box, monkeypatch):
        """Safe ceiling is 2000 MHz; 2050 needs confirm."""
        stub_box["state"].max_freq_mhz = 2020
        result = apply.set_gpu_range(1000, 2050)
        assert result["requires_confirmation"]
        assert not result["applied"]
        assert stub_box["applied"] == []

    def test_confirm_true_permits_the_confirm_tier(self, stub_box):
        stub_box["state"].max_freq_mhz = 2020
        result = apply.set_gpu_range(1000, 2050, confirm=True)
        assert result["applied"]

    def test_confirmation_prompt_tells_the_agent_what_to_do(self, stub_box):
        stub_box["state"].max_freq_mhz = 2020
        result = apply.set_gpu_range(1000, 2050)
        assert "confirm=True" in result["detail"]
        assert "user" in result["detail"].lower()

    def test_within_safe_tier_needs_no_confirmation(self, stub_box):
        assert apply.set_gpu_range(1000, 1650)["applied"]


class TestDryRun:
    def test_dry_run_does_not_touch_hardware(self, stub_box, monkeypatch):
        monkeypatch.setattr(apply, "dry_run_enabled", lambda: True)
        result = apply.set_gpu_range(1000, 1650)
        assert result["dry_run"]
        assert not result["applied"]
        assert stub_box["applied"] == []

    def test_dry_run_still_exercises_the_envelope(self, stub_box, monkeypatch):
        """A refusal must be a refusal in dry run too, or the rehearsal lies."""
        monkeypatch.setattr(apply, "dry_run_enabled", lambda: True)
        assert apply.set_gpu_range(1000, 2400, confirm=True)["refused"]

    def test_dry_run_still_snapshots(self, stub_box, monkeypatch):
        """The rehearsal should exercise the snapshot path, not skip it."""
        monkeypatch.setattr(apply, "dry_run_enabled", lambda: True)
        apply.set_gpu_range(1000, 1650)
        assert stub_box["prepared"] == ["set_gpu_range"]


class TestSnapshotIsMandatory:
    def test_write_is_refused_when_the_snapshot_fails(self, stub_box, monkeypatch):
        """No way back means no write. This is the whole safety argument."""
        def boom(*_a, **_k):
            raise snapshots.SnapshotError("disk full")

        monkeypatch.setattr(apply, "_prepare", boom)
        result = apply.set_gpu_range(1000, 1650)
        assert result["refused"]
        assert not result["applied"]
        assert stub_box["applied"] == []
        assert "snapshot" in result["detail"]


class TestBackendRouting:
    def test_oberon_uses_the_file_path(self, stub_box):
        apply.set_gpu_range(1000, 1650)
        assert stub_box["applied"][0][0] == "oberon"

    def test_cyan_uses_dbus_when_live_control_is_available(self, stub_box):
        stub_box["state"].backend = "cyan-skillfish"
        stub_box["state"].supports_live_control = True
        apply.set_gpu_range(1000, 1650)
        assert stub_box["applied"][0][0] == "cyan"

    def test_no_governor_reports_failure_rather_than_applying(self, stub_box):
        stub_box["state"].backend = "none"
        stub_box["state"].supports_live_control = False
        result = apply.set_gpu_range(1000, 1650)
        assert not result["applied"]
        assert stub_box["applied"] == []

    def test_failed_apply_warns_that_the_marker_is_still_set(self, stub_box, monkeypatch):
        monkeypatch.setattr(
            apply, "_set_range_oberon", lambda _lo, _hi, _s: (False, "restart failed")
        )
        result = apply.set_gpu_range(1000, 1650)
        assert not result["applied"]
        assert any("watchdog" in w for w in result["warnings"])


class TestMarkStable:
    def test_refuses_to_promote_a_throttling_config(self, monkeypatch):
        from bc250_mcp import telemetry

        reading = telemetry.Telemetry(timestamp=0, iso_time="")
        reading.thermal_throttle_flags = ["TEMP_EDGE"]
        reading.is_thermally_throttling = True
        monkeypatch.setattr(apply.telemetry, "collect", lambda: reading)

        result = apply.mark_stable()
        assert not result["promoted"]
        assert "throttling" in result["detail"]


class TestSetGpuConfig:
    """The operator-directed absolute config setter.

    Unlike set_gpu_range it is not step-limited -- applying a known config in
    one move is not what the step limit exists to prevent -- but every envelope
    bound still applies, and it sets voltage as well as frequency.
    """

    @pytest.fixture
    def stub(self, monkeypatch):
        written: list[tuple] = []
        state = governor.GovernorState(
            backend="oberon", service=governor.OBERON_UNIT, active=True, enabled=True,
            curve=[governor.OperatingPoint(1000, 875), governor.OperatingPoint(1600, 875)],
            min_freq_mhz=1000, max_freq_mhz=1600,
        )
        monkeypatch.setattr(apply.governor, "get_state", lambda *_a, **_k: state)
        monkeypatch.setattr(apply, "_prepare", lambda a, d, doc: (f"pre-{a}", []))
        monkeypatch.setattr(
            apply, "_write_oberon_config",
            lambda lo, hi, vlo, vhi: (written.append((lo, hi, vlo, vhi)) or (True, "ok")),
        )
        monkeypatch.setattr(apply, "dry_run_enabled", lambda: False)
        return {"written": written, "state": state}

    def test_applies_frequency_and_voltage_together(self, stub):
        result = apply.set_gpu_config(1000, 2000, 1000, 1000)
        assert result["applied"]
        assert stub["written"] == [(1000, 2000, 1000, 1000)]

    def test_is_not_step_limited(self, stub):
        """400 MHz in one move -- set_gpu_range would refuse this."""
        assert apply.set_gpu_config(1000, 2000, 1000, 1000)["applied"]
        assert apply.set_gpu_range(1000, 2000)["refused"]

    def test_voltage_beyond_hard_bound_is_refused(self, stub):
        result = apply.set_gpu_config(1000, 1600, 875, 1400, confirm=True)
        assert result["refused"]
        assert stub["written"] == []

    def test_voltage_above_safe_bound_needs_confirmation(self, stub):
        result = apply.set_gpu_config(1000, 1600, 875, 1100)
        assert result["requires_confirmation"]
        assert stub["written"] == []
        assert apply.set_gpu_config(1000, 1600, 875, 1100, confirm=True)["applied"]

    def test_frequency_beyond_the_asic_ceiling_is_refused(self, stub):
        assert apply.set_gpu_config(1000, 2400, 1000, 1000, confirm=True)["refused"]
        assert stub["written"] == []

    def test_inverted_voltage_is_refused(self, stub):
        result = apply.set_gpu_config(1000, 1600, 1000, 875)
        assert result["refused"]
        assert "voltage" in result["detail"]

    def test_snapshot_failure_blocks_the_write(self, stub, monkeypatch):
        def boom(*_a, **_k):
            raise snapshots.SnapshotError("disk full")

        monkeypatch.setattr(apply, "_prepare", boom)
        assert apply.set_gpu_config(1000, 2000, 1000, 1000)["refused"]
        assert stub["written"] == []

    def test_dry_run_touches_nothing(self, stub, monkeypatch):
        monkeypatch.setattr(apply, "dry_run_enabled", lambda: True)
        result = apply.set_gpu_config(1000, 2000, 1000, 1000)
        assert result["dry_run"] and not result["applied"]
        assert stub["written"] == []
