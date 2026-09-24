"""Wake-word listener: Porcupine hears the wake word, then speech is recorded until the user stops talking.

Recording ends after END_SILENCE_SECONDS of quiet once speech started, or at RECORD_SECONDS at most.
If nobody speaks within NO_SPEECH_SECONDS nothing is sent, so Whisper never transcribes silence.

Publishes robot/audio/wake_flag=1 so the robot stands still, and hands the recording to
api_routing_task. Run standalone with `python audio_trigger_task.py` to test the microphone.
"""
import asyncio
import io
import json
import logging
import struct
import threading
import wave
from typing import Optional

import numpy as np
import paho.mqtt.client as mqtt

import config
from mqtt_client import make_client, stop_client

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
RECORD_SECONDS = 8  # longest recording
NO_SPEECH_SECONDS = 3.0  # nobody spoke by then: give up
END_SILENCE_SECONDS = 0.8  # this much quiet after speech ends the recording

LISTEN_JOB = "listen"  # must match api_routing_task.LISTEN_JOB (kept here to avoid importing httpx)


def build_mqtt_client(client_id: str = "robot-audio-trigger") -> mqtt.Client:
    return make_client(client_id)


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


def _rms(chunk: bytes) -> float:
    samples = np.frombuffer(chunk, dtype="<i2").astype(np.float32)
    return float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0


def _record_after_wake(stream, frame_length: int) -> Optional[bytes]:
    """Record until the user stops talking and return WAV bytes (kept in memory, never on disk).

    Returns None if nobody spoke within NO_SPEECH_SECONDS. A frame counts as speech when its RMS level
    reaches config.SPEECH_RMS_THRESHOLD.
    """
    max_frames = SAMPLE_RATE * RECORD_SECONDS
    no_speech_frames = int(SAMPLE_RATE * NO_SPEECH_SECONDS)
    end_silence_frames = int(SAMPLE_RATE * END_SILENCE_SECONDS)
    recorded = bytearray()
    total = quiet = 0
    heard = False
    loudest = 0.0
    while total < max_frames:
        frames_to_read = min(frame_length, max_frames - total)
        audio_chunk, _overflowed = stream.read(frames_to_read)
        recorded.extend(audio_chunk)
        total += frames_to_read
        level = _rms(audio_chunk)
        loudest = max(loudest, level)
        if level >= config.SPEECH_RMS_THRESHOLD:
            heard, quiet = True, 0
        else:
            quiet += frames_to_read
        if heard and quiet >= end_silence_frames:
            break
        if not heard and total >= no_speech_frames:
            logger.info("No speech after the wake word (loudest RMS %.0f, threshold %.0f)",
                        loudest, config.SPEECH_RMS_THRESHOLD)
            return None
    logger.debug("Recorded %.1f s (loudest RMS %.0f)", total / SAMPLE_RATE, loudest)
    return _write_wav_to_memory(bytes(recorded))


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

    pcm_format = struct.Struct(f"<{porcupine.frame_length}h")  # built once, not per audio frame
    try:
        stream.start()
        logger.info("Wake-word listener started (mic=%s)", config.AUDIO_INPUT_DEVICE or "default")
        while not stop_event.is_set():
            pcm, _overflowed = stream.read(porcupine.frame_length)
            if busy_event.is_set():
                continue  # the robot is thinking/speaking; don't wake on its own voice
            if porcupine.process(pcm_format.unpack_from(pcm)) < 0:
                continue

            busy_event.set()
            loop.call_soon_threadsafe(events.put_nowait, ("wake", None))
            wav_bytes = _record_after_wake(stream, porcupine.frame_length)
            loop.call_soon_threadsafe(events.put_nowait, ("recorded", wav_bytes))
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
    """Wake word -> record until quiet -> queue (LISTEN_JOB, wav_bytes) for api_routing_task.

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
                logger.info("Wake word detected; recording (up to %ss)", RECORD_SECONDS)
                mqtt_client.publish(config.TOPIC_WAKE_FLAG, "1", qos=0, retain=False)
                publish_state(mqtt_client, "listening")
            elif value is None:  # nobody spoke: end the conversation here, nothing to send
                mqtt_client.publish(config.TOPIC_WAKE_FLAG, "0", qos=0, retain=False)
                publish_state(mqtt_client, "no_speech")
                busy_event.clear()
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
            _, wav_bytes = await jobs.get()
            print(f"Recorded {len(wav_bytes)} bytes of WAV")  # silence is logged instead, never queued
            busy.clear()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        stop_client(client)


if __name__ == "__main__":
    try:
        asyncio.run(_standalone())
    except KeyboardInterrupt:
        pass
