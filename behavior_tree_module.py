"""10 Hz behavior tree for the two-legged EMO-Bot.

The controller (ESP32, or the Arduino Nano alternative) runs the fast loop (IMU balance PID + gait
at 100 Hz). This tree only decides *what* the legs should do and sends high-level commands:
S (stand), W,<speed>,<turn> (walk), G,1 (gesture), O (relax), E/R (E-stop). See
firmware/emo_esp32/emo_esp32.ino (same protocol as firmware/emo_nano/emo_nano.ino).

Threading: SharedState is only touched on one thread (the tick thread). MQTT callbacks don't
modify it directly; they hand each message to a ``deliver`` function that queues it for that
thread (main.py uses loop.call_soon_threadsafe, standalone mode uses an inbox queue).
"""
import collections
import logging
import math
import queue
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import paho.mqtt.client as mqtt
import py_trees

import config
from mqtt_client import MAX_PAYLOAD_BYTES, make_client, stop_client

logger = logging.getLogger(__name__)

WALK_HEARTBEAT_S = 0.2  # firmware stops walking if W isn't repeated within 1 s
WALK_DEFAULT_S = 2.0
WALK_MAX_S = 10.0  # a walk command never runs longer than this (desk robot: don't wander off)
POSTURE_GESTURE_S = 2.0  # how long the posture branch owns the robot after an alert
STAND_RETRY_S = 1.0  # resend S if no ACK,S arrived within this time
CONVERSATION_TIMEOUT_S = 30.0  # wake flag older than this is ignored (recording + API + playback)
OUTBOX_MAX = 16  # queued tuning commands (gesture/telemetry/gains/calibrate); more are dropped
FLASH_WRITE_MIN_S = 1.0  # gains and calibrate rewrite the controller's flash: at most one per second

AUDIO_EMERGENCY_STOP = "AUDIO,EMERGENCY_STOP"
AUDIO_POSTURE_WARNING = "AUDIO,POSTURE_WARNING"
AUDIO_FALLEN = "AUDIO,FALLEN"
AUDIO_IMU_FAULT = "AUDIO,IMU_FAULT"

GESTURE_BOB = "G,1"

_ESTOP_ON = {"error", "estop", "stop", "1", "true", "on"}
_ESTOP_OFF = {"clear", "ok", "reset", "0", "false", "off"}


@dataclass
class SharedState:
    estop_active: bool = False
    conversation_active: bool = False
    conversation_since: float = 0.0  # monotonic time the wake flag was raised
    posture_poor: bool = False
    posture_alert_ts: float = 0.0  # monotonic time of the latest POSTURE_POOR alert
    posture_alert_seq: int = 0  # increments per alert (timestamps can collide on coarse clocks)
    face_error: Optional[tuple[float, float]] = None
    face_error_seq: int = 0
    last_face_error_ts: float = 0.0
    # Legs
    fallen: bool = False  # EVT,FALLEN / NACK,S,TILTED; cleared by a "stand" command
    imu_fault: bool = False  # EVT,IMU_FAIL / NACK,*,NOIMU / READY,NOIMU; cleared by ACK,S or READY,IMU
    imu_retry: bool = False  # "stand" while imu_fault: send one I (re-init the IMU without moving a servo)
    resting: bool = False  # "rest" command: servos off until "stand"
    legs_standing: bool = False  # confirmed by ACK,S; cleared by NACKs, O, E, falls, resets
    stand_sent_at: Optional[float] = None  # when the last unanswered S was sent
    walk_speed: int = 0  # -100..100
    walk_turn: int = 0  # -100..100
    walk_until: float = 0.0  # monotonic deadline of the current walk command
    walking: bool = False  # we sent a non-zero W that hasn't been stopped yet
    outbox: collections.deque = field(default_factory=collections.deque)  # raw tuning commands (max OUTBOX_MAX)
    last_flash_write: dict = field(default_factory=dict)  # "gains"/"calibrate" -> time last queued (flash writes)
    estop_resend: bool = False  # controller rebooted during an E-stop: latch it again
    last_outbox_warning: float = float("-inf")


