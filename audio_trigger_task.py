import asyncio
import io
import json
import os
import struct
import tempfile
import threading
import wave
from pathlib import Path

import paho.mqtt.client as mqtt
import pvporcupine
import sounddevice as sd


ALSA_DEVICE = "plughw:0"
MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883
AUDIO_STATE_TOPIC = "robot/audio/state"
SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
RECORD_SECONDS = 5

PORCUPINE_ACCESS_KEY = os.getenv("PORCUPINE_ACCESS_KEY", "")
PORCUPINE_KEYWORD_PATH = os.getenv("PORCUPINE_KEYWORD_PATH", "")

llm_processing_queue: asyncio.Queue[str] = asyncio.Queue()


def build_mqtt_client() -> mqtt.Client:
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="robot-audio-trigger",
    )
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()
    return client


def _create_porcupine() -> pvporcupine.Porcupine:
    if PORCUPINE_KEYWORD_PATH:
        return pvporcupine.create(
            access_key=PORCUPINE_ACCESS_KEY,
            keyword_paths=[PORCUPINE_KEYWORD_PATH],
        )
    return pvporcupine.create(
        access_key=PORCUPINE_ACCESS_KEY,
        keywords=["porcupine"],
    )


def _write_wav_to_memory(samples: bytes) -> bytes:
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as wav_file:
        wav_file.setnchannels(CHANNELS)
        wav_file.setsampwidth(SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(samples)
    return wav_buffer.getvalue()


def _record_after_wake(stream: sd.RawInputStream, frame_length: int) -> str:
    sample_count = SAMPLE_RATE * RECORD_SECONDS
    recorded = bytearray()
    remaining = sample_count

    while remaining > 0:
        frames_to_read = min(frame_length, remaining)
        audio_chunk, _overflowed = stream.read(frames_to_read)
        recorded.extend(audio_chunk)
        remaining -= frames_to_read

    wav_bytes = _write_wav_to_memory(bytes(recorded))
    temp_dir = "/dev/shm" if Path("/dev/shm").is_dir() else None
    with tempfile.NamedTemporaryFile(
        mode="wb",
        suffix=".wav",
        prefix="robot_input_",
        dir=temp_dir,
        delete=False,
    ) as wav_file:
        wav_file.write(wav_bytes)
        return wav_file.name


def _audio_worker(
    stop_event: threading.Event,
    loop: asyncio.AbstractEventLoop,
    wake_queue: asyncio.Queue[None],
    recording_queue: asyncio.Queue[str],
) -> None:
    porcupine = _create_porcupine()
    stream = sd.RawInputStream(
        samplerate=porcupine.sample_rate,
        blocksize=porcupine.frame_length,
        device=ALSA_DEVICE,
        channels=CHANNELS,
        dtype="int16",
    )

    try:
        stream.start()
        while not stop_event.is_set():
            pcm, _overflowed = stream.read(porcupine.frame_length)
            pcm_unpacked = struct.unpack_from("h" * porcupine.frame_length, pcm)
            if porcupine.process(pcm_unpacked) < 0:
                continue

            loop.call_soon_threadsafe(wake_queue.put_nowait, None)
            wav_path = _record_after_wake(stream, porcupine.frame_length)
            loop.call_soon_threadsafe(recording_queue.put_nowait, wav_path)
    finally:
        stream.stop()
        stream.close()
        porcupine.delete()


async def audio_trigger_task(
    mqtt_client: mqtt.Client,
    llm_processing_queue: asyncio.Queue[str],
) -> None:
    loop = asyncio.get_running_loop()
    stop_event = threading.Event()
    wake_queue: asyncio.Queue[None] = asyncio.Queue()
    recording_queue: asyncio.Queue[str] = asyncio.Queue()
    worker = asyncio.create_task(
        asyncio.to_thread(
            _audio_worker,
            stop_event,
            loop,
            wake_queue,
            recording_queue,
        ),
        name="porcupine_audio_worker",
    )

    try:
        while True:
            wake_waiter = asyncio.create_task(wake_queue.get())
            recording_waiter = asyncio.create_task(recording_queue.get())
            done, pending = await asyncio.wait(
                (wake_waiter, recording_waiter),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for waiter in pending:
                waiter.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

            for completed in done:
                result = completed.result()
                if result is None:
                    mqtt_client.publish(
                        AUDIO_STATE_TOPIC,
                        json.dumps({"status": "listening"}),
                        qos=0,
                        retain=False,
                    )
                else:
                    await llm_processing_queue.put(result)
    finally:
        stop_event.set()
        await asyncio.gather(worker, return_exceptions=True)


def setup_audio_trigger() -> tuple[mqtt.Client, asyncio.Queue[str], asyncio.Task[None]]:
    mqtt_client = build_mqtt_client()
    task = asyncio.create_task(
        audio_trigger_task(mqtt_client, llm_processing_queue),
        name="audio_trigger_task",
    )
    return mqtt_client, llm_processing_queue, task
