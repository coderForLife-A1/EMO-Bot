import asyncio
import contextlib
import queue
import signal
import threading
import time
from dataclasses import dataclass
from typing import Optional

import cv2
import mediapipe as mp
import paho.mqtt.client as mqtt
import serial

from behavior_tree_module import CommandBus, SharedState, apply_topic_payload, build_tree
from vision_posture_module import (
    FACE_PUBLISH_MIN_INTERVAL_S,
    POSE_EVERY_N_FRAMES,
    POSTURE_CONFIRM_FRAMES,
    TOPIC_FACE_ERROR,
    TOPIC_STATE,
    is_poor_posture,
    open_v4l2_camera,
    select_primary_face,
)


MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883
SERIAL_PORT = "/dev/ttyUSB0"
SERIAL_BAUD = 115200


@dataclass
class AppContext:
    shutdown_event: asyncio.Event
    motor_queue: asyncio.Queue[str]
    mqtt_client: mqtt.Client
    mqtt_incoming_queue: asyncio.Queue[tuple[str, bytes]]
    vision_publish_queue: asyncio.Queue[tuple[str, str]]
    intelligence_state: SharedState
    intelligence_bus: CommandBus
    audio_command_topic: str
    intelligence_tree: object


def build_mqtt_client(loop: asyncio.AbstractEventLoop, incoming_queue: asyncio.Queue[tuple[str, bytes]]) -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="emo-bot-main")

    def on_connect(client: mqtt.Client, _userdata, _flags, reason_code, _properties) -> None:
        if reason_code == 0:
            client.subscribe("robot/#")
            client.subscribe("emo/behavior/#")
        else:
            print(f"MQTT connect failed: {reason_code}")

    def on_message(_client: mqtt.Client, _userdata, msg: mqtt.MQTTMessage) -> None:
        # Marshal broker messages from paho's network thread into asyncio space.
        loop.call_soon_threadsafe(incoming_queue.put_nowait, (msg.topic, msg.payload))

    client.on_connect = on_connect
    client.on_message = on_message
    return client


async def sensor_task(ctx: AppContext) -> None:
    while not ctx.shutdown_event.is_set():
        ctx.mqtt_client.publish("robot/sensors/heartbeat", "online", qos=0, retain=False)
        await asyncio.sleep(1.0)


def _enqueue_vision_message(out_queue: asyncio.Queue[tuple[str, str]], topic: str, payload: str) -> None:
    with contextlib.suppress(asyncio.QueueFull):
        out_queue.put_nowait((topic, payload))


def vision_worker(stop_flag: threading.Event, loop: asyncio.AbstractEventLoop, out_queue: asyncio.Queue[tuple[str, str]]) -> None:
    cap = open_v4l2_camera("/dev/video0")
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
        while not stop_flag.is_set():
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
                loop.call_soon_threadsafe(_enqueue_vision_message, out_queue, TOPIC_FACE_ERROR, f"{x_err},{y_err}")
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
                    loop.call_soon_threadsafe(_enqueue_vision_message, out_queue, TOPIC_STATE, "POSTURE_POOR")
                    posture_alert_sent = True

            frame_idx += 1
    finally:
        face_detector.close()
        pose_detector.close()
        cap.release()


async def vision_task(ctx: AppContext) -> None:
    loop = asyncio.get_running_loop()
    stop_flag = threading.Event()
    worker_task = asyncio.create_task(
        asyncio.to_thread(vision_worker, stop_flag, loop, ctx.vision_publish_queue),
        name="vision_worker",
    )

    try:
        while not ctx.shutdown_event.is_set():
            try:
                topic, payload = await asyncio.wait_for(ctx.vision_publish_queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue

            ctx.mqtt_client.publish(topic, payload, qos=0, retain=False)
            ctx.vision_publish_queue.task_done()
    finally:
        stop_flag.set()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(worker_task, timeout=2.0)
        if not worker_task.done():
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)


async def audio_task(ctx: AppContext) -> None:
    while not ctx.shutdown_event.is_set():
        # Placeholder for ASR/TTS pipeline. Keep this non-blocking.
        await asyncio.sleep(0.1)


