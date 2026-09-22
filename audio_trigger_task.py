"""Wake-word listener: Porcupine hears the wake word, then 5 s of speech is recorded to a WAV file.

Publishes robot/audio/wake_flag=1 so the robot stands still, and hands the recording to
api_routing_task. Run standalone with `python audio_trigger_task.py` to test the microphone.
"""
import asyncio
import io
import json
import logging
import struct
import tempfile
import threading
import wave
from pathlib import Path
from typing import Optional

import paho.mqtt.client as mqtt

import config

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
RECORD_SECONDS = 5

LISTEN_JOB = "listen"  # must match api_routing_task.LISTEN_JOB (kept here to avoid importing httpx)


def build_mqtt_client(client_id: str = "robot-audio-trigger") -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    client.connect_async(config.MQTT_HOST, config.MQTT_PORT, keepalive=30)
    client.loop_start()
    return client


def _create_porcupine():
    import pvporcupine

    if not config.PORCUPINE_ACCESS_KEY:
        raise RuntimeError("PORCUPINE_ACCESS_KEY is not set (get one at https://console.picovoice.ai)")
    if config.PORCUPINE_KEYWORD_PATH:
        return pvporcupine.create(
            access_key=config.PORCUPINE_ACCESS_KEY,
            keyword_paths=[config.PORCUPINE_KEYWORD_PATH],
        )
    return pvporcupine.create(access_key=config.PORCUPINE_ACCESS_KEY, keywords=["porcupine"])


def _write_wav_to_memory(samples: bytes) -> bytes:
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as wav_file:
        wav_file.setnchannels(CHANNELS)
        wav_file.setsampwidth(SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(samples)
    return wav_buffer.getvalue()


def _record_after_wake(stream, frame_length: int) -> str:
    remaining = SAMPLE_RATE * RECORD_SECONDS
    recorded = bytearray()
    while remaining > 0:
        frames_to_read = min(frame_length, remaining)
        audio_chunk, _overflowed = stream.read(frames_to_read)
        recorded.extend(audio_chunk)
        remaining -= frames_to_read

    temp_dir = "/dev/shm" if Path("/dev/shm").is_dir() else None
    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=".wav", prefix="robot_input_", dir=temp_dir, delete=False,
    ) as wav_file:
        wav_file.write(_write_wav_to_memory(bytes(recorded)))
        return wav_file.name


def _audio_worker(
    stop_event: threading.Event,
    busy_event: threading.Event,
    loop: asyncio.AbstractEventLoop,
    events: asyncio.Queue,
) -> None:
    import sounddevice as sd

    porcupine = _create_porcupine()
    stream = sd.RawInputStream(
        samplerate=porcupine.sample_rate,
        blocksize=porcupine.frame_length,
        device=config.AUDIO_INPUT_DEVICE,
        channels=CHANNELS,
        dtype="int16",
    )

    try:
        stream.start()
        logger.info("Wake-word listener started (mic=%s)", config.AUDIO_INPUT_DEVICE or "default")
        while not stop_event.is_set():
            pcm, _overflowed = stream.read(porcupine.frame_length)
            if busy_event.is_set():
                continue  # the robot is thinking/speaking; don't wake on its own voice
            pcm_unpacked = struct.unpack_from("h" * porcupine.frame_length, pcm)
            if porcupine.process(pcm_unpacked) < 0:
                continue

            busy_event.set()
            loop.call_soon_threadsafe(events.put_nowait, ("wake", None))
            wav_path = _record_after_wake(stream, porcupine.frame_length)
            loop.call_soon_threadsafe(events.put_nowait, ("recorded", wav_path))
    finally:
        stream.stop()
        stream.close()
        porcupine.delete()


def publish_state(mqtt_client: mqtt.Client, status: str) -> None:
    mqtt_client.publish(config.TOPIC_AUDIO_STATE, json.dumps({"status": status}), qos=0, retain=False)


async def audio_trigger_task(
    mqtt_client: mqtt.Client,
    llm_processing_queue: asyncio.Queue,
    busy_event: Optional[threading.Event] = None,
) -> None:
    """Wake word -> record 5 s -> queue (LISTEN_JOB, wav_path) for api_routing_task.

    ``busy_event`` stays set from the wake word until api_routing_task has finished replying.
    """
    busy_event = busy_event or threading.Event()
    loop = asyncio.get_running_loop()
    stop_event = threading.Event()
    events: asyncio.Queue = asyncio.Queue()
    worker = asyncio.create_task(
        asyncio.to_thread(_audio_worker, stop_event, busy_event, loop, events),
        name="porcupine_audio_worker",
    )

    getter: asyncio.Task | None = None
    try:
        while True:
            getter = asyncio.create_task(events.get())
            done, _ = await asyncio.wait((getter, worker), return_when=asyncio.FIRST_COMPLETED)
            if worker in done:
                getter.cancel()
                worker.result()  # re-raise the worker's error so the supervisor logs it
                raise RuntimeError("Wake-word worker exited unexpectedly")

            kind, value = getter.result()
            if kind == "wake":
                logger.info("Wake word detected; recording %ss", RECORD_SECONDS)
                mqtt_client.publish(config.TOPIC_WAKE_FLAG, "1", qos=0, retain=False)
                publish_state(mqtt_client, "listening")
            else:
                publish_state(mqtt_client, "processing")
                await llm_processing_queue.put((LISTEN_JOB, value))
    finally:
        if getter is not None:
            getter.cancel()
        stop_event.set()
        await asyncio.gather(worker, return_exceptions=True)


async def _standalone() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    client = build_mqtt_client()
    jobs: asyncio.Queue = asyncio.Queue()
    busy = threading.Event()
    task = asyncio.create_task(audio_trigger_task(client, jobs, busy))
    print("Say the wake word ('porcupine' by default). Ctrl+C to stop.")
    try:
        while True:
            _, wav_path = await jobs.get()
            print(f"Recorded {wav_path}")
            busy.clear()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        client.loop_stop()


if __name__ == "__main__":
    try:
        asyncio.run(_standalone())
    except KeyboardInterrupt:
        pass
