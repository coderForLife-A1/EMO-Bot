"""What the controller last told us: link health, pitch telemetry, ToF distance and recent events.

main.py feeds every controller line into RobotStatus.on_line (on the event loop thread); the laptop console
and the distance poller read it. Distances are corrected with the ToF calibration (calibration.py) here,
so everything downstream (MQTT, console) sees calibrated millimetres.
"""
import collections
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from calibration import Calibration, corrected_mm

LINK_STALE_S = 2.0  # the Pi pings every 0.5 s when idle: no line for this long = controller not answering
EVENT_HISTORY = 20


@dataclass
class RobotStatus:
    calibration: Calibration = field(default_factory=Calibration)
    banner: str = ""  # latest READY,IMU / READY,NOIMU
    last_rx: float = float("-inf")  # monotonic time of the latest line from the controller
    pitch_deg: Optional[float] = None
    rate_dps: Optional[float] = None
    correction_deg: Optional[float] = None
    mode: str = ""  # O off, M manual, B balancing, F fallen (from telemetry)
    telemetry_at: float = float("-inf")
    distance_mm: Optional[int] = None  # calibrated; -1 = nothing in range; None = no reading yet
    distance_at: float = float("-inf")
    tof_missing: bool = False  # the controller answers NACK,D,NOTOF
    events: collections.deque = field(default_factory=lambda: collections.deque(maxlen=EVENT_HISTORY))
    calibration_changed: bool = False  # set by ACK,C; main.py saves the file and clears it

    def on_line(self, line: str, now: Optional[float] = None) -> Optional[int]:
        """Record one controller line. Returns the calibrated distance for an ACK,D reply, else None."""
        now = time.monotonic() if now is None else now
        self.last_rx = now
        if line.startswith("T,"):
            fields = line.split(",")
            if len(fields) == 5:
                try:
                    pitch, rate, corr = (int(f) / 10 for f in fields[1:4])
                except ValueError:
                    return None
                self.pitch_deg, self.rate_dps, self.correction_deg = pitch, rate, corr
                self.mode = fields[4][:1]
                self.telemetry_at = now
            return None
        if line.startswith("ACK,D,"):
            try:
                raw = int(line[6:])
            except ValueError:
                return None
            self.distance_mm = corrected_mm(raw, self.calibration)
            self.distance_at = now
            self.tof_missing = False
            return self.distance_mm
        if line == "NACK,D,NOTOF":
            repeat = self.tof_missing
            self.tof_missing = True
            self.distance_mm = None
            if repeat:  # the poller keeps asking while the sensor is missing: record the change only
                return None
        if line.startswith("ACK,C,"):  # level calibration done through the running robot (console / MQTT)
            try:
                self.calibration.imu_level_offset_deg = int(line[6:]) / 100
                self.calibration.stamp("imu_level")
                self.calibration_changed = True
            except ValueError:
                pass
            return None
        if line.startswith("READY"):
            self.banner = line
            self.mode = "O"  # the controller boots with its servos off
        if line.startswith(("EVT,", "NACK,", "READY")):
            self.events.append((datetime.now().strftime("%H:%M:%S"), line))
        return None

    def link_up(self, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        return now - self.last_rx < LINK_STALE_S

    def telemetry_fresh(self, now: Optional[float] = None, max_age_s: float = 1.0) -> bool:
        now = time.monotonic() if now is None else now
        return now - self.telemetry_at < max_age_s