@dataclass
class CommandBus:
    motor_queue: "queue.Queue[str]" = field(default_factory=lambda: queue.Queue(maxsize=200))
    audio_queue: "queue.Queue[str]" = field(default_factory=lambda: queue.Queue(maxsize=50))

    def put_motor(self, command: str) -> bool:
        try:
            self.motor_queue.put_nowait(command)
        except queue.Full:
            return False
        return True

    def put_urgent(self, command: str) -> bool:
        """Empty the motor queue and queue ``command`` (E-stop): it can't be crowded out by a full queue."""
        while True:
            try:
                self.motor_queue.get_nowait()
            except queue.Empty:
                break
        return self.put_motor(command)

    def put_audio(self, command: str) -> None:
        try:
            self.audio_queue.put_nowait(command)
        except queue.Full:
            pass


def request_stand(state: SharedState, bus: CommandBus) -> None:
    """Send S unless one is already waiting for its ACK (retried after STAND_RETRY_S)."""
    now = time.monotonic()
    if state.stand_sent_at is not None and now - state.stand_sent_at < STAND_RETRY_S:
        return
    if bus.put_motor("S"):
        state.stand_sent_at = now


def ensure_standing(state: SharedState, bus: CommandBus) -> None:
    """Stop any walk and make sure the controller is balancing."""
    if state.walking:
        bus.put_motor("W,0,0")
        state.walking = False
    if not state.legs_standing:
        request_stand(state, bus)


def _legs_off(state: SharedState) -> None:
    state.legs_standing = state.walking = False
    state.stand_sent_at = None


class ServiceCommands(py_trees.behaviour.Behaviour):
    """Forwards tuning commands (telemetry, gains, calibration) from MQTT. Always FAILURE."""

    def __init__(self, state: SharedState, bus: CommandBus):
        super().__init__(name="Service Commands")
        self.state = state
        self.bus = bus

    def update(self) -> py_trees.common.Status:
        while self.state.outbox:
            self.bus.put_motor(self.state.outbox.popleft())
        return py_trees.common.Status.FAILURE


class EStopGuard(py_trees.behaviour.Behaviour):
    """Latches the controller's E-stop ('E' = all servos off) once, and releases it ('R') when cleared."""

    def __init__(self, state: SharedState, bus: CommandBus):
        super().__init__(name="E-Stop")
        self.state = state
        self.bus = bus
        self.latched = False

    def update(self) -> py_trees.common.Status:
        if self.state.estop_active:
            # Latch only once E is really queued (put_urgent clears the queue first); else retry next tick.
            # main.py also sends E ahead of anything already waiting on the serial link.
            if not self.latched and self.bus.put_urgent("E"):
                self.bus.put_audio(AUDIO_EMERGENCY_STOP)
                _legs_off(self.state)
                self.state.walk_until = 0.0  # an E-stop cancels the walk; "clear" must not resume it
                self.state.outbox.clear()  # ...and any queued tuning commands: nothing stale after "clear"
                self.state.estop_resend = False
                self.latched = True
            elif self.latched and self.state.estop_resend and self.bus.put_urgent("E"):
                self.state.estop_resend = False  # the controller rebooted mid-E-stop: latch it again
            return py_trees.common.Status.SUCCESS

        if self.latched:
            self.bus.put_motor("R")  # servos stay off; a lower branch sends S to stand again
            self.state.outbox.clear()  # (commands are refused during an E-stop; belt and braces)
            self.latched = False
        return py_trees.common.Status.FAILURE


class ImuFaultGuard(py_trees.behaviour.Behaviour):
    """No balance or fall detection without the IMU: relax the servos and don't stand or walk.

    A "stand" command sends one I: the controller re-initialises the IMU without moving a servo and answers
    ACK,I (working: the fault clears and the normal branches stand the robot up) or NACK,I,NOIMU.
    """

    def __init__(self, state: SharedState, bus: CommandBus):
        super().__init__(name="IMU Fault")
        self.state = state
        self.bus = bus
        self.handled = False

    def update(self) -> py_trees.common.Status:
        if not self.state.imu_fault:
            self.handled = False
            return py_trees.common.Status.FAILURE
        if not self.handled:
            self.bus.put_audio(AUDIO_IMU_FAULT)
        if not self.handled or self.state.legs_standing:
            # Also when the controller stood up anyway (ACK,S,NOIMU after a retry): relax it again.
            self.bus.put_motor("O")
            _legs_off(self.state)
            self.handled = True
        if self.state.imu_retry:
            self.state.imu_retry = False
            self.bus.put_motor("I")  # never S: that would stand the robot blind if the IMU is still missing
        return py_trees.common.Status.SUCCESS