async def intelligence_task(ctx: AppContext) -> None:
    period = 0.1  # 10Hz
    next_tick = time.monotonic()

    while not ctx.shutdown_event.is_set():
        ctx.intelligence_tree.tick()

        while not ctx.intelligence_bus.motor_queue.empty():
            try:
                command = ctx.intelligence_bus.motor_queue.get_nowait()
            except queue.Empty:
                break
            await ctx.motor_queue.put(command)

        while not ctx.intelligence_bus.audio_queue.empty():
            try:
                command = ctx.intelligence_bus.audio_queue.get_nowait()
            except queue.Empty:
                break
            ctx.mqtt_client.publish(ctx.audio_command_topic, command, qos=0, retain=False)

        next_tick += period
        sleep_time = next_tick - time.monotonic()
        if sleep_time > 0:
            await asyncio.sleep(sleep_time)
        else:
            next_tick = time.monotonic()
            await asyncio.sleep(0)


async def serial_task(ctx: AppContext, serial_port: serial.Serial) -> None:
    while not ctx.shutdown_event.is_set():
        try:
            command = await asyncio.wait_for(ctx.motor_queue.get(), timeout=0.2)
        except asyncio.TimeoutError:
            continue

        frame = command.strip() + "\n"
        await asyncio.to_thread(serial_port.write, frame.encode("ascii", errors="ignore"))
        ctx.motor_queue.task_done()


async def mqtt_router_task(ctx: AppContext) -> None:
    while not ctx.shutdown_event.is_set():
        try:
            topic, payload = await asyncio.wait_for(ctx.mqtt_incoming_queue.get(), timeout=0.2)
        except asyncio.TimeoutError:
            continue

        payload_text = payload.decode("utf-8", errors="ignore").strip()

        # External motor command ingress (if another module publishes explicit joint frames).
        if topic in {"emo/behavior/motor_cmd", "robot/behavior/motor_cmd"} and payload_text:
            await ctx.motor_queue.put(payload_text)

        # Feed intelligence module state updates from MQTT topic stream.
        apply_topic_payload(ctx.intelligence_state, topic, payload_text)

        ctx.mqtt_incoming_queue.task_done()


def install_signal_handlers(shutdown_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def request_shutdown() -> None:
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, request_shutdown)


async def main() -> None:
    shutdown_event = asyncio.Event()
    motor_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
    mqtt_incoming_queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue(maxsize=500)
    vision_publish_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=500)
    intelligence_state = SharedState()
    intelligence_bus = CommandBus()
    intelligence_tree = build_tree(intelligence_state, intelligence_bus)

    install_signal_handlers(shutdown_event)

    loop = asyncio.get_running_loop()
    mqtt_client = build_mqtt_client(loop, mqtt_incoming_queue)
    mqtt_client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    mqtt_client.loop_start()

    serial_port: Optional[serial.Serial] = None
    tasks: list[asyncio.Task] = []

    try:
        serial_port = await asyncio.to_thread(
            serial.Serial,
            SERIAL_PORT,
            SERIAL_BAUD,
            timeout=0,
            write_timeout=0,
        )

        ctx = AppContext(
            shutdown_event=shutdown_event,
            motor_queue=motor_queue,
            mqtt_client=mqtt_client,
            mqtt_incoming_queue=mqtt_incoming_queue,
            vision_publish_queue=vision_publish_queue,
            intelligence_state=intelligence_state,
            intelligence_bus=intelligence_bus,
            audio_command_topic="robot/audio/intent",
            intelligence_tree=intelligence_tree,
        )

        tasks = [
            asyncio.create_task(sensor_task(ctx), name="sensor_task"),
            asyncio.create_task(vision_task(ctx), name="vision_task"),
            asyncio.create_task(intelligence_task(ctx), name="intelligence_task"),
            asyncio.create_task(audio_task(ctx), name="audio_task"),
            asyncio.create_task(serial_task(ctx, serial_port), name="serial_task"),
            asyncio.create_task(mqtt_router_task(ctx), name="mqtt_router_task"),
        ]

        await shutdown_event.wait()
    finally:
        shutdown_event.set()

        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        mqtt_client.loop_stop()
        with contextlib.suppress(Exception):
            mqtt_client.disconnect()

        if serial_port is not None and serial_port.is_open:
            await asyncio.to_thread(serial_port.close)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # asyncio.run() already triggers cancellation and cleanup in main().
        pass