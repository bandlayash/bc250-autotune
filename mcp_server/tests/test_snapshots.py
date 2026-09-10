"""Tests for snapshot capture and restore. No hardware required."""

from __future__ import annotations

import json
import os

import pytest

from bc250_mcp import restore, snapshots
from bc250_mcp.snapshots import FanCapture, FileCapture, Snapshot


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    monkeypatch.setenv(snapshots.STATE_ROOT_ENV, str(tmp_path))
    return tmp_path


class TestFileCapture:
    def test_captures_content_verbatim(self, tmp_path):
        path = tmp_path / "oberon-config.yaml"
        # Comments and spacing must survive: restore writes bytes back, and a
        # reconstructed config would silently drop them.
        content = "# hand-tuned 2026-08-28\nopps:\n  - frequency:\n    - min: 1000\n"
        path.write_text(content)

        capture = FileCapture.capture(path)
        assert capture.existed
        assert capture.content == content
        assert capture.mode is not None

    @pytest.mark.skipif(
        not hasattr(os, "geteuid"), reason="POSIX file modes; the product targets Linux"
    )
    def test_captures_posix_mode(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text("x")
        path.chmod(0o644)
        assert FileCapture.capture(path).mode == 0o644

    def test_absent_file_is_recorded_as_absent(self, tmp_path):
        capture = FileCapture.capture(tmp_path / "nope.conf")
        assert not capture.existed
        assert capture.content is None


class TestSaveLoad:
    def test_round_trips(self, state_root):
        snap = Snapshot(label="t", version=snapshots.SNAPSHOT_VERSION)
        snap.files = [FileCapture(path="/etc/x", existed=True, content="a", mode=0o644)]
        snap.fans = [FanCapture(channel=2, pwm=189, enable=None, writable=False)]
        snapshots.save(snap)

        loaded = snapshots.load("t")
        assert loaded.label == "t"
        assert loaded.files[0].content == "a"
        assert loaded.fans[0].pwm == 189
        assert loaded.fans[0].writable is False

    def test_missing_snapshot_raises(self, state_root):
        with pytest.raises(snapshots.SnapshotError):
            snapshots.load("nonexistent")

    def test_corrupt_snapshot_raises_rather_than_restoring_garbage(self, state_root):
        directory = snapshots.snapshots_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "bad.json").write_text("{not json")
        with pytest.raises(snapshots.SnapshotError):
            snapshots.load("bad")

    def test_version_mismatch_is_refused(self, state_root):
        """A layout we may not understand must not be restored."""
        directory = snapshots.snapshots_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "old.json").write_text(json.dumps({"label": "old", "version": 1}))
        with pytest.raises(snapshots.SnapshotError) as exc:
            snapshots.load("old")
        assert "version" in str(exc.value)

    def test_save_leaves_no_temp_file_behind(self, state_root):
        snapshots.save(Snapshot(label="t", version=snapshots.SNAPSHOT_VERSION))
        assert list(snapshots.snapshots_dir().glob(".*tmp")) == []


