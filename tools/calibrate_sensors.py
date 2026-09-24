"""Calibrate EMO-Bot's sensors through the controller: IMU (MPU6050) and ToF (VL53L0X).

Stop main.py first (only one program can use the serial port). No step moves a servo: every IMU step
starts with O (servos off). Results go to calibration.json (CALIBRATION_FILE); main.py applies them.

    python tools/calibrate_sensors.py status              # what answers: link, IMU, ToF
    python tools/calibrate_sensors.py imu                 # noise + gyro check (robot still, anywhere)
    python tools/calibrate_sensors.py imu-sign            # then tilt the robot FORWARD ~20-30 deg and hold
    python tools/calibrate_sensors.py imu-level           # robot upright as it should stand, still: stores C
    python tools/calibrate_sensors.py tof                 # raw + calibrated distance statistics
    python tools/calibrate_sensors.py tof --target-mm 100 --save   # flat target 100 mm from the sensor face
    python tools/calibrate_sensors.py tof --target-mm 400 --save   # a second distance adds a scale fit
    python tools/calibrate_sensors.py tof --reset         # forget the ToF points
    python tools/calibrate_sensors.py show                # print calibration.json

--port defaults to SERIAL_PORT from .env (Pi 5 GPIO UART: /dev/ttyAMA0).

What each step means:
- imu: pitch noise should be well under 0.5 deg and the gyro rate noise a few deg/s at most, with the robot
  still. More means vibration, a loose sensor or a noisy supply.
- imu-sign: the firmware's balance loop assumes leaning FORWARD reads POSITIVE pitch. If it reads negative,
  set PITCH_SIGN to -1 in firmware/emo_esp32/emo_esp32.ino and reflash (or remount the board X-forward).
- imu-level: the pitch the IMU reads while the robot stands upright becomes its zero (C command, stored in
  the ESP32's flash). Refused if the robot isn't still, or reads more than 15 deg off level (use --force if
  the board really is mounted at an angle).
- tof: hold a flat, matte target (card, book) square to the sensor at a measured distance. One distance
  corrects the offset; two or more (e.g. 100 and 400 mm) also correct the scale.
"""
import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import serial  # noqa: E402

import calibration  # noqa: E402
import config  # noqa: E402
from serial_module import send_and_wait  # noqa: E402

STILL_PITCH_SD_DEG = 0.3
STILL_RATE_MEAN_DPS = 1.5
MAX_LEVEL_DEG = 15.0
TILT_DETECT_DEG = 12.0
TOF_SAME_TARGET_MM = 20.0
TOF_MIN_VALID = 0.8  # share of readings that must see the target


class CalibrationError(RuntimeError):
    pass


class Link:
    def __init__(self, port: str):
        kwargs = {"baudrate": config.SERIAL_BAUD, "timeout": 0.05, "write_timeout": 0.5}
        if "://" not in port:
            kwargs["exclusive"] = True  # fails fast if main.py (or miniterm) has the port
        try:
            self.ser = serial.serial_for_url(port, **kwargs)
        except (serial.SerialException, OSError) as exc:
            raise CalibrationError(f"can't open {port}: {exc} (is main.py or the emo-bot service running?)") from exc
        self.ser.reset_input_buffer()

    def close(self) -> None:
        try:
            self.cmd("T,0")
        except CalibrationError:
            pass
        self.ser.close()

    def cmd(self, text: str, timeout_s=None) -> str:
        reply = send_and_wait(self.ser, f"{text}\n", timeout_s)
        if not reply:
            raise CalibrationError(f"no reply to {text!r}: check the link (python tools/calibrate_sensors.py status)")
        if reply.startswith("READY"):
            raise CalibrationError(f"the controller rebooted ({reply}) while answering {text!r}: check its power")
        return reply

    def telemetry(self, seconds: float) -> list[tuple[float, float, float, str]]:
        """(time, pitch deg, rate deg/s, mode) samples at 20 Hz for ``seconds``."""
        self.cmd("T,1")
        samples = []
        end = time.monotonic() + seconds
        try:
            while time.monotonic() < end:
                line = self.ser.readline().decode("ascii", errors="ignore").strip()
                fields = line.split(",")
                if len(fields) == 5 and fields[0] == "T":
                    try:
                        samples.append((time.monotonic(), int(fields[1]) / 10, int(fields[2]) / 10, fields[4]))
                    except ValueError:
                        continue
        finally:
            self.cmd("T,0")
        if not samples:
            raise CalibrationError("no telemetry: is the IMU answering? (status)")
        return samples

    def distance(self) -> "int | None":
        reply = self.cmd("D")
        if reply == "NACK,D,NOTOF":
            return None
        if not reply.startswith("ACK,D,"):
            raise CalibrationError(f"unexpected reply to D: {reply}")
        return int(reply[6:])


def require_imu(link: Link) -> None:
    reply = link.cmd("I")
    if reply != "ACK,I":
        raise CalibrationError(f"IMU not answering ({reply}): check the MPU6050's VCC/GND/SDA(21)/SCL(22) wiring")


