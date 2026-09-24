"""Sensor calibration store (calibration.py) and the status model that applies it (robot_status.py)."""
import json

import pytest

import calibration
import main
from behavior_tree_module import SharedState
from calibration import Calibration
from robot_status import RobotStatus


def test_missing_file_means_uncalibrated(tmp_path):
    cal = calibration.load(tmp_path / "nope.json")
    assert (cal.tof_scale, cal.tof_offset_mm, cal.tof_points) == (1.0, 0.0, [])
    assert calibration.corrected_mm(250, cal) == 250


def test_round_trip_and_unknown_keys_ignored(tmp_path):
    path = tmp_path / "calibration.json"
    cal = Calibration(tof_offset_mm=-12.0, tof_scale=1.02, tof_points=[[100, 110, 2.0]], imu_level_offset_deg=1.5)
    cal.stamp("tof")
    calibration.save(cal, path)
    raw = json.loads(path.read_text())
    raw["from_the_future"] = 1
    path.write_text(json.dumps(raw))
    loaded = calibration.load(path)
    assert loaded.tof_offset_mm == -12.0 and loaded.tof_scale == 1.02 and loaded.imu_level_offset_deg == 1.5
    assert "tof" in loaded.updated
    assert not list(tmp_path.glob(".calibration-*"))  # no temp files left behind


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", json.dumps({"tof_scale": 5, "tof_offset_mm": 0})])
def test_bad_files_fall_back_to_identity(tmp_path, content):
    path = tmp_path / "calibration.json"
    path.write_text(content)
    cal = calibration.load(path)
    assert (cal.tof_scale, cal.tof_offset_mm) == (1.0, 0.0)


def test_one_point_is_an_offset():
    assert calibration.fit_tof([[100, 118, 1.0]]) == (1.0, -18.0)


def test_two_points_fit_scale_and_offset():
    # sensor reads 5 % long plus 10 mm: raw = 1.05 * true + 10
    points = [[t, 1.05 * t + 10, 1.0] for t in (100, 300, 600)]
    scale, offset = calibration.fit_tof(points)
    assert scale == pytest.approx(1 / 1.05)
    assert offset == pytest.approx(-10 / 1.05)
    assert calibration.corrected_mm(round(1.05 * 450 + 10), Calibration(tof_scale=scale, tof_offset_mm=offset)) \
        == pytest.approx(450, abs=1)


def test_close_points_give_offset_only():
    scale, offset = calibration.fit_tof([[100, 110, 1.0], [120, 131, 1.0]])
    assert scale == 1.0 and offset == pytest.approx(-10.5)


def test_implausible_fit_is_refused():
    with pytest.raises(ValueError, match="implausible"):
        calibration.fit_tof([[100, 400, 1.0]])  # 300 mm off: the sensor saw something else


def test_nothing_in_range_passes_through_and_never_negative():
    cal = Calibration(tof_offset_mm=-30.0)
    assert calibration.corrected_mm(-1, cal) == -1
    assert calibration.corrected_mm(10, cal) == 0


def test_status_applies_calibration_and_tracks_tof():
    status = RobotStatus(calibration=Calibration(tof_offset_mm=-10.0))
    assert status.on_line("ACK,D,210", now=1.0) == 200
    assert status.distance_mm == 200 and not status.tof_missing
    status.on_line("NACK,D,NOTOF", now=2.0)
    assert status.tof_missing and status.distance_mm is None
    assert status.events[-1][1] == "NACK,D,NOTOF"
    status.on_line("NACK,D,NOTOF", now=3.0)
    assert [e[1] for e in status.events] == ["NACK,D,NOTOF"]  # repeats aren't events


def test_status_parses_telemetry_and_link_age():
    status = RobotStatus()
    status.on_line("T,-123,45,-6,B", now=10.0)
    assert (status.pitch_deg, status.rate_dps, status.correction_deg, status.mode) == (-12.3, 4.5, -0.6, "B")
    assert status.link_up(now=11.0) and not status.link_up(now=13.0)
    assert status.telemetry_fresh(now=10.5) and not status.telemetry_fresh(now=12.0)
    status.on_line("T,garbage", now=12.0)  # malformed: ignored, but still proof of life
    assert status.pitch_deg == -12.3 and status.link_up(now=13.0)


def test_line_handler_publishes_calibrated_distance_and_quiets_repeats():
    class Publisher:
        def __init__(self):
            self.sent = []

        def publish(self, topic, payload, **_):
            self.sent.append((topic, payload))

    publisher = Publisher()
    status = RobotStatus(calibration=Calibration(tof_offset_mm=5.0))
    handle = main._serial_line_handler(SharedState(), publisher, status)
    handle("ACK,D,95")
    handle("NACK,D,NOTOF")
    handle("NACK,D,NOTOF")  # the poller keeps asking: reported once
    handle("ACK,D,-1")
    topics = [p for t, p in publisher.sent]
    assert topics == ["100", "NACK,D,NOTOF", "-1"]


def test_level_calibration_through_the_robot_is_recorded(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "CALIBRATION_FILE", str(tmp_path / "calibration.json"))

    class Publisher:
        def publish(self, *a, **kw):
            pass

    status = RobotStatus()
    handle = main._serial_line_handler(SharedState(), Publisher(), status, save_calibration=True)
    handle("ACK,C,-153")
    assert calibration.load().imu_level_offset_deg == -1.53
