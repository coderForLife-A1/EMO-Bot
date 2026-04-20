import time
from typing import Optional, Tuple

import cv2
import mediapipe as mp
import paho.mqtt.client as mqtt


MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883
TOPIC_STATE = "robot/state"
TOPIC_FACE_ERROR = "robot/vision/face_error"

CAMERA_DEVICE = "/dev/video0"
FRAME_WIDTH = 640
FRAME_HEIGHT = 480
FRAME_FPS = 30

POSE_EVERY_N_FRAMES = 2
FACE_PUBLISH_MIN_INTERVAL_S = 0.03
POSTURE_CONFIRM_FRAMES = 3


def build_mqtt_client() -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="robot-vision-posture")
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()
    return client


def open_v4l2_camera(device: str) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
    if not cap.isOpened():
        raise RuntimeError(f"Failed to open CSI camera via V4L2 at {device}")

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, FRAME_FPS)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


def select_primary_face(
    detections,
    frame_w: int,
    frame_h: int,
) -> Optional[Tuple[int, int]]:
    if not detections:
        return None

    best_score = -1.0
    best_center = None
    for det in detections:
        rel_box = det.location_data.relative_bounding_box
        xmin = max(0.0, rel_box.xmin)
        ymin = max(0.0, rel_box.ymin)
        width = max(0.0, rel_box.width)
        height = max(0.0, rel_box.height)

        area = width * height
        score = float(det.score[0]) * area
        if score > best_score:
            cx = int((xmin + width * 0.5) * frame_w)
            cy = int((ymin + height * 0.5) * frame_h)
            best_center = (cx, cy)
            best_score = score

    return best_center


def is_poor_posture(pose_landmarks) -> Optional[bool]:
    if pose_landmarks is None:
        return None

    lm = mp.solutions.pose.PoseLandmark
    landmarks = pose_landmarks.landmark

    left_shoulder = landmarks[lm.LEFT_SHOULDER.value]
    right_shoulder = landmarks[lm.RIGHT_SHOULDER.value]
    nose = landmarks[lm.NOSE.value]

    if (
        left_shoulder.visibility < 0.5
        or right_shoulder.visibility < 0.5
        or nose.visibility < 0.5
    ):
        return None

    shoulder_mid_x = (left_shoulder.x + right_shoulder.x) * 0.5
    shoulder_mid_y = (left_shoulder.y + right_shoulder.y) * 0.5

    shoulder_tilt = abs(left_shoulder.y - right_shoulder.y)
    neck_forward_offset = abs(nose.x - shoulder_mid_x)
    head_drop = nose.y - shoulder_mid_y

    # Lightweight heuristic for slouching: uneven shoulders, forward head, or dropped head.
    return (
        shoulder_tilt > 0.07
        or neck_forward_offset > 0.12
        or head_drop > -0.06
    )


def run() -> None:
    mqtt_client = build_mqtt_client()
    cap = open_v4l2_camera(CAMERA_DEVICE)

    face_detector = mp.solutions.face_detection.FaceDetection(
        model_selection=0,
        min_detection_confidence=0.5,
    )
    pose_detector = mp.solutions.pose.Pose(
        static_image_mode=False,
        model_complexity=0,
        smooth_landmarks=True,
        enable_segmentation=False,
        min_detection_confidence=0.5,
        min_tracking_confidence=0.5,
    )

    last_face_pub = 0.0
    poor_posture_streak = 0
    posture_alert_sent = False
    frame_idx = 0

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.01)
                continue

            frame_h, frame_w = frame.shape[:2]
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

            face_result = face_detector.process(rgb)
            center = select_primary_face(face_result.detections, frame_w, frame_h)
            now = time.monotonic()
            if center is not None and (now - last_face_pub) >= FACE_PUBLISH_MIN_INTERVAL_S:
                x_err = center[0] - (frame_w // 2)
                y_err = center[1] - (frame_h // 2)
                mqtt_client.publish(TOPIC_FACE_ERROR, f"{x_err},{y_err}", qos=0, retain=False)
                last_face_pub = now

            if frame_idx % POSE_EVERY_N_FRAMES == 0:
                pose_result = pose_detector.process(rgb)
                poor = is_poor_posture(pose_result.pose_landmarks)
                if poor is True:
                    poor_posture_streak += 1
                elif poor is False:
                    poor_posture_streak = 0
                    posture_alert_sent = False

                if poor_posture_streak >= POSTURE_CONFIRM_FRAMES and not posture_alert_sent:
                    mqtt_client.publish(TOPIC_STATE, "POSTURE_POOR", qos=0, retain=False)
                    posture_alert_sent = True

            frame_idx += 1

    except KeyboardInterrupt:
        pass
    finally:
        face_detector.close()
        pose_detector.close()
        cap.release()
        mqtt_client.loop_stop()
        mqtt_client.disconnect()


if __name__ == "__main__":
    run()