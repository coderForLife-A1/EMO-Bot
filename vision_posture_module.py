"""Camera vision: MediaPipe pose estimation for posture reminders.

Publishes posture events (robot/state: POSTURE_POOR after 3 s of slouching, POSTURE_OK when it
recovers) and camera health (robot/vision/state: UP/DOWN; the camera is reopened with backoff if it
drops out). Face detection (robot/vision/face_error) is optional: FACE_DETECTION=1. Supports the Pi
CSI camera (picamera2), USB/V4L2 cameras, a webcam on this machine, and the laptop console's webcam
(CAMERA_SOURCE=console: frames arrive over the console's WebSocket, see console_server.py). Run standalone
with `python vision_posture_module.py`.
"""
import bisect
import collections
import logging
import time
from dataclasses import dataclass
from typing import Optional

import cv2

import config

logger = logging.getLogger(__name__)

FRAME_WIDTH = 640
FRAME_HEIGHT = 480
FRAME_FPS = 30

POSE_PERIOD_S = 0.2  # pose at ~5 Hz: posture timing (3 s confirm, 1 s clear) needs no more
FACE_PUBLISH_MIN_INTERVAL_S = 0.03

POSTURE_CONFIRM_S = 3.0  # continuous poor posture before an alert
POSTURE_CLEAR_S = 1.0  # continuous good posture before POSTURE_OK
POSTURE_REMIND_S = 60.0  # re-alert while posture stays poor
POSTURE_ABSENT_RESET_S = 10.0  # user out of view this long -> POSTURE_OK
POSTURE_ABSENT_GRACE_S = 1.0  # out of view this long -> slouch/good timers restart (ignores dropouts)

CONSOLE_SOURCE = "console"
CONSOLE_FRAME_WAIT_S = 0.5  # the page sends ~5 frames/s while its camera is on

MIN_FRAME_WAIT_S = 0.002  # a grab() faster than this didn't wait for a frame (run_vision then sleeps)
CAMERA_FAIL_LIMIT = 30  # consecutive failed reads (~0.3 s) before the camera is reopened
CAMERA_BACKOFF_MAX_S = 30.0

# MediaPipe Pose landmark indices (stable across releases)
NOSE = 0
LEFT_SHOULDER = 11
RIGHT_SHOULDER = 12

# Posture thresholds are ratios of shoulder width, so they don't depend on distance to the camera.
# They are starting points: watch the logged metrics and tune for your desk and camera height.
SHOULDER_TILT_MAX = 0.12  # |left_y - right_y| / shoulder_width (~7 deg of shoulder tilt)
HEAD_LATERAL_MAX = 0.35  # |nose_x - shoulder_mid_x| / shoulder_width (leaning sideways)
HEAD_HEIGHT_MIN = 0.30  # (shoulder_mid_y - nose_y) / shoulder_width, absolute floor
HEAD_DROP_RATIO = 0.75  # poor if head height < 75% of the user's own upright baseline

MEDIAPIPE_HINT = "Install a release with the legacy solutions API: pip install mediapipe==0.10.18"


@dataclass
class PostureMetrics:
    shoulder_tilt: float
    head_lateral: float
    head_height: float


class _Picamera2Capture:
    """cv2.VideoCapture-like wrapper around Picamera2 (Pi 5 CSI cameras go through libcamera)."""

    def __init__(self) -> None:
        from picamera2 import Picamera2  # apt: python3-picamera2

        self._cam = Picamera2()
        # "RGB888" is stored as B,G,R in memory, which is what OpenCV expects.
        cfg = self._cam.create_video_configuration(
            main={"size": (FRAME_WIDTH, FRAME_HEIGHT), "format": "RGB888"},
            controls={"FrameRate": FRAME_FPS},
        )
        self._cam.configure(cfg)
        self._cam.start()

    def read(self):
        return True, self._cam.capture_array()

    def grab(self):
        # Wait for the next frame and drop it without converting it, like cv2's grab(). Returning at once
        # would make run_vision spin a CPU core between pose frames.
        self._cam.capture_request().release()
        return True

    def release(self) -> None:
        self._cam.stop()
        self._cam.close()


class _ConsoleCapture:
    """cv2.VideoCapture-like reader of the frames the laptop console sends (frame_mailbox.CONSOLE_FRAMES).

    A read with no new frame within CONSOLE_FRAME_WAIT_S fails, so ResilientCamera reports the camera DOWN
    when the page's camera is switched off or the page is closed.
    """

    def __init__(self, mailbox=None) -> None:
        from frame_mailbox import CONSOLE_FRAMES

        self.mailbox = mailbox or CONSOLE_FRAMES
        self.seq = self.mailbox.seq  # only frames sent after opening

    def _next(self):
        seq, jpeg = self.mailbox.wait_newer(self.seq, CONSOLE_FRAME_WAIT_S)
        self.seq = seq
        return jpeg

    def read(self):
        import numpy as np

        jpeg = self._next()
        if jpeg is None:
            return False, None
        frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        return frame is not None, frame

    def grab(self):
        return self._next() is not None

    def release(self) -> None:
        pass