class FallenGuard(py_trees.behaviour.Behaviour):
    """After a fall the controller has switched the servos off. Ask for help once and wait for "stand"."""

    def __init__(self, state: SharedState, bus: CommandBus):
        super().__init__(name="Fallen")
        self.state = state
        self.bus = bus
        self.announced = False

    def update(self) -> py_trees.common.Status:
        if not self.state.fallen:
            self.announced = False
            return py_trees.common.Status.FAILURE
        if not self.announced:
            self.bus.put_audio(AUDIO_FALLEN)
            self.announced = True
        _legs_off(self.state)
        return py_trees.common.Status.SUCCESS


class RestGuard(py_trees.behaviour.Behaviour):
    def __init__(self, state: SharedState, bus: CommandBus):
        super().__init__(name="Rest")
        self.state = state
        self.bus = bus
        self.sent = False

    def update(self) -> py_trees.common.Status:
        if not self.state.resting:
            self.sent = False
            return py_trees.common.Status.FAILURE
        if not self.sent:
            self.bus.put_motor("O")
            _legs_off(self.state)
            self.sent = True
        return py_trees.common.Status.SUCCESS


class CheckConversationActive(py_trees.behaviour.Behaviour):
    """Active while the wake flag is up, but never longer than CONVERSATION_TIMEOUT_S."""

    def __init__(self, state: SharedState):
        super().__init__(name="Check Conversation Active")
        self.state = state

    def update(self) -> py_trees.common.Status:
        if not self.state.conversation_active:
            return py_trees.common.Status.FAILURE
        if time.monotonic() - self.state.conversation_since > CONVERSATION_TIMEOUT_S:
            logger.warning("Conversation flag stuck for %.0fs; clearing it", CONVERSATION_TIMEOUT_S)
            self.state.conversation_active = False
            return py_trees.common.Status.FAILURE
        return py_trees.common.Status.SUCCESS


class ConversationAction(py_trees.behaviour.Behaviour):
    """Stand still and balanced while talking."""

    def __init__(self, state: SharedState, bus: CommandBus):
        super().__init__(name="Conversation Action")
        self.state = state
        self.bus = bus

    def update(self) -> py_trees.common.Status:
        ensure_standing(self.state, self.bus)
        return py_trees.common.Status.SUCCESS


class CheckPosturePoor(py_trees.behaviour.Behaviour):
    """Succeeds only during the gesture window after an alert, so other behaviours aren't starved."""

    def __init__(self, state: SharedState):
        super().__init__(name="Check Posture Poor")
        self.state = state

    def update(self) -> py_trees.common.Status:
        in_window = (time.monotonic() - self.state.posture_alert_ts) < POSTURE_GESTURE_S
        if self.state.posture_poor and in_window:
            return py_trees.common.Status.SUCCESS
        return py_trees.common.Status.FAILURE


class PostureCorrectionAction(py_trees.behaviour.Behaviour):
    """One knee bob and one spoken reminder per alert."""

    def __init__(self, state: SharedState, bus: CommandBus):
        super().__init__(name="Posture Correction Action")
        self.state = state
        self.bus = bus
        self.handled_alert_seq = 0

    def update(self) -> py_trees.common.Status:
        ensure_standing(self.state, self.bus)
        if self.state.posture_alert_seq != self.handled_alert_seq:
            self.handled_alert_seq = self.state.posture_alert_seq
            self.bus.put_audio(AUDIO_POSTURE_WARNING)
            if self.state.legs_standing:
                self.bus.put_motor(GESTURE_BOB)
        return py_trees.common.Status.SUCCESS


class WalkAction(py_trees.behaviour.Behaviour):
    """Walk while a walk command is active, re-sending W as a heartbeat; stop when it expires."""

    def __init__(self, state: SharedState, bus: CommandBus):
        super().__init__(name="Walk")
        self.state = state
        self.bus = bus
        self.last_sent = 0.0

    def update(self) -> py_trees.common.Status:
        now = time.monotonic()
        active = now < self.state.walk_until and (self.state.walk_speed or self.state.walk_turn)
        if not active:
            return py_trees.common.Status.FAILURE

        if not self.state.legs_standing:
            request_stand(self.state, self.bus)  # walk once ACK,S confirms the robot is balancing
            return py_trees.common.Status.SUCCESS
        if not self.state.walking or now - self.last_sent >= WALK_HEARTBEAT_S:
            self.bus.put_motor(f"W,{self.state.walk_speed},{self.state.walk_turn}")
            self.state.walking = True
            self.last_sent = now
        return py_trees.common.Status.SUCCESS


