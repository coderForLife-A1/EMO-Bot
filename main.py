"""EMO-Bot entry point: starts and supervises every runtime task in one asyncio process.

Critical tasks (the robot stops if they crash): serial link to the controller, behavior tree.
Optional tasks (logged and skipped if they crash): vision, wake word, cloud speech pipeline.
Run with `python main.py`; settings come from `.env` via config.py.
"""
import config  # noqa: I001  (must be first: loads .env before other modules read settings)

import asyncio
import contextlib
import logging
import queue
import signal
import threading
import time
from typing import Awaitable, Callable

import paho.mqtt.client as mqtt

from api_routing_task import SAY_JOB, api_routing_task
from behavior_tree_module import (
    AUDIO_EMERGENCY_STOP,
    AUDIO_FALLEN,
    AUDIO_IMU_FAULT,
    AUDIO_POSTURE_WARNING,
    CommandBus,
    SharedState,
    apply_serial_line,
    build_mqtt_client as build_behavior_mqtt_client,
    build_tree,
    clear_conversation,
    on_nano_reset,
    safe_apply,
)
from mqtt_client import make_client, stop_client
from serial_module import offer, offer_urgent, serial_task


logger = logging.getLogger(__name__)

# What the robot says for each behavior-tree audio cue
CUE_PHRASES = {
    AUDIO_POSTURE_WARNING: "Hey, let's sit up a little straighter.",
    AUDIO_EMERGENCY_STOP: "Emergency stop.",
    AUDIO_IMU_FAULT: "My balance sensor stopped working, so I'm resting my legs.",
    AUDIO_FALLEN: "Whoops, I fell over. Could you stand me back up?",
}


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
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except (NotImplementedError, RuntimeError):
            # Windows event loops lack add_signal_handler; hop onto the loop thread safely instead.
            # signal.signal only works in the main thread; elsewhere the embedder handles shutdown.
            with contextlib.suppress(ValueError):
                signal.signal(sig, lambda _s, _f: loop.call_soon_threadsafe(_request_shutdown))


def _serial_line_handler(state: SharedState, publisher: mqtt.Client) -> Callable[[str], None]:
    """Route every controller line: replies/events update the behavior tree; events and refusals are
    published on robot/locomotion/event, telemetry on robot/locomotion/telemetry, ToF distances on
    robot/sensor/distance."""

    def handle(line: str) -> None:
        if line.startswith("T,"):
            publisher.publish(config.TOPIC_LOCOMOTION_TELEMETRY, line, qos=0, retain=False)
            return
        if line.startswith("ACK,D,"):
            publisher.publish(config.TOPIC_DISTANCE, line[6:], qos=0, retain=False)
            return
        if line.startswith(("EVT,", "NACK,", "READY")):
            logger.info("Controller: %s", line)
            publisher.publish(config.TOPIC_LOCOMOTION_EVENT, line, qos=0, retain=False)
        apply_serial_line(state, line)

    return handle


async def vision_task(publisher: mqtt.Client) -> None:
    from vision_posture_module import publish_options, run_vision

    publish_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=200)
    loop = asyncio.get_running_loop()
    stop_flag = threading.Event()

    def publish(topic: str, payload: str) -> None:  # called on the camera thread
        loop.call_soon_threadsafe(offer, publish_queue, (topic, payload))

    worker = asyncio.create_task(asyncio.to_thread(run_vision, stop_flag, publish), name="vision_worker")

    getter: asyncio.Task | None = None
    try:
        while True:
            getter = asyncio.create_task(publish_queue.get())
            done, _ = await asyncio.wait((getter, worker), return_when=asyncio.FIRST_COMPLETED)
            if worker in done:
                getter.cancel()
                worker.result()  # surface the camera/mediapipe error to the supervisor
                raise RuntimeError("Vision worker exited unexpectedly")
            topic, payload = getter.result()
            publisher.publish(topic, payload, **publish_options(topic))
    finally:
        if getter is not None:
            getter.cancel()
        stop_flag.set()
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(asyncio.shield(worker), timeout=2.0)


def queue_cue(speech_queue: asyncio.Queue, phrase: str) -> None:
    """Queue a spoken cue, or drop the cue itself when the queue is full.

    Never evict older jobs: one of them may be a recording (LISTEN_JOB) whose completion releases
    the wake-word listener.
    """
    try:
        speech_queue.put_nowait((SAY_JOB, phrase))
    except asyncio.QueueFull:
        logger.warning("Speech queue full; not saying %r", phrase)


