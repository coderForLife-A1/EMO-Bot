"""Bench test for ONE leg (hip + knee servo) through the ESP32, with the robot held in the air.

Talks straight to the controller over serial with raw joint moves (J commands), so it needs no IMU,
no MQTT and none of the Pi software. Close main.py first: only one program can use the serial port.

    python tools/leg_test.py --side left                 # interactive prompt (type "help")
    python tools/leg_test.py --side left demo            # scripted check: straight, stance, sweeps, steps, relax
    python tools/leg_test.py --side right demo           # the same for the other leg
    python tools/leg_test.py --side left --port socket://127.0.0.1:7777 demo   # against tools/sim_nano.py

Angles are "logical" degrees, the same as in the firmware:
    hip 0 = thigh straight down, hip > 0 = thigh swung forward     (limits -30..45)
    knee 0 = leg straight,        knee > 0 = knee bent, foot goes back (limits 0..75)
    servo degrees = 90 + trim + dir * logical angle
The left leg uses joints 0 (hip) and 2 (knee), the right leg 1 and 3 (PCA9685 channels 0-3). Once a leg
moves the right way, copy --hip-trim/--knee-trim/--hip-dir/--knee-dir into LEG_TRIM_DEG / LEG_DIR in
firmware/emo_esp32/emo_esp32.ino.

The servos are switched off (O) when the script exits, including on Ctrl+C.
"""
import argparse
import math
import sys
import time

import serial

BAUD = 115200
REPLY_TIMEOUT_S = 0.5
RATE_HZ = 50  # position updates per second during smooth moves
SPEED_DEG_S = 60  # default speed of smooth moves (the firmware's balance loop allows 250)

JOINTS = {"left": {"hip": 0, "knee": 2}, "right": {"hip": 1, "knee": 3}}
DEFAULT_DIR = {"left": 1, "right": -1}  # mirrored mounting, as LEG_DIR in the firmware
LIMITS = {"hip": (-30.0, 45.0), "knee": (0.0, 75.0)}  # LEG_MIN_DEG / LEG_MAX_DEG
STAND = {"hip": 15.0, "knee": 15.0}  # STAND_HIP_DEG / STAND_KNEE_DEG
HIP_SWING_DEG = 12.0  # gait sizes at full speed, as in the firmware
KNEE_LIFT_DEG = 12.0
GAIT_HZ = 1.0


class ControllerError(RuntimeError):
    pass


