"""Tests for robot_mic.py (push-to-talk on the Pi's own mic) and USB webcam auto-detection."""
import io
import wave

import numpy as np
import pytest

import robot_mic
import vision_posture_module as vision

DEVICES = [
    {"name": "vc4-hdmi-0: MAI PCM i2s-hifi-0 (hw:0,0)", "max_input_channels": 0},
    {"name": "sysdefault", "max_input_channels": 128},
    {"name": "USB 2.0 Camera: Audio (hw:2,0)", "max_input_channels": 1},
    {"name": "default", "max_input_channels": 128},
]


def test_picks_the_webcam_mic_over_virtual_devices():
    assert robot_mic.pick_input_device(DEVICES) == 2


def test_audio_input_device_overrides_by_name_or_index():
    assert robot_mic.pick_input_device(DEVICES, "default") == 3
    assert robot_mic.pick_input_device(DEVICES, "1") == 1
    assert robot_mic.pick_input_device(DEVICES, "0") is None  # an output-only device
    assert robot_mic.pick_input_device(DEVICES, "nope") is None


def test_no_input_devices():
    assert robot_mic.pick_input_device(DEVICES[:1]) is None


def test_to_wav_resamples_to_16k_mono():
    data = robot_mic.to_wav(np.zeros(48_000, dtype="float32"), 48_000)
    with wave.open(io.BytesIO(data)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16_000, 1, 2)
        assert w.getnframes() == 16_000


class FakeStream:
    def __init__(self, device, samplerate, channels, dtype, callback):
        if samplerate == 16_000:
            raise RuntimeError("Invalid sample rate")  # like many USB mics: native 48 kHz only
        self.callback, self.channels = callback, channels

    def start(self):
        self.callback(np.full((4800, self.channels), 0.5, dtype="float32"), 4800, None, None)

    def stop(self):
        pass

    def close(self):
        pass


class FakeSd:
    InputStream = FakeStream

    def query_devices(self, device=None):
        if device is None:
            return DEVICES
        return {**DEVICES[device], "default_samplerate": 48_000.0}


def test_robot_mic_falls_back_to_native_rate(monkeypatch):
    monkeypatch.setattr(robot_mic, "_sounddevice", FakeSd)
    monkeypatch.setattr(robot_mic, "refresh_devices", lambda: None)
    monkeypatch.setattr(robot_mic.config, "AUDIO_INPUT_DEVICE", None)
    mic = robot_mic.RobotMic()
    mic.start()
    assert mic.recording and mic.device_name.startswith("USB 2.0 Camera") and mic.level > 0
    data = mic.stop()
    assert not mic.recording and mic.stop() is None
    with wave.open(io.BytesIO(data)) as w:
        assert w.getframerate() == 16_000 and w.getnframes() == 1600  # 0.1 s at 48 kHz -> 16 kHz


def test_robot_mic_without_a_mic(monkeypatch):
    class NoMic(FakeSd):
        def query_devices(self, device=None):
            return DEVICES[:1]
    monkeypatch.setattr(robot_mic, "_sounddevice", NoMic)
    monkeypatch.setattr(robot_mic, "refresh_devices", lambda: None)
    monkeypatch.setattr(robot_mic.config, "AUDIO_INPUT_DEVICE", None)
    with pytest.raises(RuntimeError, match="webcam plugged in"):
        robot_mic.RobotMic().start()


def _video_node(root, name, driver, index=0):
    node = root / name
    (root / "drivers" / driver).mkdir(parents=True, exist_ok=True)
    (node / "device").mkdir(parents=True)
    (node / "device" / "driver").symlink_to(root / "drivers" / driver)
    (node / "index").write_text(f"{index}\n")


def test_find_usb_camera_skips_pi_isp_and_metadata_nodes(tmp_path):
    _video_node(tmp_path, "video19", "rp1-cfe")
    _video_node(tmp_path, "video20", "pispbe")
    _video_node(tmp_path, "video1", "uvcvideo", index=1)  # metadata node listed first on purpose
    _video_node(tmp_path, "video2", "uvcvideo", index=0)
    assert vision.find_usb_camera(str(tmp_path)) == "/dev/video2"


def test_find_usb_camera_none(tmp_path):
    _video_node(tmp_path, "video20", "pispbe")
    assert vision.find_usb_camera(str(tmp_path)) is None
    assert vision.find_usb_camera(str(tmp_path / "missing")) is None
