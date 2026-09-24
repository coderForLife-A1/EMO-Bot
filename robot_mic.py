"""Push-to-talk on the robot's own microphone (e.g. the USB webcam's mic on the Pi).

The console's talk button starts and stops the recording (MIC_SOURCE=robot); the WAV goes through the
normal LISTEN_JOB pipeline. No wake word needed. Test the mic with `python robot_mic.py`: it lists the
input devices, picks one like the robot will, and records 3 s (`python robot_mic.py 6` for 6 s).
"""
import io
import logging
import sys
import threading
import time
import wave
from typing import Optional

import numpy as np

import config

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000  # what the speech APIs want; the device's own rate is resampled to this
MAX_SECONDS = 15.0
# Names that aren't a real microphone (PortAudio's virtual ALSA devices, HDMI outputs...)
_VIRTUAL = ("default", "sysdefault", "pulse", "pipewire", "dmix", "dsnoop", "hdmi", "vc4")
_PREFERRED = ("usb", "camera", "webcam", "cam", "mic")


def _sounddevice():
    import sounddevice as sd

    return sd


def refresh_devices() -> None:
    """PortAudio lists devices once at start-up: re-scan so a webcam plugged in later is found."""
    sd = _sounddevice()
    try:
        sd._terminate()
        sd._initialize()
    except Exception as exc:  # noqa: BLE001 - worst case we keep the old list
        logger.debug("PortAudio re-scan failed: %s", exc)


def pick_input_device(devices: list[dict], wanted: Optional[str] = None) -> Optional[int]:
    """Index of the microphone to use: AUDIO_INPUT_DEVICE (index or name substring) if set, else the first
    real capture device, USB/camera ones first. None if there is no input device at all."""
    inputs = [(i, d) for i, d in enumerate(devices) if d.get("max_input_channels", 0) > 0]
    if wanted is not None and str(wanted).strip():
        wanted = str(wanted).strip()
        if wanted.isdigit():
            index = int(wanted)
            return index if any(i == index for i, _ in inputs) else None
        for match in (lambda name: name == wanted.lower(), lambda name: wanted.lower() in name):
            for i, d in inputs:
                if match(d["name"].lower()):
                    return i
        return None
    real = [(i, d) for i, d in inputs if not d["name"].lower().startswith(_VIRTUAL)]
    for i, d in real:
        if any(word in d["name"].lower() for word in _PREFERRED):
            return i
    if real:
        return real[0][0]
    return inputs[0][0] if inputs else None


def to_wav(samples: np.ndarray, rate: int) -> bytes:
    """Float mono samples in -1..1 at ``rate`` -> 16 kHz 16-bit mono WAV bytes."""
    if rate != SAMPLE_RATE and len(samples):
        n_out = int(len(samples) * SAMPLE_RATE / rate)
        positions = np.linspace(0, len(samples) - 1, n_out)
        samples = np.interp(positions, np.arange(len(samples)), samples)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class RobotMic:
    """One recording at a time from the robot's microphone. start()/stop() are quick and thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stream = None
        self._chunks: list[np.ndarray] = []
        self._rate = SAMPLE_RATE
        self._started = 0.0
        self.level = 0.0  # 0..1, for the console's listening face
        self.device_name = ""

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def _open(self, device: int):
        sd = _sounddevice()
        info = sd.query_devices(device)
        # Ask for 16 kHz mono; USB mics that can't do that get their native rate/channels, converted here.
        attempts = [(SAMPLE_RATE, 1), (int(info["default_samplerate"]), 1),
                    (int(info["default_samplerate"]), int(info["max_input_channels"]))]
        last_error: Exception = RuntimeError("no settings tried")
        for rate, channels in attempts:
            try:
                stream = sd.InputStream(device=device, samplerate=rate, channels=channels, dtype="float32",
                                        callback=self._callback)
                stream.start()
                return stream, rate, info["name"]
            except Exception as exc:  # noqa: BLE001 - try the next combination
                last_error = exc
        raise RuntimeError(f"can't open microphone {info['name']!r}: {last_error}")

    def _callback(self, indata, frames, time_info, status) -> None:
        mono = indata.mean(axis=1) if indata.shape[1] > 1 else indata[:, 0]
        self._chunks.append(mono.copy())
        self.level = min(1.0, float(np.sqrt(np.mean(mono * mono))) * 6) if len(mono) else 0.0

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                return
            refresh_devices()
            sd = _sounddevice()
            device = pick_input_device(list(sd.query_devices()), config.AUDIO_INPUT_DEVICE)
            if device is None:
                raise RuntimeError("no microphone found on the robot: is the webcam plugged in?")
            self._chunks = []
            self._stream, self._rate, self.device_name = self._open(device)
            self._started = time.monotonic()
            logger.info("Recording on %s (%d Hz)", self.device_name, self._rate)

    def too_long(self) -> bool:
        return self.recording and time.monotonic() - self._started > MAX_SECONDS

    def stop(self) -> Optional[bytes]:
        """Stop and return the recording as a 16 kHz WAV (None if nothing was recording)."""
        with self._lock:
            stream, self._stream = self._stream, None
            if stream is None:
                return None
            try:
                stream.stop()
                stream.close()
            except Exception as exc:  # noqa: BLE001 - keep what was recorded
                logger.warning("Closing the microphone failed: %s", exc)
            self.level = 0.0
            samples = np.concatenate(self._chunks) if self._chunks else np.zeros(0, dtype="float32")
            self._chunks = []
            return to_wav(samples, self._rate)

    def cancel(self) -> None:
        self.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    refresh_devices()
    sd = _sounddevice()
    devices = list(sd.query_devices())
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            print(f"  [{i}] {d['name']} ({d['max_input_channels']} ch, {d['default_samplerate']:.0f} Hz)")
    chosen = pick_input_device(devices, config.AUDIO_INPUT_DEVICE)
    if chosen is None:
        raise SystemExit("No microphone found. Is the webcam plugged in? Check `arecord -l`.")
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
    print(f"Using [{chosen}] {devices[chosen]['name']}. Speak now: recording {seconds:g} s...")
    mic = RobotMic()
    mic.start()
    peak = 0.0
    for _ in range(int(seconds * 10)):
        time.sleep(0.1)
        peak = max(peak, mic.level)
    wav_bytes = mic.stop()
    out = config.REPO_ROOT / "mic_test.wav"
    out.write_bytes(wav_bytes)
    print(f"Saved {out} ({len(wav_bytes)} bytes), loudest level {peak:.2f}"
          + ("  <- very quiet: check the mic/volume (alsamixer)" if peak < 0.05 else ""))
