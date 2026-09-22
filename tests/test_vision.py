"""Tests for vision_posture_module.py: face selection, posture rules, baseline and alert timing."""
from types import SimpleNamespace

import pytest

import vision_posture_module as vp


def det(xmin, ymin, w, h, score):
    box = SimpleNamespace(xmin=xmin, ymin=ymin, width=w, height=h)
    return SimpleNamespace(location_data=SimpleNamespace(relative_bounding_box=box), score=[score])


def test_primary_face_prefers_large_confident_face():
    far = det(0.1, 0.1, 0.05, 0.05, 0.99)
    near = det(0.5, 0.4, 0.3, 0.3, 0.80)
    assert vp.select_primary_face([far, near], 640, 480) == (416, 264)
    assert vp.select_primary_face(None, 640, 480) is None


def test_clipped_face_keeps_true_centre():
    # half off the left edge: true centre x = 0.05 -> 32 px
    assert vp.select_primary_face([det(-0.1, 0.3, 0.3, 0.3, 0.9)], 640, 480)[0] == 32


def person(half_width, head_height, cx=0.5, top=0.2, lean_x=0.0, tilt=0.0, vis=0.99):
    """Shoulders 2*half_width apart; nose head_height*shoulder_width above the shoulder line."""
    lm = [SimpleNamespace(x=0.0, y=0.0, visibility=0.0) for _ in range(33)]
    width = 2 * half_width
    shoulder_y = top + head_height * width
    lm[vp.NOSE] = SimpleNamespace(x=cx + lean_x * width, y=top, visibility=vis)
    lm[vp.LEFT_SHOULDER] = SimpleNamespace(x=cx + half_width, y=shoulder_y - tilt * width / 2, visibility=vis)
    lm[vp.RIGHT_SHOULDER] = SimpleNamespace(x=cx - half_width, y=shoulder_y + tilt * width / 2, visibility=vis)
    return SimpleNamespace(landmark=lm)


@pytest.mark.parametrize("half_width", [0.25, 0.12, 0.06, 0.03])
def test_posture_judgement_is_distance_independent(half_width):
    assert vp.is_poor_posture(person(half_width, head_height=0.5)) is False
    assert vp.is_poor_posture(person(half_width, head_height=0.2)) is True


def test_posture_rules():
    assert vp.is_poor_posture(person(0.2, 0.5, lean_x=0.5)) is True  # leaning sideways
    assert vp.is_poor_posture(person(0.2, 0.5, tilt=0.2)) is True  # uneven shoulders
    assert vp.is_poor_posture(person(0.2, 0.5, vis=0.1)) is None  # not visible
    assert vp.is_poor_posture(None) is None


def test_baseline_catches_personal_slump():
    upright = person(0.2, head_height=0.6)
    slumped = person(0.2, head_height=0.42)  # above the absolute floor, but 30% below this user's normal
    assert vp.is_poor_posture(slumped) is False
    assert vp.is_poor_posture(slumped, baseline_head_height=0.6) is True
    assert vp.is_poor_posture(upright, baseline_head_height=0.6) is False


def test_baseline_percentile():
    base = vp.HeadHeightBaseline(window=100, min_samples=10, sample_period_s=1.0)
    assert base.value is None
    for i in range(200):
        base.add(0.4 + (i % 10) * 0.02, now=float(i))
    assert base.value == pytest.approx(0.56)


def test_posture_monitor_hysteresis():
    mon = vp.PostureMonitor(confirm_s=3, clear_s=1, remind_s=60, absent_reset_s=10)
    events = {t: mon.update(True, t) for t in [0, 1, 2, 2.9]}
    assert set(events.values()) == {None}
    assert mon.update(True, 3.0) == "POSTURE_POOR"
    assert mon.update(True, 30) is None
    assert mon.update(True, 63) == "POSTURE_POOR"  # reminder
    assert mon.update(False, 64) is None
    assert mon.update(False, 65) == "POSTURE_OK"


