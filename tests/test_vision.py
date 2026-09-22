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


def test_mediapipe_version_message(monkeypatch):
    import sys

    fake = SimpleNamespace(__version__="9.9.9")
    monkeypatch.setitem(sys.modules, "mediapipe", fake)
    with pytest.raises(RuntimeError, match="0.10.18"):
        vp.VisionPipeline()