class IdleStand(py_trees.behaviour.Behaviour):
    """Default: stand balanced (stopping any finished walk)."""

    def __init__(self, state: SharedState, bus: CommandBus):
        super().__init__(name="Idle Stand")
        self.state = state
        self.bus = bus

    def update(self) -> py_trees.common.Status:
        ensure_standing(self.state, self.bus)
        return py_trees.common.Status.SUCCESS


def parse_bool(payload: str) -> bool:
    return payload.strip().lower() in {"1", "true", "on", "yes"}


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"not a finite number: {text!r}")
    return value


def _clamp_int(text: str, low: int, high: int) -> int:
    return max(low, min(high, int(_finite(text))))


def _queue_raw(state: SharedState, *commands: str) -> bool:
    """Queue tuning commands for the controller; a flood beyond OUTBOX_MAX is dropped, not buffered.

    Returns False if they were dropped.
    """
    if len(state.outbox) + len(commands) > OUTBOX_MAX:
        now = time.monotonic()
        if now - state.last_outbox_warning > 1.0:  # a flood logs once a second, not once per message
            state.last_outbox_warning = now
            logger.warning("Too many queued tuning commands; dropping %s (and any more this second)",
                           ", ".join(commands))
        return False
    state.outbox.extend(commands)
    return True


def _flash_write_allowed(state: SharedState, cmd: str) -> bool:
    """gains and calibrate each rewrite the controller's settings in flash: one of each per FLASH_WRITE_MIN_S.

    Only checks; the time is recorded by _queue_flash_write once the command is really queued.
    """
    if time.monotonic() - state.last_flash_write.get(cmd, float("-inf")) < FLASH_WRITE_MIN_S:
        logger.warning("Ignoring %r: at most one %s per %.0f s (each one writes flash)", cmd, cmd, FLASH_WRITE_MIN_S)
        return False
    return True


def _queue_flash_write(state: SharedState, cmd: str, *commands: str) -> bool:
    if _queue_raw(state, *commands):
        state.last_flash_write[cmd] = time.monotonic()
        return True
    return False


# Commands refused while the E-stop is latched: nothing asked for during an emergency runs after "clear".
_REFUSED_DURING_ESTOP = {"walk", "gesture", "telemetry", "gains", "calibrate"}


def apply_locomotion_command(state: SharedState, payload: str) -> None:
    """robot/locomotion/cmd payloads:

    stand | rest | stop | walk,<speed>,<turn>[,<seconds>] | gesture | telemetry,<0|1> |
    gains,<kp>,<ki>,<kd> (floats) | calibrate | distance (read the ToF once)
    """
    parts = [p.strip() for p in payload.strip().lower().split(",")]
    cmd, args = parts[0], parts[1:]
    if state.estop_active and cmd in _REFUSED_DURING_ESTOP:
        logger.warning("Ignoring %r during the E-stop (send it again after 'clear')", payload)
        return
    try:
        if cmd == "stand":
            state.fallen = state.resting = False
            state.walk_until = 0.0
            state.stand_sent_at = None
            if state.imu_fault:
                state.imu_retry = True
        elif cmd == "rest":
            state.resting = True
            state.walk_until = 0.0
        elif cmd == "stop":
            state.walk_until = 0.0
        elif cmd == "walk":
            speed = _clamp_int(args[0], -100, 100)
            turn = _clamp_int(args[1], -100, 100) if len(args) > 1 else 0
            seconds = _finite(args[2]) if len(args) > 2 else WALK_DEFAULT_S
            state.walk_speed, state.walk_turn = speed, turn
            state.walk_until = time.monotonic() + max(0.0, min(WALK_MAX_S, seconds))
            state.resting = False
        elif cmd == "gesture":
            _queue_raw(state, GESTURE_BOB)
        elif cmd == "telemetry":
            _queue_raw(state, f"T,{1 if parse_bool(args[0]) else 0}")
        elif cmd == "distance":
            _queue_raw(state, "D")  # the reply is published on robot/sensor/distance (main.py)
        elif cmd == "gains":
            kp, ki, kd = (round(_finite(a) * 100) for a in args[:3])
            if min(kp, ki, kd) < 0 or max(kp, ki, kd) > 100_000:
                raise ValueError("gains must be between 0 and 1000")
            if _flash_write_allowed(state, cmd):
                _queue_flash_write(state, cmd, f"K,{kp},{ki},{kd}")
        elif cmd == "calibrate":
            # Calibration needs the balance loop off: relax, measure, then stay relaxed until "stand".
            if _flash_write_allowed(state, cmd) and _queue_flash_write(state, cmd, "O", "C"):
                state.resting = True
                state.walk_until = 0.0
        else:
            logger.warning("Unknown locomotion command %r", payload)
    except (IndexError, ValueError, OverflowError):
        logger.warning("Malformed locomotion command %r", payload)