async def behavior_tree_task(
    state: SharedState,
    bus: CommandBus,
    serial_queue: asyncio.Queue[str],
    speech_queue: asyncio.Queue,
    publisher: mqtt.Client,
) -> None:
    tree = build_tree(state, bus)
    loop = asyncio.get_running_loop()
    # MQTT callbacks run on paho's thread; apply them on this loop so SharedState has one writer.
    mqtt_client = build_behavior_mqtt_client(
        lambda topic, payload: loop.call_soon_threadsafe(safe_apply, state, topic, payload))

    period = 0.1  # 10Hz
    next_tick = time.monotonic()

    try:
        while True:
            tree.tick()

            while True:
                try:
                    command = bus.motor_queue.get_nowait()
                except queue.Empty:
                    break
                if command == "E":
                    offer_urgent(serial_queue, command)  # E-stop jumps ahead of everything queued
                else:
                    offer(serial_queue, command)  # never block the tree on a slow/dead serial link

            while True:
                try:
                    cue = bus.audio_queue.get_nowait()
                except queue.Empty:
                    break
                publisher.publish(config.TOPIC_AUDIO_INTENT, cue, qos=0, retain=False)
                if cue in CUE_PHRASES:
                    queue_cue(speech_queue, CUE_PHRASES[cue])

            next_tick += period
            sleep_time = next_tick - time.monotonic()
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
            else:
                next_tick = time.monotonic()
                await asyncio.sleep(0)
    finally:
        stop_client(mqtt_client)


async def _run_guarded(
    name: str,
    coroutine_factory: Callable[[], Awaitable[None]],
    shutdown_event: asyncio.Event,
    critical: bool,
    on_crash: Callable[[], None] | None = None,
) -> None:
    try:
        await coroutine_factory()
    except asyncio.CancelledError:
        raise
    except Exception:
        if critical:
            logger.exception("Critical task %s crashed; shutting down", name)
            shutdown_event.set()
        else:
            logger.exception("Optional task %s crashed; robot continues without it", name)
        if on_crash is not None:
            on_crash()


async def main() -> None:
    _configure_logging()

    shutdown_event = asyncio.Event()
    _install_signal_handlers(shutdown_event)

    state = SharedState()
    bus = CommandBus()
    serial_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
    speech_queue: asyncio.Queue = asyncio.Queue(maxsize=20)
    conversation_busy = threading.Event()
    publisher = make_client("robot-main")

    def end_conversation() -> None:
        # The speech side died: nobody will lower the wake flag, so do it here.
        clear_conversation(state)
        conversation_busy.clear()
        publisher.publish(config.TOPIC_WAKE_FLAG, "0", qos=0, retain=False)

    # (name, factory, critical, on_crash)
    specs: list[tuple[str, Callable[[], Awaitable[None]], bool, Callable[[], None] | None]] = [
        ("serial_task", lambda: serial_task(
            serial_queue,
            on_connect=lambda: on_nano_reset(state),
            on_line=_serial_line_handler(state, publisher),
        ), True, None),
        ("behavior_tree_task",
         lambda: behavior_tree_task(state, bus, serial_queue, speech_queue, publisher), True, None),
        ("api_routing_task", lambda: api_routing_task(speech_queue, publisher, conversation_busy), False,
         end_conversation),
    ]
    if config.ENABLE_VISION:
        specs.append(("vision_task", lambda: vision_task(publisher), False, None))
    else:
        logger.info("Vision disabled (ENABLE_VISION=0)")
    if config.ENABLE_AUDIO:
        from audio_trigger_task import audio_trigger_task

        specs.append(("audio_trigger_task",
                      lambda: audio_trigger_task(publisher, speech_queue, conversation_busy), False,
                      end_conversation))
    else:
        logger.info("Wake word disabled (ENABLE_AUDIO=0)")

    worker_tasks = [
        asyncio.create_task(_run_guarded(name, factory, shutdown_event, critical, on_crash), name=name)
        for name, factory, critical, on_crash in specs
    ]
    logger.info("EMO-Bot running: %s (serial=%s)", ", ".join(s[0] for s in specs), config.SERIAL_PORT)

    try:
        await shutdown_event.wait()
    finally:
        for task in worker_tasks:
            task.cancel()
        await asyncio.gather(*worker_tasks, return_exceptions=True)

        stop_client(publisher)
        logger.info("EMO-Bot stopped")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