class TestRestoreFiles:
    def _snap(self, path, content):
        snap = Snapshot(label="t", version=snapshots.SNAPSHOT_VERSION)
        snap.files = [
            FileCapture(path=str(path), existed=True, content=content, mode=0o644)
        ]
        return snap

    def test_unchanged_file_is_not_rewritten(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text("same")
        steps = restore._restore_files(self._snap(path, "same"), dry_run=False)
        assert steps[0].ok
        assert not steps[0].changed
        assert "already matches" in steps[0].detail

    def test_dry_run_reports_without_writing(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text("changed")
        steps = restore._restore_files(self._snap(path, "original"), dry_run=True)
        assert "WOULD RESTORE" in steps[0].detail
        assert path.read_text() == "changed"   # untouched
        assert not steps[0].changed

    def test_file_absent_at_capture_and_still_absent_is_a_noop(self, tmp_path):
        snap = Snapshot(label="t", version=snapshots.SNAPSHOT_VERSION)
        snap.files = [FileCapture(path=str(tmp_path / "gone.conf"), existed=False)]
        steps = restore._restore_files(snap, dry_run=False)
        assert steps[0].ok
        assert not steps[0].changed

    def test_file_created_since_capture_is_removed(self, tmp_path):
        """Leaving it would keep a config the snapshot says was not there."""
        path = tmp_path / "new.conf"
        path.write_text("created after the snapshot")
        snap = Snapshot(label="t", version=snapshots.SNAPSHOT_VERSION)
        snap.files = [FileCapture(path=str(path), existed=False)]
        steps = restore._restore_files(snap, dry_run=True)
        assert "WOULD REMOVE" in steps[0].detail


class TestRestoreFans:
    def _snap(self, writable):
        snap = Snapshot(label="t", version=snapshots.SNAPSHOT_VERSION)
        snap.fans = [FanCapture(channel=1, pwm=189, enable=None, writable=writable)]
        return snap

    def test_readonly_pwm_reports_nothing_to_restore(self, monkeypatch, tmp_path):
        """In-tree nct6683 exposes pwm read-only, so no rollback was ever needed."""
        monkeypatch.setattr(restore.sysfs, "find_superio_hwmon", lambda: tmp_path)
        steps = restore._restore_fans(self._snap(writable=False), dry_run=False)
        assert steps[0].ok
        assert not steps[0].changed
        assert "read-only" in steps[0].detail

    def test_dry_run_compares_before_claiming_a_write(self, monkeypatch, tmp_path):
        """A preview that overstates what it will do erodes its own value."""
        (tmp_path / "pwm1").write_text("189")
        monkeypatch.setattr(restore.sysfs, "find_superio_hwmon", lambda: tmp_path)
        steps = restore._restore_fans(self._snap(writable=True), dry_run=True)
        assert "already matches" in steps[0].detail

    def test_dry_run_reports_a_genuine_difference(self, monkeypatch, tmp_path):
        (tmp_path / "pwm1").write_text("120")
        monkeypatch.setattr(restore.sysfs, "find_superio_hwmon", lambda: tmp_path)
        steps = restore._restore_fans(self._snap(writable=True), dry_run=True)
        assert "WOULD WRITE" in steps[0].detail


class TestRestoreUnits:
    def _snap(self, active):
        snap = Snapshot(label="t", version=snapshots.SNAPSHOT_VERSION)
        snap.units = {"x.service": {"active": active, "enabled": "enabled"}}
        return snap

    def test_active_unit_is_not_restarted_when_config_is_unchanged(self, monkeypatch):
        """Needless restarts drop the governor for its RestartSec window."""
        monkeypatch.setattr(restore, "_run", lambda _c: (0, "active"))
        steps = restore._restore_units(self._snap("active"), False, files_changed=False)
        assert steps[0].ok
        assert "no restart needed" in steps[0].detail

    def test_active_unit_is_restarted_when_config_changed(self, monkeypatch):
        monkeypatch.setattr(restore, "_run", lambda _c: (0, "active"))
        steps = restore._restore_units(self._snap("active"), True, files_changed=True)
        assert "WOULD restart" in steps[0].detail

    def test_inactive_unit_is_left_stopped(self, monkeypatch):
        """Starting a deliberately-stopped governor would be harmful, not a restore."""
        monkeypatch.setattr(restore, "_run", lambda _c: (0, "inactive"))
        steps = restore._restore_units(self._snap("inactive"), False, files_changed=True)
        assert steps[0].ok
        assert "inactive, as captured" in steps[0].detail


class TestBootPathAvoidsDeadlock:
    """Regression tests for a deadlock observed on real hardware.

    The watchdog unit is ordered ``Before=oberon-governor.service``. Calling
    ``systemctl restart oberon-governor`` from inside it blocked forever -- the
    restart waited on the watchdog, the watchdog waited on the restart. The unit
    hung in ``activating`` and the governor never started at all.
    """

    @pytest.fixture
    def snap_on_disk(self, state_root, tmp_path):
        config = tmp_path / "oberon-config.yaml"
        config.write_text("restored contents\n")
        snap = Snapshot(label=snapshots.LAST_GOOD_LABEL, version=snapshots.SNAPSHOT_VERSION)
        snap.files = [
            FileCapture(path=str(config), existed=True, content="restored contents\n",
                        mode=0o644)
        ]
        snap.units = {"oberon-governor.service": {"active": "active", "enabled": "enabled"}}
        snap.governor_backend = "cyan-skillfish"
        snap.governor_live_range_mhz = {"min": 1000, "max": 1600}
        snapshots.save(snap)
        return snap

    def test_boot_restore_issues_no_systemctl_calls(self, snap_on_disk, monkeypatch):
        calls: list[list[str]] = []
        monkeypatch.setattr(restore, "_run", lambda cmd: (calls.append(cmd), (0, ""))[1])
        monkeypatch.setattr(restore.sysfs, "find_superio_hwmon", lambda: None)

        report = restore.restore(snapshots.LAST_GOOD_LABEL, restart_units=False)

        assert report.ok
        assert not any("systemctl" in part for cmd in calls for part in cmd)

    def test_boot_restore_skips_dbus_since_the_daemon_is_not_up(
        self, snap_on_disk, monkeypatch
    ):
        calls: list[list[str]] = []
        monkeypatch.setattr(restore, "_run", lambda cmd: (calls.append(cmd), (0, ""))[1])
        monkeypatch.setattr(restore.sysfs, "find_superio_hwmon", lambda: None)

        restore.restore(snapshots.LAST_GOOD_LABEL, restart_units=False)
        assert not any("busctl" in part for cmd in calls for part in cmd)

    def test_boot_restore_still_restores_the_config_file(self, snap_on_disk, monkeypatch):
        """Skipping restarts must not skip the actual restore."""
        target = snap_on_disk.files[0].path
        Path = type(snapshots.snapshots_dir())
        Path(target).write_text("a bad config from the failed session\n")

        monkeypatch.setattr(restore, "_run", lambda _cmd: (0, ""))
        monkeypatch.setattr(restore.sysfs, "find_superio_hwmon", lambda: None)
        monkeypatch.setattr(
            restore, "_write_privileged",
            lambda path, content, mode: (
                Path(path).write_text(content), (True, f"restored {path}")
            )[1],
        )

        report = restore.restore(snapshots.LAST_GOOD_LABEL, restart_units=False)
        assert report.ok
        assert Path(target).read_text() == "restored contents\n"

    def test_runtime_restore_does_restart_units(self, snap_on_disk, monkeypatch):
        """The deadlock fix must not disable restarts for normal rollbacks."""
        calls: list[list[str]] = []

        def fake_run(cmd):
            calls.append(cmd)
            return (0, "inactive" if "is-active" in cmd else "")

        monkeypatch.setattr(restore, "_run", fake_run)
        monkeypatch.setattr(restore.sysfs, "find_superio_hwmon", lambda: None)
        monkeypatch.setattr(
            restore, "_write_privileged", lambda *_a: (True, "restored")
        )

        restore.restore(snapshots.LAST_GOOD_LABEL, restart_units=True)
        assert any("systemctl" in part for cmd in calls for part in cmd)


class TestReportAggregation:
    def test_one_failed_step_does_not_mask_the_others(self):
        report = restore.RestoreReport(label="t", dry_run=False)
        report.steps = [
            restore.StepResult("a", True, "ok", changed=True),
            restore.StepResult("b", False, "failed"),
            restore.StepResult("c", True, "ok"),
        ]
        assert not report.ok
        assert report.changed
        assert report.to_dict()["failed_steps"] == ["b"]
