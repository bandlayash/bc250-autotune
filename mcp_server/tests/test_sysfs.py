"""Tests for sysfs parsing. No hardware required."""

from __future__ import annotations

from bc250_mcp import sysfs


class TestParseDpmClock:
    # Captured verbatim from the test unit. The middle row is a live readout,
    # not a selectable state, and its value moves between reads.
    BC250_TABLE = "0: 350Mhz \n1: 89Mhz *\n2: 2230Mhz "

    def test_current_is_the_marked_row(self):
        assert sysfs.parse_dpm_clock(self.BC250_TABLE).current_mhz == 89

    def test_live_readout_is_excluded_from_bounds(self):
        """The 89 MHz row must not be mistaken for the hardware floor.

        This is the bug that motivated DpmTable: an optimizer taking min() of
        the raw table would use ~89 MHz (or 12, or 9 -- it varies between
        reads) as the floor instead of the real 350 MHz.
        """
        table = sysfs.parse_dpm_clock(self.BC250_TABLE)
        assert table.min_mhz == 350
        assert table.max_mhz == 2230

    def test_states_stay_truthful(self):
        """The raw table is reported as-is; only the bounds apply exclusion."""
        assert sysfs.parse_dpm_clock(self.BC250_TABLE).states_mhz == [89, 350, 2230]

    def test_bounds_are_stable_as_the_live_row_moves(self):
        """Same table, different instantaneous clock -> identical bounds."""
        bounds = {
            sysfs.parse_dpm_clock(f"0: 350Mhz \n1: {live}Mhz *\n2: 2230Mhz ")[3:]
            for live in (89, 12, 9, 1850)
        }
        assert bounds == {(350, 2230)}

    def test_marked_top_state_is_not_dropped(self):
        """Regression: an ASIC sitting at its top state keeps that ceiling.

        Excluding the marked row unconditionally reported a 1000 MHz ceiling
        here -- a worse error than the one the exclusion exists to prevent.
        """
        table = sysfs.parse_dpm_clock("0: 500Mhz \n1: 1000Mhz \n2: 2000Mhz *")
        assert table.current_mhz == 2000
        assert table.max_mhz == 2000
        assert table.min_mhz == 500

    def test_marked_state_matching_another_row_is_kept(self):
        """A marked value that also appears unmarked is a genuine state."""
        table = sysfs.parse_dpm_clock("0: 500Mhz \n1: 500Mhz *\n2: 2000Mhz ")
        assert table.min_mhz == 500
        assert table.max_mhz == 2000

    def test_single_state_table_keeps_its_only_row(self):
        table = sysfs.parse_dpm_clock("0: 1200Mhz *")
        assert table.current_mhz == 1200
        assert table.states_mhz == [1200]
        assert table.min_mhz == 1200
        assert table.max_mhz == 1200

    def test_empty_and_malformed_input(self):
        for raw in (None, "", "   ", "garbage without a colon"):
            table = sysfs.parse_dpm_clock(raw)
            assert table.current_mhz is None
            assert table.states_mhz == []
            assert table.min_mhz is None
            assert table.max_mhz is None

    def test_entries_preserve_the_raw_table(self):
        entries = sysfs.parse_dpm_clock(self.BC250_TABLE).entries
        assert entries == [(0, 350, False), (1, 89, True), (2, 2230, False)]


class TestReadHelpers:
    def test_missing_file_is_none_not_an_exception(self, tmp_path):
        assert sysfs.read_text(tmp_path / "nope") is None
        assert sysfs.read_int(tmp_path / "nope") is None

    def test_non_integer_content_is_none(self, tmp_path):
        path = tmp_path / "value"
        path.write_text("not-a-number")
        assert sysfs.read_int(path) is None
        assert sysfs.read_text(path) == "not-a-number"

    def test_directory_read_does_not_raise(self, tmp_path):
        """Reading a directory raises IsADirectoryError on Linux; swallow it."""
        assert sysfs.read_text(tmp_path) is None

    def test_unit_conversion(self, tmp_path):
        (tmp_path / "temp1_input").write_text("54000")
        (tmp_path / "power1_average").write_text("50159000")
        assert sysfs.hwmon_millicelsius(tmp_path, "temp1_input") == 54.0
        assert sysfs.hwmon_microwatts(tmp_path, "power1_average") == 50.159

    def test_conversion_of_absent_hwmon_is_none(self):
        assert sysfs.hwmon_millicelsius(None, "temp1_input") is None
        assert sysfs.hwmon_microwatts(None, "power1_average") is None


class TestHwmonDiscovery:
    def test_superio_names_cover_both_driver_spellings(self):
        """The in-tree driver binds as nct6686; the DKMS one as nct6687."""
        assert "nct6686" in sysfs.SUPERIO_NAMES
        assert "nct6687" in sysfs.SUPERIO_NAMES

    def test_find_hwmon_returns_none_when_nothing_matches(self, monkeypatch):
        monkeypatch.setattr(sysfs.glob, "glob", lambda _pattern: [])
        assert sysfs.find_hwmon("nct6686") is None
        assert sysfs.find_amdgpu_hwmon() is None

    def test_find_hwmon_matches_by_name_not_index(self, tmp_path, monkeypatch):
        """hwmon indices are probe-order and unstable; matching must use `name`."""
        for index, name in ((0, "nvme"), (1, "amdgpu"), (2, "nct6686")):
            node = tmp_path / f"hwmon{index}"
            node.mkdir()
            (node / "name").write_text(name)

        monkeypatch.setattr(
            sysfs.glob,
            "glob",
            lambda _pattern: sorted(str(p) for p in tmp_path.glob("hwmon*")),
        )
        assert sysfs.find_hwmon("amdgpu").name == "hwmon1"
        assert sysfs.find_superio_hwmon().name == "hwmon2"