def pitch_stats(samples) -> dict:
    pitch = [s[1] for s in samples]
    rate = [s[2] for s in samples]
    return {
        "n": len(samples),
        "pitch_mean": statistics.fmean(pitch),
        "pitch_sd": statistics.pstdev(pitch),
        "rate_mean": statistics.fmean(rate),
        "rate_sd": statistics.pstdev(rate),
    }


def is_still(stats: dict) -> bool:
    return stats["pitch_sd"] < STILL_PITCH_SD_DEG and abs(stats["rate_mean"]) < STILL_RATE_MEAN_DPS


def print_stats(stats: dict) -> None:
    print(f"  pitch {stats['pitch_mean']:+.2f} deg (noise {stats['pitch_sd']:.2f} deg), "
          f"rate {stats['rate_mean']:+.2f} deg/s (noise {stats['rate_sd']:.2f} deg/s), {stats['n']} samples")


# ------------------------------------------------------------------ steps
def cmd_status(link: Link, args) -> int:
    print("link:", link.cmd("P"))
    print("IMU: ", link.cmd("I"))
    distance = link.distance()
    print("ToF: ", "not found (NACK,D,NOTOF)" if distance is None else f"{distance} mm raw")
    return 0


def cmd_imu(link: Link, args) -> int:
    link.cmd("O")
    require_imu(link)
    print(f"Keep the robot still for {args.seconds:.0f} s...")
    stats = pitch_stats(link.telemetry(args.seconds))
    print_stats(stats)
    ok = stats["pitch_sd"] < 0.5 and stats["rate_sd"] < 3 and abs(stats["rate_mean"]) < STILL_RATE_MEAN_DPS
    if not ok:
        print("  NOISY (not saved): check that the robot was still, the sensor is fixed down and the supply is clean")
        return 1
    cal = calibration.load()
    cal.imu_pitch_noise_deg = round(stats["pitch_sd"], 3)
    cal.imu_rate_noise_dps = round(stats["rate_sd"], 3)
    cal.stamp("imu_noise")
    calibration.save(cal)
    print("  OK: quiet sensor, gyro bias removed (saved)")
    return 0


def wait_level_and_still(link: Link, timeout_s: float) -> float:
    """Wait until pitch holds within MAX_LEVEL_DEG of level, still, for 1 s; returns that pitch."""
    window: list[float] = []
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        fields = link.ser.readline().decode("ascii", errors="ignore").strip().split(",")
        if len(fields) != 5 or fields[0] != "T":
            continue
        window = (window + [int(fields[1]) / 10])[-20:]  # 1 s at 20 Hz
        if len(window) == 20 and statistics.pstdev(window) < 0.5 and abs(statistics.fmean(window)) < MAX_LEVEL_DEG:
            return statistics.fmean(window)
    shown = f"{statistics.fmean(window):+.1f} deg" if window else "nothing"
    raise CalibrationError(f"never held level and still for 1 s (last reading {shown}): start upright/flat, "
                           f"within {MAX_LEVEL_DEG:.0f} deg of level")


def cmd_imu_sign(link: Link, args) -> int:
    link.cmd("O")
    require_imu(link)
    print("Hold the robot upright (or the loose board flat, X arrow away from you) and still...")
    link.cmd("T,1")
    try:
        base = wait_level_and_still(link, args.seconds)
    finally:
        link.cmd("T,0")
    print(f"  baseline pitch {base:+.1f} deg")
    print(f"Now tilt the robot FORWARD (face down) by about 20-30 deg and hold it there "
          f"(waiting up to {args.seconds:.0f} s)...")
    link.cmd("T,1")
    held_since = None
    delta = 0.0
    largest = 0.0
    end = time.monotonic() + args.seconds
    try:
        while time.monotonic() < end:
            fields = link.ser.readline().decode("ascii", errors="ignore").strip().split(",")
            if len(fields) != 5 or fields[0] != "T":
                continue
            delta = int(fields[1]) / 10 - base
            largest = max(largest, delta, key=abs)
            if abs(delta) >= TILT_DETECT_DEG:
                held_since = held_since or time.monotonic()
                if time.monotonic() - held_since >= 0.5:
                    break
            else:
                held_since = None
        else:
            raise CalibrationError(
                f"no pitch change of {TILT_DETECT_DEG:.0f} deg held for 0.5 s (largest {largest:+.1f} deg). "
                "Tilt so the X arrow's tip dips down (a rotation around the board's Y axis); rocking it sideways "
                "around the X arrow is roll, which pitch doesn't see")
    finally:
        link.cmd("T,0")
    ok = delta > 0
    print(f"  pitch changed by {delta:+.1f} deg")
    cal = calibration.load()
    cal.imu_pitch_sign_ok = ok
    cal.stamp("imu_sign")
    calibration.save(cal)
    if ok:
        print("  OK: leaning forward reads positive pitch, as the balance loop expects")
        return 0
    print("  REVERSED: leaning forward reads NEGATIVE pitch. Set PITCH_SIGN to -1 in "
          "firmware/emo_esp32/emo_esp32.ino and reflash, or remount the MPU6050 with its X arrow forward.\n"
          "  If you tilted it backward by mistake, run this step again.")
    return 1


