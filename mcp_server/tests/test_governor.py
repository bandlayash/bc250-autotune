"""Tests for governor config parsing. No hardware or running daemon required."""

from __future__ import annotations

from bc250_mcp import governor

# Verbatim from the test unit's /etc/oberon-config.yaml.
OBERON_LIVE = """\
opps:
  - frequency:
    - min: 1000
    - max: 1600
  - voltage:
    - min: 875
    - max: 875
"""


class TestOberonConfigParsing:
    def test_parses_the_two_operating_points(self):
        """oberon has exactly two points: (freq.min, volt.min) and (freq.max, volt.max).

        Pairing is positional in oberon's own C++: opps[0] carries frequency and
        opps[1] carries voltage, so the low point takes both minima.
        """
        curve, warnings = governor._parse_oberon_config(OBERON_LIVE)
        assert warnings == []
        assert len(curve) == 2
        assert (curve[0].frequency_mhz, curve[0].voltage_mv) == (1000, 875)
        assert (curve[1].frequency_mhz, curve[1].voltage_mv) == (1600, 875)

    def test_distinct_voltages_pair_with_their_own_frequency(self):
        curve, warnings = governor._parse_oberon_config(
            "opps:\n"
            "  - frequency:\n    - min: 1000\n    - max: 2000\n"
            "  - voltage:\n    - min: 800\n    - max: 1000\n"
        )
        assert warnings == []
        assert (curve[0].frequency_mhz, curve[0].voltage_mv) == (1000, 800)
        assert (curve[1].frequency_mhz, curve[1].voltage_mv) == (2000, 1000)

    def test_inverted_frequency_range_is_flagged(self):
        """oberon reads these positionally and would apply them backwards.

        It has no validation of its own, so a hand-edited file with min above
        max is silently honoured. We surface it rather than passing it on.
        """
        curve, warnings = governor._parse_oberon_config(
            "opps:\n"
            "  - frequency:\n    - min: 2000\n    - max: 1000\n"
            "  - voltage:\n    - min: 875\n    - max: 875\n"
        )
        assert curve
        assert any("exceeds max" in w for w in warnings)

    def test_inverted_voltage_range_is_flagged(self):
        _, warnings = governor._parse_oberon_config(
            "opps:\n"
            "  - frequency:\n    - min: 1000\n    - max: 1600\n"
            "  - voltage:\n    - min: 1000\n    - max: 800\n"
        )
        assert any("voltage min" in w for w in warnings)

    def test_missing_sections_are_reported_not_guessed(self):
        curve, warnings = governor._parse_oberon_config(
            "opps:\n  - frequency:\n    - min: 1000\n    - max: 1600\n"
        )
        assert curve == []
        assert any("voltage" in w for w in warnings)

    def test_missing_keys_are_reported(self):
        curve, warnings = governor._parse_oberon_config(
            "opps:\n"
            "  - frequency:\n    - min: 1000\n"
            "  - voltage:\n    - min: 875\n    - max: 875\n"
        )
        assert curve == []
        assert any("missing keys" in w for w in warnings)

    def test_malformed_yaml_is_reported_not_raised(self):
        curve, warnings = governor._parse_oberon_config("opps: [unclosed\n")
        assert curve == []
        assert any("not valid YAML" in w for w in warnings)

    def test_empty_and_unrelated_documents(self):
        for raw in ("", "something_else: 1\n"):
            curve, warnings = governor._parse_oberon_config(raw)
            assert curve == []
            assert warnings


class TestBackendDetection:
    def test_running_service_wins_over_installed_config(self, monkeypatch):
        """Both governors can be installed at once mid-migration.

        The test box is exactly this case: oberon running, cyan-skillfish
        layered but disabled. Whichever is *active* is the one in charge.
        """
        monkeypatch.setattr(
            governor, "_unit_is", lambda unit, _c: unit == governor.OBERON_UNIT
        )
        monkeypatch.setattr(governor.Path, "exists", lambda _self: True)
        assert governor.detect_backend() == "oberon"

    def test_cyan_active_is_detected(self, monkeypatch):
        monkeypatch.setattr(
            governor, "_unit_is", lambda unit, _c: unit == governor.CYAN_UNIT
        )
        monkeypatch.setattr(governor.Path, "exists", lambda _self: False)
        assert governor.detect_backend() == "cyan-skillfish"

    def test_no_governor_yields_none(self, monkeypatch):
        monkeypatch.setattr(governor, "_unit_is", lambda _u, _c: False)
        monkeypatch.setattr(governor.Path, "exists", lambda _self: False)
        assert governor.detect_backend() is None

    def test_state_with_no_governor_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(governor, "detect_backend", lambda: None)
        state = governor.get_state()
        assert state.backend == "none"
        assert not state.active
        assert state.warnings


class TestStateShape:
    def test_oberon_state_declares_no_live_control(self, monkeypatch):
        """The optimizer branches on this: no D-Bus means rewrite-and-restart."""
        monkeypatch.setattr(governor, "_unit_is", lambda _u, _c: True)
        monkeypatch.setattr(
            governor.Path, "read_text", lambda _self, **_kw: OBERON_LIVE
        )
        state = governor._read_oberon()
        assert state.backend == "oberon"
        assert state.supports_live_control is False
        assert "survives reboot" in state.persistence
        assert state.min_freq_mhz == 1000
        assert state.max_freq_mhz == 1600

    def test_unreadable_oberon_config_warns_rather_than_raising(self, monkeypatch):
        def boom(_self, **_kw):
            raise OSError("permission denied")

        monkeypatch.setattr(governor, "_unit_is", lambda _u, _c: True)
        monkeypatch.setattr(governor.Path, "read_text", boom)
        state = governor._read_oberon()
        assert state.curve == []
        assert any("cannot read" in w for w in state.warnings)

    def test_state_is_json_serialisable(self, monkeypatch):
        import json

        monkeypatch.setattr(governor, "_unit_is", lambda _u, _c: True)
        monkeypatch.setattr(
            governor.Path, "read_text", lambda _self, **_kw: OBERON_LIVE
        )
        json.dumps(governor._read_oberon().to_dict())