def open_v4l2_camera(device: str) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
    if not cap.isOpened():
        raise RuntimeError(f"Failed to open camera via V4L2 at {device}")
    return _configure_capture(cap)


def _configure_capture(cap: cv2.VideoCapture) -> cv2.VideoCapture:
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, FRAME_FPS)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


def open_camera(source: Optional[str] = None):
    """Open "picamera2", "console" (the laptop console's webcam), a webcam index such as "0", or a V4L2 path."""
    source = source if source is not None else config.CAMERA_SOURCE
    if source == CONSOLE_SOURCE:
        return _ConsoleCapture()
    if source == "picamera2":
        return _Picamera2Capture()
    if source.isdigit():
        cap = cv2.VideoCapture(int(source))
        if not cap.isOpened():
            raise RuntimeError(f"Failed to open webcam index {source}")
        return _configure_capture(cap)
    return open_v4l2_camera(source)


def select_primary_face(
    detections,
    frame_w: int,
    frame_h: int,
) -> Optional[tuple[int, int]]:
    """Centre of the most confident, largest face, clamped to the frame."""
    if not detections:
        return None

    best_score = -1.0
    best_center = None
    for det in detections:
        rel_box = det.location_data.relative_bounding_box
        width = max(0.0, rel_box.width)
        height = max(0.0, rel_box.height)
        # Use the unclipped box so a face partly off-frame keeps its true centre.
        cx_rel = min(1.0, max(0.0, rel_box.xmin + width * 0.5))
        cy_rel = min(1.0, max(0.0, rel_box.ymin + height * 0.5))

        score = float(det.score[0]) * width * height
        if score > best_score:
            best_center = (round(cx_rel * frame_w), round(cy_rel * frame_h))
            best_score = score

    return best_center


def posture_metrics(pose_landmarks) -> Optional[PostureMetrics]:
    if pose_landmarks is None:
        return None

    landmarks = pose_landmarks.landmark
    left_shoulder = landmarks[LEFT_SHOULDER]
    right_shoulder = landmarks[RIGHT_SHOULDER]
    nose = landmarks[NOSE]
    if min(left_shoulder.visibility, right_shoulder.visibility, nose.visibility) < 0.5:
        return None

    shoulder_width = abs(left_shoulder.x - right_shoulder.x)
    if shoulder_width < 0.02:  # side-on or too far away to judge
        return None

    shoulder_mid_x = (left_shoulder.x + right_shoulder.x) * 0.5
    shoulder_mid_y = (left_shoulder.y + right_shoulder.y) * 0.5
    return PostureMetrics(
        shoulder_tilt=abs(left_shoulder.y - right_shoulder.y) / shoulder_width,
        head_lateral=abs(nose.x - shoulder_mid_x) / shoulder_width,
        head_height=(shoulder_mid_y - nose.y) / shoulder_width,
    )


def is_poor_posture(pose_landmarks, baseline_head_height: Optional[float] = None) -> Optional[bool]:
    """True/False for poor/good posture, None when the user isn't clearly visible."""
    return judge_posture(posture_metrics(pose_landmarks), baseline_head_height)


def judge_posture(metrics: Optional[PostureMetrics], baseline_head_height: Optional[float] = None) -> Optional[bool]:
    """is_poor_posture() for metrics that were already computed."""
    if metrics is None:
        return None

    min_head_height = HEAD_HEIGHT_MIN
    if baseline_head_height is not None:
        min_head_height = max(min_head_height, baseline_head_height * HEAD_DROP_RATIO)

    return (
        metrics.shoulder_tilt > SHOULDER_TILT_MAX
        or metrics.head_lateral > HEAD_LATERAL_MAX
        or metrics.head_height < min_head_height
    )


class HeadHeightBaseline:
    """The user's upright head height: 90th percentile of one sample per second over ~10 minutes."""

    def __init__(self, window: int = 600, min_samples: int = 30, sample_period_s: float = 1.0) -> None:
        self.min_samples = min_samples
        self.sample_period_s = sample_period_s
        self._samples: collections.deque[float] = collections.deque(maxlen=window)
        self._sorted: list[float] = []
        self._last_sample_t = float("-inf")

    def add(self, head_height: float, now: float) -> None:
        if now - self._last_sample_t < self.sample_period_s:
            return
        self._last_sample_t = now
        if len(self._samples) == self._samples.maxlen:
            self._sorted.pop(bisect.bisect_left(self._sorted, self._samples[0]))
        self._samples.append(head_height)
        bisect.insort(self._sorted, head_height)

    @property
    def value(self) -> Optional[float]:
        if len(self._sorted) < self.min_samples:
            return None
        return self._sorted[int(0.9 * (len(self._sorted) - 1))]


