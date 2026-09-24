"""tools/calibrate_sensors.py against a fake controller, plus the console webcam hand-off and the
exactly-once delivery of in-process topics (main.py)."""
import argparse
import importlib.util
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

import calibration
import config
import main
from behavior_tree_module import SUBSCRIBED_TOPICS
from frame_mailbox import FrameMailbox

spec = importlib.util.spec_from_file_location(
    "calibrate_sensors", Path(__file__).resolve().parents[1] / "tools" / "calibrate_sensors.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class FakeController:
    """Answers like the firmware; pitch follows ``self.pitch`` (a number or a function of time)."""

    def __init__(self, pitch=0.0, distances=None, imu=True):
        self.pitch = pitch
        self.distances = list(distances or [])
        self.imu = imu
        self.offset = 0.0
        self.telemetry = False
        self.out = []
        self.sent = []

    def _pitch(self):
        p = self.pitch(time.monotonic()) if callable(self.pitch) else self.pitch
        return p - self.offset

    def write(self, data):
        for line in data.decode().split("\n"):
            if not line:
                continue
            self.sent.append(line)
            if line == "P" or line == "O":
                self.out.append(f"ACK,{line}")
            elif line == "I":
                self.out.append("ACK,I" if self.imu else "NACK,I,NOIMU")
            elif line.startswith("T,"):
                self.telemetry = line == "T,1"
                self.out.append(f"ACK,{line}")
            elif line == "C":
                self.offset += self._pitch()
                self.out.append(f"ACK,C,{round(self.offset * 100)}")
            elif line == "D":
                self.out.append("NACK,D,NOTOF" if not self.distances else f"ACK,D,{self.distances.pop(0)}")

    def readline(self):
        if self.out:
            return (self.out.pop(0) + "\r\n").encode()
        if self.telemetry:
            time.sleep(0.002)
            return f"T,{round(self._pitch() * 10)},0,0,O\r\n".encode()
        return b""

    def reset_input_buffer(self):
        pass

    def close(self):
        pass


def link_for(fake):
    link = object.__new__(tool.Link)
    link.ser = fake
    return link


@pytest.fixture
def cal_file(tmp_path, monkeypatch):
    path = tmp_path / "calibration.json"
    monkeypatch.setattr(config, "CALIBRATION_FILE", str(path))
    return path


def args(**kw):
    defaults = {"seconds": 0.2, "force": False, "target_mm": None, "samples": 10, "save": False, "reset": False}
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def test_level_is_refused_when_the_robot_is_not_upright(cal_file):
    fake = FakeController(pitch=74.0)
    with pytest.raises(tool.CalibrationError, match="from level"):
        tool.cmd_imu_level(link_for(fake), args())
    assert "C" not in fake.sent and "O" in fake.sent  # never stored; servos were switched off first


def test_level_stores_the_offset(cal_file):
    fake = FakeController(pitch=3.2)
    assert tool.cmd_imu_level(link_for(fake), args()) == 0
    assert calibration.load().imu_level_offset_deg == pytest.approx(3.2)


def test_level_is_refused_while_moving(cal_file):
    fake = FakeController(pitch=lambda t: 5 * np.sin(t * 40))
    with pytest.raises(tool.CalibrationError, match="moved"):
        tool.cmd_imu_level(link_for(fake), args())


def test_sign_check_detects_both_directions(cal_file):
    for direction, expected in ((1, 0), (-1, 1)):
        start = time.monotonic()
        fake = FakeController(pitch=lambda t, d=direction, t0=start: 0.0 if t - t0 < 1.5 else d * 25.0)
        assert tool.cmd_imu_sign(link_for(fake), args(seconds=3)) == expected
        assert calibration.load().imu_pitch_sign_ok is (direction == 1)


def test_sign_check_waits_for_a_level_start(cal_file):
    fake = FakeController(pitch=34.0)  # already tipped: no trustworthy baseline
    with pytest.raises(tool.CalibrationError, match="never held level"):
        tool.cmd_imu_sign(link_for(fake), args(seconds=1.5))
    assert calibration.load().imu_pitch_sign_ok is None


def test_missing_imu_is_reported(cal_file):
    with pytest.raises(tool.CalibrationError, match="IMU not answering"):
        tool.cmd_imu(link_for(FakeController(imu=False)), args())


def test_tof_points_build_a_fit(cal_file):
    fake = FakeController(distances=[118] * 11)
    tool.cmd_tof(link_for(fake), args(target_mm=100, save=True))
    cal = calibration.load()
    assert cal.tof_offset_mm == pytest.approx(-18) and cal.tof_scale == 1.0
    fake = FakeController(distances=[438] * 11)  # 5 % long + 18: a second distance fits the scale too
    tool.cmd_tof(link_for(fake), args(target_mm=400, save=True))
    cal = calibration.load()
    assert len(cal.tof_points) == 2
    assert calibration.corrected_mm(118, cal) == 100 and calibration.corrected_mm(438, cal) == 400


def test_tof_missing_and_out_of_range(cal_file):
    with pytest.raises(tool.CalibrationError, match="NOTOF"):
        tool.cmd_tof(link_for(FakeController()), args(target_mm=100))
    with pytest.raises(tool.CalibrationError, match="too few"):
        tool.cmd_tof(link_for(FakeController(distances=[-1] * 11)), args(target_mm=100))


# ------------------------------------------------------------------ console webcam -> vision
def test_mailbox_keeps_only_the_latest_frame():
    box = FrameMailbox()
    assert not box.put(b"not a jpeg")
    assert box.put(b"\xff\xd8one") and box.put(b"\xff\xd8two")
    seq, jpeg = box.wait_newer(0, 0.1)
    assert jpeg == b"\xff\xd8two" and seq == 2
    assert box.wait_newer(seq, 0.05) == (seq, None)  # nothing newer: times out


def test_console_capture_decodes_frames():
    from vision_posture_module import _ConsoleCapture

    box = FrameMailbox()
    cap = _ConsoleCapture(box)
    ok, _ = cap.read()
    assert not ok  # no frame yet
    image = np.full((48, 64, 3), 200, np.uint8)
    threading.Timer(0.05, lambda: box.put(cv2.imencode(".jpg", image)[1].tobytes())).start()
    ok, frame = cap.read()
    assert ok and frame.shape == (48, 64, 3)


# ------------------------------------------------------------------ exactly-once delivery
def test_in_process_topics_are_not_also_taken_from_mqtt():
    delivered, published = [], []

    class Pub:
        def publish(self, topic, payload, *a, **kw):
            published.append((topic, payload))

    echo = main.LocalEchoPublisher(Pub(), lambda t, p: delivered.append((t, p)), (config.TOPIC_WAKE_FLAG,))
    echo.publish(config.TOPIC_WAKE_FLAG, "1", qos=0)
    echo.publish(config.TOPIC_AUDIO_STATE, "{}")
    assert delivered == [(config.TOPIC_WAKE_FLAG, "1")]
    assert len(published) == 2  # MQTT still sees everything
    assert config.TOPIC_WAKE_FLAG in SUBSCRIBED_TOPICS  # ...and main() drops it from the tree's subscriptions
