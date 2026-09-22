"""Camera vision: MediaPipe pose estimation for posture reminders.

Publishes posture events (robot/state: POSTURE_POOR after 3 s of slouching, POSTURE_OK when it
recovers) and camera health (robot/vision/state: UP/DOWN; the camera is reopened with backoff if it
drops out). Face detection (robot/vision/face_error) is optional: FACE_DETECTION=1. Supports the Pi
CSI camera (picamera2), USB/V4L2 cameras and laptop webcams. Run standalone with
`python vision_posture_module.py`.
"""
import bisect
import collections
import logging
import time
from dataclasses import dataclass
from typing import Optional

import cv2
import paho.mqtt.client as mqtt

import config

logger = logging.getLogger(__name__)

# Kept for backwards compatibility with older imports; the source of truth is config.
TOPIC_STATE = config.TOPIC_STATE
TOPIC_FACE_ERROR = config.TOPIC_FACE_ERROR

FRAME_WIDTH = 640
FRAME_HEIGHT = 480
FRAME_FPS = 30

POSE_EVERY_N_FRAMES = 2
FACE_PUBLISH_MIN_INTERVAL_S = 0.03

POSTURE_CONFIRM_S = 3.0  # continuous poor posture before an alert
POSTURE_CLEAR_S = 1.0  # continuous good posture before POSTURE_OK
POSTURE_REMIND_S = 60.0  # re-alert while posture stays poor
POSTURE_ABSENT_RESET_S = 10.0  # user out of view this long -> POSTURE_OK
POSTURE_ABSENT_GRACE_S = 1.0  # out of view this long -> slouch/good timers restart (ignores dropouts)

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


def build_mqtt_client() -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="robot-vision-posture")
    client.connect_async(config.MQTT_HOST, config.MQTT_PORT, keepalive=30)
    client.loop_start()
    return client


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

    def release(self) -> None:
        self._cam.stop()
        self._cam.close()


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
    """Open "picamera2", a webcam index such as "0", or a V4L2 device path."""
    source = source if source is not None else config.CAMERA_SOURCE
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
    metrics = posture_metrics(pose_landmarks)
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
            ok, frame = self.cap.read()
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
        self.frame_idx = 0

    def process(self, frame, now: float) -> list[tuple[str, str]]:
        messages: list[tuple[str, str]] = []
        frame_h, frame_w = frame.shape[:2]
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        if self.face_detector is not None:
            face_result = self.face_detector.process(rgb)
            center = select_primary_face(face_result.detections, frame_w, frame_h)
            if center is not None and (now - self.last_face_pub) >= FACE_PUBLISH_MIN_INTERVAL_S:
                x_err = center[0] - (frame_w // 2)
                y_err = center[1] - (frame_h // 2)
                messages.append((config.TOPIC_FACE_ERROR, f"{x_err},{y_err}"))
                self.last_face_pub = now

        if self.frame_idx % POSE_EVERY_N_FRAMES == 0:
            pose_result = self.pose_detector.process(rgb)
            metrics = posture_metrics(pose_result.pose_landmarks)
            if metrics is not None:
                self.baseline.add(metrics.head_height, now)
            poor = is_poor_posture(pose_result.pose_landmarks, self.baseline.value)
            event = self.posture.update(poor, now)
            if event is not None:
                logger.info("Posture %s (metrics=%s, baseline=%s)", event, metrics, self.baseline.value)
                messages.append((config.TOPIC_STATE, event))

        self.frame_idx += 1
        return messages

    def close(self) -> None:
        if self.face_detector is not None:
            self.face_detector.close()
        self.pose_detector.close()


def run() -> None:
    """Standalone mode: publish posture events (and face errors if enabled) straight to MQTT."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    mqtt_client = build_mqtt_client()
    cap = ResilientCamera(on_state=lambda s: mqtt_client.publish(config.TOPIC_VISION_STATE, s, qos=1, retain=True))
    pipeline = VisionPipeline()
    print(f"Vision running on camera {config.CAMERA_SOURCE!r} (Ctrl+C to stop)")

    try:
        while True:
            frame = cap.read()
            if frame is None:
                continue
            for topic, payload in pipeline.process(frame, time.monotonic()):
                mqtt_client.publish(topic, payload, qos=0, retain=False)
    except KeyboardInterrupt:
        pass
    finally:
        pipeline.close()
        cap.release()
        mqtt_client.loop_stop()
        mqtt_client.disconnect()


if __name__ == "__main__":
    run()