def cmd_imu_level(link: Link, args) -> int:
    link.cmd("O")
    require_imu(link)
    print(f"Hold the robot upright, as it should stand, and keep it still for {args.seconds:.0f} s...")
    stats = pitch_stats(link.telemetry(args.seconds))
    print_stats(stats)
    if not is_still(stats):
        raise CalibrationError("the robot moved: hold it still (or rest it upright against something) and retry")
    cal = calibration.load()
    raw = stats["pitch_mean"] + (cal.imu_level_offset_deg or 0.0)  # undo the offset stored last time
    if abs(raw) > MAX_LEVEL_DEG and not args.force:
        raise CalibrationError(
            f"the IMU reads {raw:+.1f} deg from level. That looks like a robot that isn't upright or an IMU that "
            f"isn't mounted flat on the pelvis. Fix that first, or use --force if the mount really is angled.")
    reply = link.cmd("C")
    if not reply.startswith("ACK,C,"):
        raise CalibrationError(f"calibration refused: {reply}")
    offset = int(reply[6:]) / 100
    after = pitch_stats(link.telemetry(1.0))
    cal.imu_level_offset_deg = offset
    cal.stamp("imu_level")
    calibration.save(cal)
    print(f"  stored level offset {offset:+.2f} deg in the controller's flash; pitch now reads "
          f"{after['pitch_mean']:+.2f} deg")
    return 0


def cmd_tof(link: Link, args) -> int:
    cal = calibration.load()
    if args.reset:
        cal.tof_points, cal.tof_scale, cal.tof_offset_mm = [], 1.0, 0.0
        cal.stamp("tof")
        calibration.save(cal)
        print("ToF calibration cleared")
        return 0
    first = link.distance()
    if first is None:
        raise CalibrationError("ToF not answering (NACK,D,NOTOF): check the VL53L0X's VIN(3V3)/GND/SDA(21)/SCL(22) "
                               "wiring, and that the firmware was built with TOF_ENABLED 1")
    readings = []
    for _ in range(args.samples):
        value = link.distance()
        if value is not None:
            readings.append(value)
        time.sleep(0.055)  # the sensor ranges at 20 Hz
    valid = [r for r in readings if r >= 0]
    print(f"  {len(valid)}/{args.samples} readings saw a target")
    if len(valid) < max(3, TOF_MIN_VALID * args.samples):
        raise CalibrationError("too few readings in range: move the target closer (VL53L0X: ~30-1200 mm) "
                               "and make sure nothing else is in the beam")
    mean = statistics.fmean(valid)
    sd = statistics.pstdev(valid)
    print(f"  raw {mean:.1f} mm (noise {sd:.1f} mm, range {min(valid)}-{max(valid)})")
    print(f"  calibrated now: {calibration.corrected_mm(round(mean), cal)} mm")
    if args.target_mm is None:
        return 0
    error = mean - args.target_mm
    print(f"  target {args.target_mm:.0f} mm: sensor reads {error:+.1f} mm off")
    if not args.save:
        print("  (add --save to store this point)")
        return 0
    points = [p for p in cal.tof_points if abs(p[0] - args.target_mm) > TOF_SAME_TARGET_MM]
    points.append([args.target_mm, round(mean, 1), round(sd, 1)])
    scale, offset = calibration.fit_tof(points)
    cal.tof_points = sorted(points)
    cal.tof_scale, cal.tof_offset_mm = round(scale, 4), round(offset, 1)
    cal.stamp("tof")
    path = calibration.save(cal)
    print(f"  saved {len(points)} point(s) to {path}: distance = {scale:.4f} x raw {offset:+.1f} mm")
    return 0


def cmd_show(args) -> int:
    path = calibration.calibration_path()
    if not path.exists():
        print(f"{path}: not calibrated yet")
        return 0
    print(path.read_text(), end="")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--port", default=None, help="serial port (default: SERIAL_PORT from .env)")
    sub = parser.add_subparsers(dest="step", required=True)
    sub.add_parser("status")
    for name, seconds in (("imu", 5.0), ("imu-level", 3.0), ("imu-sign", 20.0)):
        p = sub.add_parser(name)
        p.add_argument("--seconds", type=float, default=seconds)
        if name == "imu-level":
            p.add_argument("--force", action="store_true", help="accept an IMU more than 15 deg from level")
    p = sub.add_parser("tof")
    p.add_argument("--target-mm", type=float, help="measured distance from the sensor face to the target")
    p.add_argument("--samples", type=int, default=40)
    p.add_argument("--save", action="store_true", help="store this point and refit")
    p.add_argument("--reset", action="store_true", help="forget all ToF points")
    sub.add_parser("show")
    args = parser.parse_args(argv)

    if args.step == "show":
        return cmd_show(args)
    steps = {"status": cmd_status, "imu": cmd_imu, "imu-sign": cmd_imu_sign, "imu-level": cmd_imu_level,
             "tof": cmd_tof}
    try:
        link = Link(args.port or config.SERIAL_PORT)
        try:
            return steps[args.step](link, args)
        finally:
            link.close()
    except (CalibrationError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
