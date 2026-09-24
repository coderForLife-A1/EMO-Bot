"""Per-robot sensor calibration, stored in calibration.json next to this file (CALIBRATION_FILE in .env).

Written by tools/calibrate_sensors.py and read by main.py at start-up:

- ToF (VL53L0X): measured at known distances; a straight-line fit (true = scale * raw + offset) is applied
  to every distance before it is published. One point gives an offset only, two or more a full fit.
- IMU (MPU6050): the level offset itself lives in the ESP32's flash (command C); the file keeps a copy,
  the pitch-sign check and the noise figures so you can see when and how the robot was calibrated.

A missing or unreadable file means "not calibrated": raw values pass through unchanged.
"""
import contextlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger(__name__)

TOF_MIN_POINT_SPREAD_MM = 50.0  # two points closer than this can't give a trustworthy scale
TOF_SCALE_LIMITS = (0.8, 1.25)  # a VL53L0X that needs more than this is misread or mis-mounted


@dataclass
class Calibration:
    tof_offset_mm: float = 0.0
    tof_scale: float = 1.0
    tof_points: list = field(default_factory=list)  # [[true_mm, measured_mm, noise_mm], ...]
    imu_level_offset_deg: Optional[float] = None  # copy of the ESP32's stored offset (ACK,C,<x100>)
    imu_pitch_sign_ok: Optional[bool] = None  # True: leaning forward reads positive pitch
    imu_pitch_noise_deg: Optional[float] = None
    imu_rate_noise_dps: Optional[float] = None
    updated: dict = field(default_factory=dict)  # "tof" / "imu_level" / "imu_sign" / "imu_noise" -> ISO time

    def stamp(self, what: str) -> None:
        self.updated[what] = datetime.now(timezone.utc).isoformat(timespec="seconds")


def calibration_path() -> Path:
    return Path(config.CALIBRATION_FILE)


def load(path: Optional[Path] = None) -> Calibration:
    path = Path(path) if path is not None else calibration_path()
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return Calibration()
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable calibration file %s: %s", path, exc)
        return Calibration()
    if not isinstance(raw, dict):
        logger.warning("Ignoring calibration file %s: not a JSON object", path)
        return Calibration()
    known = Calibration.__dataclass_fields__
    cal = Calibration(**{k: v for k, v in raw.items() if k in known})
    if not _tof_fit_sane(cal.tof_scale, cal.tof_offset_mm):
        logger.warning("Ignoring implausible ToF calibration in %s (scale %s, offset %s)",
                       path, cal.tof_scale, cal.tof_offset_mm)
        cal.tof_scale, cal.tof_offset_mm = 1.0, 0.0
    return cal


def save(cal: Calibration, path: Optional[Path] = None) -> Path:
    """Write atomically (temp file + rename), so a crash never leaves half a file behind."""
    path = Path(path) if path is not None else calibration_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".calibration-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(cal), f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def _tof_fit_sane(scale, offset) -> bool:
    try:
        scale, offset = float(scale), float(offset)
    except (TypeError, ValueError):
        return False
    return TOF_SCALE_LIMITS[0] <= scale <= TOF_SCALE_LIMITS[1] and abs(offset) <= 200


def fit_tof(points: list) -> tuple[float, float]:
    """(scale, offset) so that true_mm ~= scale * measured_mm + offset.

    One point (or points too close together for a slope): offset only. Raises ValueError on a fit a
    working sensor would never need.
    """
    if not points:
        return 1.0, 0.0
    true = [float(p[0]) for p in points]
    meas = [float(p[1]) for p in points]
    n = len(points)
    if n == 1 or max(meas) - min(meas) < TOF_MIN_POINT_SPREAD_MM:
        scale, offset = 1.0, sum(t - m for t, m in zip(true, meas, strict=True)) / n
    else:
        mean_t, mean_m = sum(true) / n, sum(meas) / n
        sxx = sum((m - mean_m) ** 2 for m in meas)
        sxy = sum((m - mean_m) * (t - mean_t) for m, t in zip(meas, true, strict=True))
        scale = sxy / sxx
        offset = mean_t - scale * mean_m
    if not _tof_fit_sane(scale, offset):
        raise ValueError(f"fit scale {scale:.3f}, offset {offset:+.0f} mm is implausible: check the target "
                         "distances and that the sensor sees the target, not the desk")
    return scale, offset


def corrected_mm(raw_mm: int, cal: Calibration) -> int:
    """Apply the ToF fit. -1 (nothing in range) passes through; results never go below 0."""
    if raw_mm < 0:
        return raw_mm
    return max(0, round(cal.tof_scale * raw_mm + cal.tof_offset_mm))