class Leg:
    def __init__(self, link: serial.Serial, side: str, trim: dict, direction: dict, verbose: bool):
        self.link = link
        self.side = side
        self.joint = JOINTS[side]
        self.trim = trim
        self.dir = direction
        self.verbose = verbose
        self.angle = {"hip": None, "knee": None}  # unknown until the first move
        self.powered = False  # a servo may be holding a position

    # ------------------------------------------------------------ serial
    def command(self, text: str) -> str:
        """Send one command and return its ACK/NACK. Events and noise are printed and skipped."""
        self.link.reset_input_buffer()
        self.link.write(f"{text}\n".encode("ascii"))
        letter = text[0]
        deadline = time.monotonic() + REPLY_TIMEOUT_S
        while time.monotonic() < deadline:
            line = self.link.readline().decode("ascii", errors="ignore").strip()
            if not line:
                continue
            if line.startswith("NACK,"):
                return line
            if line.startswith("ACK,") and (line[4:5] == letter or (letter == "J" and line[4:5].isdigit())):
                return line
            print(f"  controller: {line}")
        raise ControllerError(f"no reply to {text!r}")

    # ------------------------------------------------------------ moves
    def servo_deg(self, part: str, logical: float) -> int:
        return round(90 + self.trim[part] + self.dir[part] * logical)

    def set(self, part: str, logical: float) -> None:
        lo, hi = LIMITS[part]
        logical = max(lo, min(hi, logical))
        deg = self.servo_deg(part, logical)
        reply = self.command(f"J,{self.joint[part]},{deg}")
        self.powered = self.powered or reply.startswith("ACK")
        if reply.startswith("NACK"):
            hint = "  (E-stop latched: type 'release')" if reply.endswith("ESTOP") else ""
            raise ControllerError(f"{part} refused: {reply}{hint}")
        applied = int(reply.split(",")[3])
        if applied != deg:
            print(f"  warning: {part} servo clamped to {applied} deg (asked {deg}): check trim/dir")
        if self.verbose:
            print(f"  {part} {logical:6.1f} -> servo {deg}")
        self.angle[part] = logical

    def move(self, hip: float = None, knee: float = None, speed: float = SPEED_DEG_S) -> None:
        """Move smoothly to the target. A joint whose position is unknown jumps there directly."""
        target = {"hip": hip, "knee": knee}
        start = {}
        for part, value in target.items():
            if value is None:
                continue
            if self.angle[part] is None:
                self.set(part, value)
            start[part] = self.angle[part]
        distance = max((abs(target[p] - start[p]) for p in start), default=0)
        steps = max(1, math.ceil(distance / speed * RATE_HZ))
        for i in range(1, steps + 1):
            t0 = time.monotonic()
            for part in start:
                self.set(part, start[part] + (target[part] - start[part]) * i / steps)
            time.sleep(max(0.0, 1 / RATE_HZ - (time.monotonic() - t0)))

    def sweep(self, part: str, speed: float = 30) -> None:
        lo, hi = LIMITS[part]
        print(f"sweep {part}: {lo:.0f} -> {hi:.0f} -> {STAND[part]:.0f} deg")
        self.move(**{part: lo}, speed=speed)
        self.move(**{part: hi}, speed=speed)
        self.move(**{part: STAND[part]}, speed=speed)

    def step(self, cycles: float = 3, stride: float = 1.0) -> None:
        """The firmware's walking gait for this leg (without balance), in the air."""
        print(f"step: {cycles:g} cycle(s) at stride {stride:g}")
        self.move(**STAND)
        phase0 = 0.0 if self.side == "left" else math.pi  # the right leg is half a cycle behind
        t_start = time.monotonic()
        while True:
            t = time.monotonic() - t_start
            if t >= cycles / GAIT_HZ:
                break
            phase = 2 * math.pi * GAIT_HZ * t + phase0
            swing = HIP_SWING_DEG * stride * math.sin(phase)
            lift = KNEE_LIFT_DEG * abs(stride) * max(math.cos(phase), 0.0)
            t0 = time.monotonic()
            self.set("hip", STAND["hip"] + swing)
            self.set("knee", STAND["knee"] + lift)
            time.sleep(max(0.0, 1 / RATE_HZ - (time.monotonic() - t0)))
        self.move(**STAND)

    def relax(self) -> None:
        self.command("O")
        self.angle = {"hip": None, "knee": None}
        self.powered = False
        print("servos off")


# ---------------------------------------------------------------- scripts
def demo(leg: Leg) -> None:
    print("1/5 straight leg (hip 0, knee 0): the thigh should hang straight down, the leg straight")
    leg.move(hip=0, knee=0)
    time.sleep(2)
    print("2/5 stance (hip 15, knee 15): thigh forward, knee bent by the same amount")
    leg.move(**STAND)
    time.sleep(2)
    print("3/5 hip: positive = thigh swings FORWARD")
    leg.sweep("hip")
    print("4/5 knee: positive = knee bends, foot goes BACK")
    leg.sweep("knee")
    print("5/5 walking motion")
    leg.step(3)
    leg.relax()


HELP = """commands (angles in logical degrees):
  hip <deg> | knee <deg>   move smoothly            pose <hip> <knee>   move both
  straight                 hip 0, knee 0            stand               hip 15, knee 15
  sweep hip|knee           full range and back      step [cycles] [stride 0..1]
  speed <deg/s>            speed of smooth moves    raw <hip|knee> <servo deg>  (no limits, no trim)
  where                    show angles              off                 relax the servos
  release                  clear a latched E-stop   demo                the scripted check
  quit"""


def interactive(leg: Leg) -> None:
    speed = SPEED_DEG_S
    print(HELP)
    while True:
        try:
            words = input(f"{leg.side} leg> ").split()
        except EOFError:
            return
        if not words:
            continue
        cmd, args = words[0].lower(), words[1:]
        try:
            if cmd in ("quit", "exit", "q"):
                return
            elif cmd in ("hip", "knee") and len(args) == 1:
                leg.move(**{cmd: float(args[0])}, speed=speed)
            elif cmd == "pose" and len(args) == 2:
                leg.move(hip=float(args[0]), knee=float(args[1]), speed=speed)
            elif cmd == "straight":
                leg.move(hip=0, knee=0, speed=speed)
            elif cmd == "stand":
                leg.move(**STAND, speed=speed)
            elif cmd == "sweep" and args and args[0] in LIMITS:
                leg.sweep(args[0])
            elif cmd == "step":
                leg.step(float(args[0]) if args else 3, float(args[1]) if len(args) > 1 else 1.0)
            elif cmd == "speed" and len(args) == 1:
                speed = max(5.0, min(250.0, float(args[0])))
            elif cmd == "raw" and len(args) == 2 and args[0] in LIMITS:
                print(" ", leg.command(f"J,{leg.joint[args[0]]},{int(args[1])}"))
                leg.angle[args[0]] = None
                leg.powered = True
            elif cmd == "where":
                for part in ("hip", "knee"):
                    a = leg.angle[part]
                    state = "unknown/off" if a is None else f"{a:.1f} (servo {leg.servo_deg(part, a)})"
                    print(f"  {part}: {state}")
            elif cmd == "off":
                leg.relax()
            elif cmd == "release":
                print(" ", leg.command("R"))
            elif cmd == "demo":
                demo(leg)
            else:
                print(HELP)
        except ValueError:
            print("  numbers please")
        except ControllerError as exc:
            print(f"  {exc}")