def apply_serial_line(state: SharedState, line: str) -> None:
    """Update the leg state from any line the controller sends (replies, events, banners)."""
    if line.startswith("READY"):
        on_nano_reset(state)
        state.imu_fault = line == "READY,NOIMU" and not config.ALLOW_NO_IMU
        return
    if line in ("ACK,S", "ACK,S,NOIMU"):
        state.legs_standing = True
        state.stand_sent_at = None
        # ACK,S: the IMU answered (the firmware retries it on every S). ACK,S,NOIMU: standing without
        # balance, which is only acceptable on the bench (ImuFaultGuard relaxes it again otherwise).
        state.imu_fault = line == "ACK,S,NOIMU" and not config.ALLOW_NO_IMU
        return
    if line == "ACK,I":
        state.imu_fault = False  # IMU answered; the normal branches now stand the robot up
        state.stand_sent_at = None
        return
    if line in ("ACK,O", "ACK,E"):
        _legs_off(state)
        return
    if line == "EVT,FALLEN" or line == "NACK,S,TILTED":
        state.fallen = True
        _legs_off(state)
        return
    if line == "EVT,IMU_FAIL" or line in ("NACK,S,NOIMU", "NACK,W,NOIMU", "NACK,I,NOIMU"):
        # (NACK,C,NOIMU only means calibration needs the IMU: not a fault on a bench without one.)
        state.imu_fault = True
        _legs_off(state)
        return
    if line == "EVT,WATCHDOG":
        state.walking = False
        return
    if line.startswith("NACK,S,") or line.startswith("NACK,W,"):
        # Not balancing after all (e.g. NACK,W,MODE): stand again after the retry delay.
        state.legs_standing = state.walking = False
        state.stand_sent_at = time.monotonic()


def on_nano_reset(state: SharedState) -> None:
    """The controller (re)booted with its servos off: stand again on the next tick.

    If it rebooted during an E-stop, its latch is gone: EStopGuard sends E again.
    """
    _legs_off(state)
    state.estop_resend = state.estop_active


def clear_conversation(state: SharedState) -> None:
    """Called when the audio/API task dies so the conversation branch can't stay stuck."""
    state.conversation_active = False


def apply_topic_payload(state: SharedState, topic: str, payload: str) -> None:
    if topic == config.TOPIC_ERROR:
        value = payload.strip().lower()
        if value in _ESTOP_ON:
            state.estop_active = True
        elif value in _ESTOP_OFF:
            state.estop_active = False
        else:
            logger.warning("Ignoring unknown %s payload %r (use 'error' or 'clear')", topic, payload)
        return

    if topic == config.TOPIC_LOCOMOTION_CMD:
        apply_locomotion_command(state, payload)
        return

    if topic == config.TOPIC_WAKE_FLAG:
        state.conversation_active = parse_bool(payload)
        if state.conversation_active:
            state.conversation_since = time.monotonic()
        return

    if topic == config.TOPIC_STATE:
        if payload == "POSTURE_POOR":
            state.posture_poor = True
            state.posture_alert_ts = time.monotonic()
            state.posture_alert_seq += 1
        elif payload == "POSTURE_OK":
            state.posture_poor = False
        return

    if topic == config.TOPIC_FACE_ERROR:
        try:
            x_str, y_str = payload.split(",", 1)
            state.face_error = (_finite(x_str), _finite(y_str))
        except ValueError:
            return
        state.face_error_seq += 1
        state.last_face_error_ts = time.monotonic()


def safe_apply(state: SharedState, topic: str, payload: str) -> None:
    """apply_topic_payload that never raises (a bad message must not stop E-stop handling)."""
    try:
        apply_topic_payload(state, topic, payload)
    except Exception:  # noqa: BLE001
        logger.exception("Error handling %s payload %r", topic, payload)


