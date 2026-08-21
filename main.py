import asyncio
import contextlib
import logging
import queue
import signal
import threading
import time
from typing import Awaitable, Callable

import cv2
import mediapipe as mp
from dotenv import load_dotenv

from api_routing_task import api_routing_task
from audio_trigger_task import audio_trigger_task, build_mqtt_client as build_audio_mqtt_client
from behavior_tree_module import CommandBus, SharedState, build_mqtt_client as build_behavior_mqtt_client, build_tree
from serial_module import serial_task
from vision_posture_module import (
    FACE_PUBLISH_MIN_INTERVAL_S,
    POSE_EVERY_N_FRAMES,
    POSTURE_CONFIRM_FRAMES,
    TOPIC_FACE_ERROR,
    TOPIC_STATE,
    build_mqtt_client as build_vision_mqtt_client,
    open_v4l2_camera,
    select_primary_face,
    is_poor_posture,
)


logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def _install_signal_handlers(shutdown_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _request_shutdown() -> None:
        if not shutdown_event.is_set():
            logger.info("Shutdown signal received")
            shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_shutdown)

    # Windows can lack loop-level signal handler support for some loops.
    def _sync_signal_handler(_sig, _frame) -> None:
        if not shutdown_event.is_set():
            shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(Exception):
            signal.signal(sig, _sync_signal_handler)


def _vision_worker(
    stop_flag: threading.Event,
    loop: asyncio.AbstractEventLoop,
    out_queue: asyncio.Queue[tuple[str, str]],
) -> None:
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
                loop.call_soon_threadsafe(out_queue.put_nowait, (TOPIC_FACE_ERROR, f"{x_err},{y_err}"))
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
                    loop.call_soon_threadsafe(out_queue.put_nowait, (TOPIC_STATE, "POSTURE_POOR"))
                    posture_alert_sent = True

            frame_idx += 1
    finally:
        face_detector.close()
        pose_detector.close()
        cap.release()


async def vision_task(shutdown_event: asyncio.Event) -> None:
    mqtt_client = build_vision_mqtt_client()
    publish_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=200)
    loop = asyncio.get_running_loop()
    stop_flag = threading.Event()
    worker_task = asyncio.create_task(
        asyncio.to_thread(_vision_worker, stop_flag, loop, publish_queue),
        name="vision_worker",
    )

    try:
        while not shutdown_event.is_set():
            try:
                topic, payload = await asyncio.wait_for(publish_queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue

            mqtt_client.publish(topic, payload, qos=0, retain=False)
            publish_queue.task_done()
    finally:
        stop_flag.set()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(worker_task, timeout=2.0)
        if not worker_task.done():
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)

        mqtt_client.loop_stop()
        with contextlib.suppress(Exception):
            mqtt_client.disconnect()


async def behavior_tree_task(
    serial_queue: asyncio.Queue[str],
    shutdown_event: asyncio.Event,
) -> None:
    state = SharedState()
    bus = CommandBus()
    tree = build_tree(state, bus)
    mqtt_client = build_behavior_mqtt_client(state)

    period = 0.1  # 10Hz
    next_tick = time.monotonic()

    try:
        while not shutdown_event.is_set():
            tree.tick()

            while True:
                try:
                    command = bus.motor_queue.get_nowait()
                except queue.Empty:
                    break
                await serial_queue.put(command)

            while True:
                try:
                    _audio_cmd = bus.audio_queue.get_nowait()
                except queue.Empty:
                    break

            next_tick += period
            sleep_time = next_tick - time.monotonic()
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
            else:
                next_tick = time.monotonic()
                await asyncio.sleep(0)
    finally:
        mqtt_client.loop_stop()
        with contextlib.suppress(Exception):
            mqtt_client.disconnect()


async def _run_guarded(
    name: str,
    coroutine_factory: Callable[[], Awaitable[None]],
    shutdown_event: asyncio.Event,
) -> None:
    try:
        await coroutine_factory()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Task %s crashed", name)
        shutdown_event.set()


async def _shutdown_watcher(shutdown_event: asyncio.Event, worker_tasks: list[asyncio.Task[None]]) -> None:
    await shutdown_event.wait()
    for task in worker_tasks:
        task.cancel()


async def main() -> None:
    load_dotenv()
    _configure_logging()

    shutdown_event = asyncio.Event()
    _install_signal_handlers(shutdown_event)

    serial_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
    audio_in_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=50)

    audio_mqtt_client = build_audio_mqtt_client()

    worker_tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(
            _run_guarded(
                "serial_task",
                lambda: serial_task(serial_queue),
                shutdown_event,
            ),
            name="serial_task",
        ),
        asyncio.create_task(
            _run_guarded(
                "audio_trigger_task",
                lambda: audio_trigger_task(audio_mqtt_client, audio_in_queue),
                shutdown_event,
            ),
            name="audio_trigger_task",
        ),
        asyncio.create_task(
            _run_guarded(
                "api_routing_task",
                lambda: api_routing_task(audio_in_queue),
                shutdown_event,
            ),
            name="api_routing_task",
        ),
        asyncio.create_task(
            _run_guarded(
                "vision_task",
                lambda: vision_task(shutdown_event),
                shutdown_event,
            ),
            name="vision_task",
        ),
        asyncio.create_task(
            _run_guarded(
                "behavior_tree_task",
                lambda: behavior_tree_task(serial_queue, shutdown_event),
                shutdown_event,
            ),
            name="behavior_tree_task",
        ),
    ]

    watcher = asyncio.create_task(
        _shutdown_watcher(shutdown_event, worker_tasks),
        name="shutdown_watcher",
    )

    try:
        await asyncio.gather(*worker_tasks, watcher, return_exceptions=True)
    finally:
        shutdown_event.set()
        for task in worker_tasks:
            task.cancel()
        watcher.cancel()

        await asyncio.gather(*worker_tasks, return_exceptions=True)
        await asyncio.gather(watcher, return_exceptions=True)

        audio_mqtt_client.loop_stop()
        with contextlib.suppress(Exception):
            audio_mqtt_client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
