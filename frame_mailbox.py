"""Hand-off of webcam frames from the laptop console to the vision thread (CAMERA_SOURCE=console).

Holds only the latest JPEG: the vision loop runs at its own pace (pose at ~5 Hz) and older frames are dropped
unseen. Frames live in memory only and are never written anywhere.
"""
import threading
from typing import Optional

MAX_FRAME_BYTES = 400_000  # a 640x480 JPEG at the page's quality is ~30-60 kB
JPEG_MAGIC = b"\xff\xd8"


class FrameMailbox:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._jpeg: Optional[bytes] = None
        self._seq = 0

    @property
    def seq(self) -> int:
        with self._cond:
            return self._seq

    def put(self, jpeg: bytes) -> bool:
        """Offer a frame; False (and ignored) if it isn't a JPEG of a sane size."""
        if not jpeg.startswith(JPEG_MAGIC) or len(jpeg) > MAX_FRAME_BYTES:
            return False
        with self._cond:
            self._jpeg = jpeg
            self._seq += 1
            self._cond.notify_all()
        return True

    def wait_newer(self, seq: int, timeout_s: float) -> tuple[int, Optional[bytes]]:
        """The latest frame if it is newer than ``seq`` (waiting up to ``timeout_s``), else (seq, None)."""
        with self._cond:
            if not self._cond.wait_for(lambda: self._seq > seq, timeout=timeout_s):
                return seq, None
            return self._seq, self._jpeg


CONSOLE_FRAMES = FrameMailbox()