def test_brief_slouch_does_not_alert():
    mon = vp.PostureMonitor(confirm_s=3, clear_s=1)
    for t in range(10):
        assert mon.update(t % 2 == 0, float(t)) is None


def test_user_leaving_resets_alert():
    mon = vp.PostureMonitor(confirm_s=0, clear_s=1, absent_reset_s=10)
    assert mon.update(True, 0) == "POSTURE_POOR"
    assert mon.update(None, 1) is None
    assert mon.update(None, 11) == "POSTURE_OK"


def test_returning_user_needs_full_confirm_time():
    """Issue #8: after leaving mid-slouch, one poor frame on return used to trigger an alert."""
    mon = vp.PostureMonitor(confirm_s=3, clear_s=1, absent_reset_s=10)
    assert mon.update(True, 0.0) is None  # slouch starts
    assert mon.update(None, 1.0) is None  # user leaves before an alert
    assert mon.update(None, 30.0) is None
    assert mon.update(True, 60.0) is None  # back, one poor frame: not yet
    assert mon.update(True, 62.9) is None
    assert mon.update(True, 63.0) == "POSTURE_POOR"


def test_brief_dropout_does_not_restart_the_timer():
    mon = vp.PostureMonitor(confirm_s=3, clear_s=1)
    mon.update(True, 0.0)
    mon.update(None, 1.0)  # a single missed frame (< grace period)
    mon.update(True, 1.2)
    assert mon.update(True, 3.0) == "POSTURE_POOR"


class FlakyCapture:
    """Delivers ``frames`` good frames, then fails forever."""

    def __init__(self, frames):
        self.frames = frames
        self.released = False

    def read(self):
        if self.frames > 0:
            self.frames -= 1
            return True, "frame"
        return False, None

    def release(self):
        self.released = True


def test_camera_reopens_after_failures(monkeypatch):
    """Issue #9: a camera that stopped delivering frames was retried silently forever."""
    monkeypatch.setattr(vp.time, "sleep", lambda s: None)
    opened, states = [], []

    def opener(_source):
        cap = FlakyCapture(frames=2)
        opened.append(cap)
        return cap

    cam = vp.ResilientCamera(on_state=states.append, opener=opener)
    assert cam.read() == "frame"
    assert states == ["UP"]
    frames = [cam.read() for _ in range(vp.CAMERA_FAIL_LIMIT + 1)]
    assert frames[0] == "frame" and all(f is None for f in frames[1:])
    assert states == ["UP", "DOWN"]
    assert opened[0].released

    cam.next_open = 0  # skip the backoff wait
    assert cam.read() == "frame"  # reopened
    assert len(opened) == 2 and states == ["UP", "DOWN", "UP"]


def test_camera_missing_at_start_keeps_retrying(monkeypatch):
    monkeypatch.setattr(vp.time, "sleep", lambda s: None)
    states, attempts = [], []

    def opener(_source):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("no camera")
        return FlakyCapture(frames=5)

    cam = vp.ResilientCamera(on_state=states.append, opener=opener)
    for _ in range(3):
        cam.next_open = 0
        frame = cam.read()
    assert frame == "frame"
    assert states == ["DOWN", "UP"]
    assert cam.backoff == 1.0  # reset after success


def test_face_detection_is_optional(monkeypatch):
    """Issue #11: face detection costs Pi CPU and nothing uses it, so it's off by default."""
    import sys

    created = []

    class Detector:
        def __init__(self, **kw):
            created.append(type(self).__name__)

        def close(self):
            pass

    class FaceDetection(Detector):
        pass

    class Pose(Detector):
        pass

    fake = SimpleNamespace(__version__="0.10.18", solutions=SimpleNamespace(
        face_detection=SimpleNamespace(FaceDetection=FaceDetection), pose=SimpleNamespace(Pose=Pose)))
    monkeypatch.setitem(sys.modules, "mediapipe", fake)
    vp.VisionPipeline(face_detection=False).close()
    assert created == ["Pose"]
    vp.VisionPipeline(face_detection=True).close()
    assert created == ["Pose", "FaceDetection", "Pose"]