class PostureMonitor:
    """Turns per-frame posture judgements into POSTURE_POOR / POSTURE_OK events with hysteresis."""

    def __init__(
        self,
        confirm_s: float = POSTURE_CONFIRM_S,
        clear_s: float = POSTURE_CLEAR_S,
        remind_s: float = POSTURE_REMIND_S,
        absent_reset_s: float = POSTURE_ABSENT_RESET_S,
    ) -> None:
        self.confirm_s, self.clear_s = confirm_s, clear_s
        self.remind_s, self.absent_reset_s = remind_s, absent_reset_s
        self.alerted = False
        self.poor_since: Optional[float] = None
        self.good_since: Optional[float] = None
        self.absent_since: Optional[float] = None
        self.last_alert = 0.0

    def update(self, poor: Optional[bool], now: float) -> Optional[str]:
        if poor is None:
            if self.absent_since is None:
                self.absent_since = now
            if now - self.absent_since >= POSTURE_ABSENT_GRACE_S:
                # User really gone (not a one-frame dropout): timing starts afresh when they return.
                self.poor_since = self.good_since = None
            if self.alerted and now - self.absent_since >= self.absent_reset_s:
                self.alerted = False
                self.poor_since = None
                return "POSTURE_OK"
            return None
        self.absent_since = None

        if poor:
            self.good_since = None
            if self.poor_since is None:
                self.poor_since = now
            if not self.alerted and now - self.poor_since >= self.confirm_s:
                self.alerted, self.last_alert = True, now
                return "POSTURE_POOR"
            if self.alerted and now - self.last_alert >= self.remind_s:
                self.last_alert = now
                return "POSTURE_POOR"
            return None

        self.poor_since = None
        if self.good_since is None:
            self.good_since = now
        if self.alerted and now - self.good_since >= self.clear_s:
            self.alerted = False
            return "POSTURE_OK"
        return None