def build_tree(state: SharedState, bus: CommandBus) -> py_trees.trees.BehaviourTree:
    root = py_trees.composites.Selector(name="Priority Selector", memory=False)

    conversation_seq = py_trees.composites.Sequence(name="Conversation Active", memory=False)
    conversation_seq.add_children([CheckConversationActive(state), ConversationAction(state, bus)])

    posture_seq = py_trees.composites.Sequence(name="Posture Correction", memory=False)
    posture_seq.add_children([CheckPosturePoor(state), PostureCorrectionAction(state, bus)])

    root.add_children([
        EStopGuard(state, bus),  # first: nothing may run (or fill the queue) ahead of the E-stop
        ServiceCommands(state, bus),
        ImuFaultGuard(state, bus),
        FallenGuard(state, bus),
        RestGuard(state, bus),
        conversation_seq,
        posture_seq,
        WalkAction(state, bus),
        IdleStand(state, bus),
    ])
    return py_trees.trees.BehaviourTree(root=root)


SUBSCRIBED_TOPICS = (
    config.TOPIC_ERROR,
    config.TOPIC_WAKE_FLAG,
    config.TOPIC_STATE,
    config.TOPIC_FACE_ERROR,
    config.TOPIC_LOCOMOTION_CMD,
)

Deliver = Callable[[str, str], None]


def build_mqtt_client(deliver: Deliver, topics: tuple = SUBSCRIBED_TOPICS) -> mqtt.Client:
    """MQTT client that passes (topic, payload) of every subscribed message to ``deliver``.

    ``topics`` defaults to all of SUBSCRIBED_TOPICS; main.py leaves out the ones its own tasks deliver
    in-process, so each message is applied exactly once.

    ``deliver`` runs on paho's network thread; it must hand the message to the tick thread
    rather than touching SharedState itself.
    """
    last_oversize_warning = [0.0]

    def on_connect(client: mqtt.Client, _userdata, _flags, reason_code, _properties) -> None:
        if reason_code == 0:  # failures are logged by mqtt_client.make_client
            for topic in topics:
                client.subscribe(topic)

    def on_message(_client: mqtt.Client, _userdata, msg: mqtt.MQTTMessage) -> None:
        try:
            if len(msg.payload) > MAX_PAYLOAD_BYTES:  # checked before decoding anything
                now = time.monotonic()
                if now - last_oversize_warning[0] > 1.0:
                    last_oversize_warning[0] = now
                    logger.warning("Dropping %d-byte MQTT message on %s (limit %d)",
                                   len(msg.payload), msg.topic, MAX_PAYLOAD_BYTES)
                return
            deliver(msg.topic, msg.payload.decode("utf-8", errors="ignore").strip())
        except Exception:  # noqa: BLE001 - an exception here would kill paho's network thread
            logger.exception("Dropping MQTT message on %s", msg.topic)

    return make_client("robot-behavior-tree", on_connect=on_connect, on_message=on_message)


def drain_queues(bus: CommandBus) -> None:
    # Standalone mode: print what would be sent to the controller / audio pipeline.
    while not bus.motor_queue.empty():
        print(f"MOTOR_CMD: {bus.motor_queue.get_nowait()}")
    while not bus.audio_queue.empty():
        print(f"AUDIO_CMD: {bus.audio_queue.get_nowait()}")


def main() -> None:
    """Standalone mode: run the tree without a controller, printing the commands it would send.

    Every command is treated as acknowledged, as if a healthy controller were attached.
    """
    state = SharedState()
    bus = CommandBus()
    tree = build_tree(state, bus)
    inbox: "queue.SimpleQueue[tuple[str, str]]" = queue.SimpleQueue()
    mqtt_client = build_mqtt_client(lambda topic, payload: inbox.put((topic, payload)))

    print("Behavior tree running at 10Hz (Ctrl+C to stop)")
    try:
        period = 0.1
        next_tick = time.monotonic()
        while True:
            while not inbox.empty():
                safe_apply(state, *inbox.get_nowait())
            tree.tick()
            while not bus.motor_queue.empty():
                command = bus.motor_queue.get_nowait()
                print(f"MOTOR_CMD: {command}")
                apply_serial_line(state, "ACK," + command)
            drain_queues(bus)
            next_tick += period
            sleep_time = next_tick - time.monotonic()
            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                next_tick = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        stop_client(mqtt_client)


if __name__ == "__main__":
    main()