def test_mediapipe_version_message(monkeypatch):
    import sys

    fake = SimpleNamespace(__version__="9.9.9")
    monkeypatch.setitem(sys.modules, "mediapipe", fake)
    with pytest.raises(RuntimeError, match="0.10.18"):
        vp.VisionPipeline()


# ---------------------------------------------------------------- round 2 (#24, #26)

class CountingCapture:
    def __init__(self):
        self.reads = self.grabs = 0

    def read(self):
        self.reads += 1
        return True, "frame"

    def grab(self):
        self.grabs += 1
        return True

    def release(self):
        pass


def test_grab_skips_decoding(monkeypatch):
    monkeypatch.setattr(vp.time, "sleep", lambda s: None)
    cap = CountingCapture()
    cam = vp.ResilientCamera(opener=lambda _s: cap)
    assert cam.grab() is True and cam.read() == "frame"
    assert (cap.grabs, cap.reads) == (1, 1)


class FakePipeline:
    """Stands in for VisionPipeline (no MediaPipe): wants every 3rd frame."""

    def __init__(self, stop, frames):
        self.stop, self.frames, self.calls, self.processed = stop, frames, 0, 0

    def wants_frame(self, now):
        self.calls += 1
        if self.calls >= self.frames:
            self.stop.set()  # the loop finishes this frame, then exits
        return self.calls % 3 == 0

    def process(self, frame, now):
        self.processed += 1
        return [("robot/state", "POSTURE_OK")]

    def close(self):
        pass


def test_shared_camera_loop_grabs_unwanted_frames(monkeypatch):
    """#24/#26: one camera loop for main.py and standalone mode; frames nobody needs aren't decoded."""
    import threading

    monkeypatch.setattr(vp.time, "sleep", lambda s: None)
    stop = threading.Event()
    cap = CountingCapture()
    published = []
    pipeline = FakePipeline(stop, frames=30)
    vp.run_vision(stop, lambda t, p: published.append((t, p)),
                  camera=vp.ResilientCamera(opener=lambda _s: cap, on_state=lambda s: published.append(("state", s))),
                  pipeline=pipeline)
    assert cap.reads == pipeline.processed == 10
    assert cap.grabs == 20
    assert published[0] == ("state", "UP")


def test_pose_runs_at_about_5_hz(monkeypatch):
    """#24: pose estimation is time-based (~5 Hz) and colour conversion only runs when it's due."""
    import sys

    calls = {"pose": 0, "convert": 0}

    class Pose:
        def __init__(self, **kw):
            pass

        def process(self, rgb):
            calls["pose"] += 1
            return SimpleNamespace(pose_landmarks=None)

        def close(self):
            pass

    fake = SimpleNamespace(__version__="0.10.18", solutions=SimpleNamespace(pose=SimpleNamespace(Pose=Pose)))
    monkeypatch.setitem(sys.modules, "mediapipe", fake)

    def convert(frame, code):
        calls["convert"] += 1
        return frame

    monkeypatch.setattr(vp.cv2, "cvtColor", convert)
    pipe = vp.VisionPipeline(face_detection=False)
    frame = SimpleNamespace(shape=(480, 640, 3))
    for i in range(30):  # 1 s of video at 30 fps
        pipe.process(frame, i / 30)
    assert calls["pose"] == calls["convert"] == 5


def test_posture_metrics_computed_once(monkeypatch):
    """#24: metrics used to be computed twice per pose frame."""
    calls = []
    real = vp.posture_metrics

    def counting(landmarks):
        calls.append(1)
        return real(landmarks)

    monkeypatch.setattr(vp, "posture_metrics", counting)
    assert vp.judge_posture(real(person(0.2, 0.5))) is False
    assert calls == []  # judge_posture works on metrics, it doesn't recompute them