class ResilientCamera:
    """Camera that survives unplugging: reopens after repeated read failures, with backoff.

    ``read()`` returns a frame or None (no frame right now; the caller just tries again).
    ``grab()`` takes the next frame without decoding it (for frames nobody will look at).
    ``on_state("UP" | "DOWN")`` is called whenever frames start or stop arriving.
    """

    def __init__(self, source: Optional[str] = None, on_state=None, opener=None) -> None:
        self.source = source
        self.on_state = on_state
        self.opener = opener or open_camera
        self.cap = None
        self.up: Optional[bool] = None
        self.failures = 0
        self.backoff = 1.0
        self.next_open = 0.0

    def _set_up(self, up: bool) -> None:
        if up != self.up:
            self.up = up
            if self.on_state is not None:
                self.on_state("UP" if up else "DOWN")

    def _close(self, now: float) -> None:
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:  # noqa: BLE001
                pass
        self.cap = None
        self.next_open = now + self.backoff
        self.backoff = min(self.backoff * 2, CAMERA_BACKOFF_MAX_S)
        self._set_up(False)

    def read(self):
        return self._fetch(decode=True)

    def grab(self) -> bool:
        return self._fetch(decode=False) is not None

    def _fetch(self, decode: bool):
        now = time.monotonic()
        if self.cap is None:
            if now < self.next_open:
                time.sleep(0.05)
                return None
            try:
                self.cap = self.opener(self.source)
            except Exception as exc:  # noqa: BLE001 - keep retrying, e.g. camera plugged in later
                logger.warning("Camera %r unavailable: %s (retrying in %.0fs)",
                               self.source or config.CAMERA_SOURCE, exc, self.backoff)
                self._close(now)
                return None
            self.failures = 0

        try:
            if decode or not hasattr(self.cap, "grab"):
                ok, frame = self.cap.read()
            else:
                ok, frame = self.cap.grab(), True
        except Exception:  # noqa: BLE001 - e.g. picamera2 raising when the ribbon comes loose
            ok, frame = False, None
        if ok:
            self.failures = 0
            self.backoff = 1.0
            self._set_up(True)
            return frame

        self.failures += 1
        time.sleep(0.01)
        if self.failures >= CAMERA_FAIL_LIMIT:
            logger.warning("Camera stopped delivering frames; reopening in %.0fs", self.backoff)
            self._close(now)
        return None

    def release(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None


class VisionPipeline:
    """Posture monitoring (and optional face detection) on BGR frames.

    ``process()`` returns (topic, payload) messages to publish.
    """

    def __init__(self, face_detection: Optional[bool] = None) -> None:
        import mediapipe as mp

        if not hasattr(mp, "solutions"):
            raise RuntimeError(f"mediapipe {mp.__version__} has no 'solutions' module. {MEDIAPIPE_HINT}")

        if face_detection is None:
            face_detection = config.FACE_DETECTION
        self.face_detector = None
        if face_detection:
            self.face_detector = mp.solutions.face_detection.FaceDetection(
                model_selection=0,
                min_detection_confidence=0.5,
            )
        self.pose_detector = mp.solutions.pose.Pose(
            static_image_mode=False,
            model_complexity=0,
            smooth_landmarks=True,
            enable_segmentation=False,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self.posture = PostureMonitor()
        self.baseline = HeadHeightBaseline()
        self.last_face_pub = 0.0
        self.last_pose = float("-inf")

    def wants_frame(self, now: float) -> bool:
        """False when process() would ignore this frame: grab it without decoding instead."""
        return self.face_detector is not None or now - self.last_pose >= POSE_PERIOD_S

    def process(self, frame, now: float) -> list[tuple[str, str]]:
        messages: list[tuple[str, str]] = []
        if not self.wants_frame(now):
            return messages
        frame_h, frame_w = frame.shape[:2]
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)  # only for frames a detector will look at

        if self.face_detector is not None:
            face_result = self.face_detector.process(rgb)
            center = select_primary_face(face_result.detections, frame_w, frame_h)
            if center is not None and (now - self.last_face_pub) >= FACE_PUBLISH_MIN_INTERVAL_S:
                x_err = center[0] - (frame_w // 2)
                y_err = center[1] - (frame_h // 2)
                messages.append((config.TOPIC_FACE_ERROR, f"{x_err},{y_err}"))
                self.last_face_pub = now

        if now - self.last_pose >= POSE_PERIOD_S:
            self.last_pose = now
            pose_result = self.pose_detector.process(rgb)
            metrics = posture_metrics(pose_result.pose_landmarks)  # once per pose frame
            if metrics is not None:
                self.baseline.add(metrics.head_height, now)
            poor = judge_posture(metrics, self.baseline.value)
            event = self.posture.update(poor, now)
            if event is not None:
                logger.info("Posture %s (metrics=%s, baseline=%s)", event, metrics, self.baseline.value)
                messages.append((config.TOPIC_STATE, event))

        return messages

    def close(self) -> None:
        if self.face_detector is not None:
            self.face_detector.close()
        self.pose_detector.close()


def run_vision(stop, publish, camera=None, pipeline=None) -> None:
    """The camera loop shared by main.py and standalone mode.

    Runs until ``stop.is_set()``. Every message goes to ``publish(topic, payload)``, including
    robot/vision/state UP/DOWN when the camera starts or stops delivering frames. Frames the
    pipeline doesn't need are grabbed without decoding.
    """

    def on_state(state: str) -> None:
        (logger.info if state == "UP" else logger.warning)("Camera %r is %s", config.CAMERA_SOURCE, state)
        publish(config.TOPIC_VISION_STATE, state)

    pipeline = pipeline or VisionPipeline()
    camera = camera or ResilientCamera(on_state=on_state)
    try:
        while not stop.is_set():
            if not pipeline.wants_frame(time.monotonic()):
                started = time.monotonic()
                camera.grab()
                if time.monotonic() - started < MIN_FRAME_WAIT_S:
                    # This camera's grab() didn't wait for a frame: don't spin, wait a frame time instead.
                    time.sleep(1.0 / FRAME_FPS)
                continue
            frame = camera.read()
            if frame is None:
                continue
            for topic, payload in pipeline.process(frame, time.monotonic()):
                publish(topic, payload)
    finally:
        pipeline.close()
        camera.release()


def publish_options(topic: str) -> dict:
    """UP/DOWN is retained so late subscribers still see the camera state."""
    retained = topic == config.TOPIC_VISION_STATE
    return {"qos": 1 if retained else 0, "retain": retained}


def run() -> None:
    """Standalone mode: publish posture events (and face errors if enabled) straight to MQTT."""
    import threading

    from mqtt_client import make_client, stop_client

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    mqtt_client = make_client("robot-vision-posture")
    print(f"Vision running on camera {config.CAMERA_SOURCE!r} (Ctrl+C to stop)")
    try:
        run_vision(threading.Event(), lambda t, p: mqtt_client.publish(t, p, **publish_options(t)))
    except KeyboardInterrupt:
        pass
    finally:
        stop_client(mqtt_client)


if __name__ == "__main__":
    run()
