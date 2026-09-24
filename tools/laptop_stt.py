"""Speech to text on the laptop for EMO, next to Ollama on the laptop's GPU (faster-whisper).

The Pi sends its recording here instead of over the phone's mobile data, so only text leaves the local network.
Same request and response as OpenAI's /v1/audio/transcriptions, so the Pi only needs STT_URL:

    python tools/laptop_stt.py                      # listens on 0.0.0.0:8765
    Pi .env: STT_URL=http://<laptop-ip>:8765/v1

No login, like Ollama: allow the port only from the hotspot's subnet in the firewall (RUNNING.md section 7a).
Needs: pip install faster-whisper nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*" (the last two: CUDA libraries).
"""
import argparse
import email.parser
import email.policy
import io
import json
import logging
import os
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

logger = logging.getLogger("laptop_stt")

TRANSCRIBE_PATH = "/v1/audio/transcriptions"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # the Pi's longest recording (8 s, 16 kHz mono WAV) is ~256 KB

# (audio bytes, language, prompt, temperature) -> text
Transcriber = Callable[[bytes, str, str, float], str]


def parse_form(content_type: str, body: bytes) -> dict[str, bytes]:
    """multipart/form-data -> {field name: raw value}."""
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        f"Content-Type: {content_type}\r\n\r\n".encode() + body)
    if not message.is_multipart():
        raise ValueError("expected multipart/form-data")
    fields = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            fields[name] = part.get_payload(decode=True) or b""
    return fields


class Handler(BaseHTTPRequestHandler):
    server: "SttServer"
    server_version = "EMO-STT"

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        if self.path == "/health":
            self._json(200, {"model": self.server.model_name})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def _discard(self, length: int) -> None:
        """Read an unwanted upload so the client gets the error reply, not a reset connection."""
        while length > 0:
            chunk = self.rfile.read(min(length, 64 * 1024))
            if not chunk:
                break
            length -= len(chunk)

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        length = int(self.headers.get("Content-Length") or 0)
        if self.path.rstrip("/") != TRANSCRIBE_PATH:
            self._discard(length)
            return self._json(404, {"error": {"message": "not found"}})
        if not 0 < length <= MAX_UPLOAD_BYTES:
            self._discard(length)
            return self._json(413, {"error": {"message": f"upload must be 1..{MAX_UPLOAD_BYTES} bytes"}})
        body = self.rfile.read(length)
        try:
            fields = parse_form(self.headers.get("Content-Type", ""), body)
            audio = fields.get("file")
            if not audio:
                raise ValueError("no file field")
            language = fields.get("language", b"").decode().strip()
            prompt = fields.get("prompt", b"").decode().strip()
            temperature = float(fields.get("temperature", b"0").decode() or 0)
        except (ValueError, UnicodeDecodeError) as exc:
            return self._json(400, {"error": {"message": str(exc)}})
        start = time.monotonic()
        try:
            with self.server.lock:  # one job at a time on the GPU
                text = self.server.transcribe(audio, language, prompt, temperature)
        except Exception as exc:  # noqa: BLE001 - reported to the Pi, which falls back to the cloud
            logger.exception("Transcription failed")
            return self._json(500, {"error": {"message": f"transcription failed: {exc}"}})
        logger.info("%.0f KB transcribed in %.2f s", length / 1024, time.monotonic() - start)
        logger.debug("Heard %r", text)
        self._json(200, {"text": text})

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - http.server signature
        logger.debug("%s %s", self.address_string(), format % args)


class SttServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], transcribe: Transcriber, model_name: str):
        super().__init__(address, Handler)
        self.transcribe = transcribe
        self.model_name = model_name
        self.lock = threading.Lock()


def _add_cuda_dll_dirs() -> None:
    """pip's nvidia-cublas-cu12 / nvidia-cudnn-cu12 put their DLLs where Windows doesn't look."""
    if os.name != "nt":
        return
    try:
        import nvidia
    except ImportError:
        return
    for base in nvidia.__path__:
        for lib in ("cublas", "cudnn"):
            bin_dir = Path(base, lib, "bin")
            if bin_dir.is_dir():
                os.add_dll_directory(str(bin_dir))
                os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def whisper_transcriber(model_name: str, device: str, compute_type: str) -> Transcriber:
    _add_cuda_dll_dirs()
    from faster_whisper import WhisperModel

    model = WhisperModel(model_name, device=device, compute_type=compute_type)

    def transcribe(audio: bytes, language: str, prompt: str, temperature: float) -> str:
        segments, _ = model.transcribe(
            io.BytesIO(audio), language=language or None, initial_prompt=prompt or None,
            temperature=temperature, beam_size=5, vad_filter=True, condition_on_previous_text=False,
        )
        return " ".join(s.text.strip() for s in segments).strip()

    return transcribe


def silent_wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--model", default="large-v3-turbo", help="faster-whisper model (e.g. small, large-v3-turbo)")
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    parser.add_argument("--compute-type", default="int8_float16",
                        help="int8_float16 = ~1 GB of GPU memory for large-v3-turbo; use int8 with --device cpu")
    parser.add_argument("--verbose", action="store_true", help="log what was heard")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

    logger.info("Loading %s on %s (%s)", args.model, args.device, args.compute_type)
    transcribe = whisper_transcriber(args.model, args.device, args.compute_type)
    start = time.monotonic()
    transcribe(silent_wav(), "en", "", 0.0)  # load the GPU kernels now, not on the first question
    logger.info("Warm-up took %.2f s", time.monotonic() - start)

    server = SttServer((args.host, args.port), transcribe, args.model)
    logger.info("Listening on http://%s:%d%s", args.host, args.port, TRANSCRIBE_PATH)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
