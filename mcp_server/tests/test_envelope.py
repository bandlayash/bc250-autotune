"""Tests for safety envelope enforcement.

These guard the property the whole project rests on: no code path applies a
value beyond a hard bound. Upstream bricked a BC-250 by letting Vid scale past
1.325 V, so the refusal tests below are not hypothetical.
"""

from __future__ import annotations

import pytest

from bc250_mcp import envelope
from bc250_mcp.envelope import Verdict


@pytest.fixture
def doc():
    return envelope.load()


class TestShippedEnvelope:
    def test_loads_and_declares_a_version(self, doc):
        assert doc["version"] == 1

    def test_cpu_hard_vid_stays_below_the_brick_threshold(self, doc):
        """Upstream: Vid must never exceed 1325 mV. Our ceiling must be lower.

        Strictly lower, not equal: the margin is what makes an off-by-one or a
        rounding error in our own code non-fatal.
        """
        assert doc["cpu"]["hard_max_vid_mv"] < 1325

    def test_safe_bounds_are_inside_hard_bounds(self, doc):
        assert doc["cpu"]["safe_max_vid_mv"] <= doc["cpu"]["hard_max_vid_mv"]
        assert doc["cpu"]["safe_max_frequency_mhz"] <= doc["cpu"]["hard_max_frequency_mhz"]
        assert doc["gpu"]["safe_max_frequency_mhz"] <= doc["gpu"]["hard_max_frequency_mhz"]
        assert doc["gpu"]["safe_max_voltage_mv"] <= doc["gpu"]["hard_max_voltage_mv"]

    def test_fan_floor_never_permits_a_stopped_fan(self, doc):
        """A BC-250 idles around 50 W; a curve reaching 0% must be impossible."""
        assert doc["fan"]["hard_min_pwm_percent"] > 0

    def test_optimizer_ceilings_sit_below_the_abort_thresholds(self, doc):
        """Back off before aborting, and abort before the hardware throttles."""
        assert doc["temperature"]["optimizer_ceiling_gpu_c"] < doc["benchmark"]["abort_gpu_temp_c"]
        assert doc["temperature"]["optimizer_ceiling_cpu_c"] < doc["benchmark"]["abort_cpu_temp_c"]


class TestUpperBoundChecks:
    def _vid(self, value, doc):
        return envelope.check_upper(
            "vid_mv", value,
            section="cpu", hard_key="hard_max_vid_mv", safe_key="safe_max_vid_mv",
            doc=doc,
        )

    def test_value_inside_safe_tier_is_allowed(self, doc):
        check = self._vid(1200, doc)
        assert check.verdict is Verdict.ALLOWED
        assert check.ok
        assert check.permitted(confirm=False)

    def test_value_between_safe_and_hard_needs_confirmation(self, doc):
        check = self._vid(1290, doc)
        assert check.verdict is Verdict.CONFIRM
        assert not check.permitted(confirm=False)
        assert check.permitted(confirm=True)

    def test_value_beyond_hard_bound_is_refused_even_with_confirm(self, doc):
        """The one guarantee that must never have an escape hatch."""
        check = self._vid(1325, doc)
        assert check.verdict is Verdict.REFUSED
        assert not check.permitted(confirm=False)
        assert not check.permitted(confirm=True)

    def test_absurd_value_is_refused(self, doc):
        assert self._vid(9999, doc).verdict is Verdict.REFUSED

    def test_boundary_values_are_inclusive(self, doc):
        """Exactly-at-the-bound is inside the bound, not over it."""
        assert self._vid(doc["cpu"]["safe_max_vid_mv"], doc).verdict is Verdict.ALLOWED
        assert self._vid(doc["cpu"]["hard_max_vid_mv"], doc).verdict is Verdict.CONFIRM


class TestLowerBoundChecks:
    def test_below_hard_floor_is_refused(self, doc):
        check = envelope.check_lower(
            "vid_mv", 500,
            section="cpu", hard_key="hard_min_vid_mv", doc=doc,
        )
        assert check.verdict is Verdict.REFUSED
        assert not check.permitted(confirm=True)

    def test_gpu_undervolt_below_safe_needs_confirmation(self, doc):
        check = envelope.check_lower(
            "voltage_mv", 720,
            section="gpu", hard_key="hard_min_voltage_mv", safe_key="safe_min_voltage_mv",
            doc=doc,
        )
        assert check.verdict is Verdict.CONFIRM


class TestWorst:
    def test_refusal_dominates_a_mixed_batch(self, doc):
        checks = [
            envelope.check_upper("a", 1200, section="cpu", hard_key="hard_max_vid_mv",
                                 safe_key="safe_max_vid_mv", doc=doc),
            envelope.check_upper("b", 1290, section="cpu", hard_key="hard_max_vid_mv",
                                 safe_key="safe_max_vid_mv", doc=doc),
            envelope.check_upper("c", 1400, section="cpu", hard_key="hard_max_vid_mv",
                                 safe_key="safe_max_vid_mv", doc=doc),
        ]
        assert envelope.worst(checks).verdict is Verdict.REFUSED

    def test_confirm_dominates_allowed(self, doc):
        checks = [
            envelope.check_upper("a", 1000, section="cpu", hard_key="hard_max_vid_mv",
                                 safe_key="safe_max_vid_mv", doc=doc),
            envelope.check_upper("b", 1290, section="cpu", hard_key="hard_max_vid_mv",
                                 safe_key="safe_max_vid_mv", doc=doc),
        ]
        assert envelope.worst(checks).verdict is Verdict.CONFIRM

    def test_empty_batch_is_an_error_not_a_pass(self):
        """An empty check list must never read as approval."""
        with pytest.raises(ValueError):
            envelope.worst([])


class TestLoadFailures:
    def test_missing_envelope_raises_rather_than_defaulting_to_no_limits(self, tmp_path):
        with pytest.raises(envelope.EnvelopeError):
            envelope.load(tmp_path / "absent.yaml")

    def test_malformed_envelope_raises(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("cpu: [this is not a mapping\n")
        with pytest.raises(envelope.EnvelopeError):
            envelope.load(path)

    def test_non_mapping_envelope_raises(self, tmp_path):
        path = tmp_path / "list.yaml"
        path.write_text("- just\n- a\n- list\n")
        with pytest.raises(envelope.EnvelopeError):
            envelope.load(path)

    def test_missing_key_raises_rather_than_being_treated_as_unbounded(self, tmp_path):
        path = tmp_path / "partial.yaml"
        path.write_text("version: 1\ncpu:\n  hard_max_vid_mv: 1300\n")
        doc = envelope.load(path)
        with pytest.raises(envelope.EnvelopeError):
            envelope.check_upper(
                "vid_mv", 1200, section="cpu", hard_key="nonexistent_key", doc=doc
            )