def open_link(port: str) -> serial.Serial:
    link = serial.serial_for_url(port, baudrate=BAUD, timeout=0.05)
    # Opening a USB port resets the board (and the simulator powers on per connection): wait for it to boot.
    time.sleep(2.0 if port.startswith(("/dev/ttyUSB", "socket://")) else 0.2)
    return link


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", nargs="?", choices=("demo",), help="run the scripted check instead of the prompt")
    parser.add_argument("--side", choices=("left", "right"), required=True, help="which leg")
    parser.add_argument("--port", default="/dev/serial0",
                        help="/dev/serial0 = Pi GPIO UART (default), /dev/ttyUSB0 = USB cable")
    parser.add_argument("--hip-trim", type=float, default=0, help="servo degrees added at hip 0 (LEG_TRIM_DEG)")
    parser.add_argument("--knee-trim", type=float, default=0, help="servo degrees added at knee 0 (LEG_TRIM_DEG)")
    parser.add_argument("--hip-dir", type=int, choices=(1, -1), help="flip if the hip moves backwards (LEG_DIR)")
    parser.add_argument("--knee-dir", type=int, choices=(1, -1), help="flip if the knee bends the wrong way (LEG_DIR)")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every servo command")
    args = parser.parse_args()

    direction = {"hip": args.hip_dir or DEFAULT_DIR[args.side], "knee": args.knee_dir or DEFAULT_DIR[args.side]}
    trim = {"hip": args.hip_trim, "knee": args.knee_trim}

    try:
        link = open_link(args.port)
    except (serial.SerialException, OSError) as exc:
        print(f"Can't open {args.port}: {exc}")
        return 1

    leg = Leg(link, args.side, trim, direction, args.verbose)
    try:
        try:
            leg.command("P")
        except ControllerError:
            print(f"The controller doesn't answer on {args.port}. Check:\n"
                  "  - the repo firmware as-is (LINK_UART2 0) talks on UART0 only: the USB port (--port /dev/ttyUSB0)\n"
                  "    or, with USB unplugged, Pi pin 8 (TXD) -> ESP32 RX0/GPIO3, ESP32 TX0/GPIO1 -> Pi pin 10 (RXD)\n"
                  "  - GPIO16/17 (Serial2) only work if the firmware was flashed with '#define LINK_UART2 1':\n"
                  "    Pi pin 8 (TXD) -> ESP32 GPIO16, ESP32 GPIO17 -> Pi pin 10 (RXD)\n"
                  "  - Pi GND -> ESP32 GND\n"
                  "  - raspi-config: Serial Port, login shell OFF, hardware ON\n"
                  "    (on a Pi 5, also try --port /dev/ttyAMA0)\n"
                  "  - main.py isn't running (it holds the port)")
            return 1
        print(f"Controller answers. Testing the {args.side} leg: hip = joint {leg.joint['hip']}, "
              f"knee = joint {leg.joint['knee']}; dir hip {direction['hip']:+d}, knee {direction['knee']:+d}; "
              f"trim hip {trim['hip']:+g}, knee {trim['knee']:+g}")
        print("Hold the robot in the air with the servo supply ON. The first move jumps straight to position.")
        if args.action == "demo":
            demo(leg)
        else:
            interactive(leg)
    except KeyboardInterrupt:
        print()
    except ControllerError as exc:
        print(exc)
        return 1
    finally:
        try:
            if leg.powered:
                leg.relax()
        except (ControllerError, serial.SerialException, OSError):
            print("warning: couldn't switch the servos off; send O or power them down")
        link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
