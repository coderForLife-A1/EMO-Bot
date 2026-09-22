"""10 Hz behavior tree for the two-legged EMO-Bot.

The Nano runs the fast loop (IMU balance PID + gait at 100 Hz). This tree only decides *what* the
legs should do and sends high-level commands: S (stand), W,<speed>,<turn> (walk), G,1 (gesture),
O (relax), E/R (E-stop). See firmware/emo_nano/emo_nano.ino for the protocol.
"""
import collections
import logging
import queue
import time
from dataclasses import dataclass, field
from typing import Optional

import paho.mqtt.client as mqtt
import py_trees

import config

logger = logging.getLogger(__name__)

WALK_HEARTBEAT_S = 0.2  # firmware stops walking if W isn't repeated within 1 s
WALK_DEFAULT_S = 2.0
WALK_MAX_S = 10.0  # a walk command never runs longer than this (desk robot: don't wander off)
POSTURE_GESTURE_S = 2.0  # how long the posture branch owns the robot after an alert

AUDIO_EMERGENCY_STOP = "AUDIO,EMERGENCY_STOP"
AUDIO_POSTURE_WARNING = "AUDIO,POSTURE_WARNING"
AUDIO_FALLEN = "AUDIO,FALLEN"

GESTURE_BOB = "G,1"

_ESTOP_ON = {"error", "estop", "stop", "1", "true", "on"}
_ESTOP_OFF = {"clear", "ok", "reset", "0", "false", "off"}


@dataclass
class SharedState:
    estop_active: bool = False
    conversation_active: bool = False
    posture_poor: bool = False
    posture_alert_ts: float = 0.0  # monotonic time of the latest POSTURE_POOR alert
    posture_alert_seq: int = 0  # increments per alert (timestamps can collide on coarse clocks)
    face_error: Optional[tuple[float, float]] = None
    face_error_seq: int = 0
    last_face_error_ts: float = 0.0
    # Legs
    fallen: bool = False  # set by the Nano's EVT,FALLEN / NACK,TILTED; cleared by a "stand" command
    resting: bool = False  # "rest" command: servos off until "stand"
    legs_standing: bool = False  # we believe the Nano is in balance mode
    walk_speed: int = 0  # -100..100
    walk_turn: int = 0  # -100..100
    walk_until: float = 0.0  # monotonic deadline of the current walk command
    walking: bool = False  # we sent a non-zero W that hasn't been stopped yet
    outbox: collections.deque = field(default_factory=collections.deque)  # raw tuning commands


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

    def put_audio(self, command: str) -> None:
        try:
            self.audio_queue.put_nowait(command)
        except queue.Full:
            pass


def ensure_standing(state: SharedState, bus: CommandBus) -> None:
    """Stop any walk and make sure the Nano is balancing (S is only sent when needed)."""
    if state.walking:
        bus.put_motor("W,0,0")
        state.walking = False
    if not state.legs_standing:
        if bus.put_motor("S"):
            state.legs_standing = True


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
    """Latches the Nano's E-stop ('E' = all servos off) once, and releases it ('R') when cleared."""

    def __init__(self, state: SharedState, bus: CommandBus):
        super().__init__(name="E-Stop")
        self.state = state
        self.bus = bus
        self.latched = False

    def update(self) -> py_trees.common.Status:
        if self.state.estop_active:
            if not self.latched:
                self.bus.put_motor("E")
                self.bus.put_audio(AUDIO_EMERGENCY_STOP)
                self.state.legs_standing = self.state.walking = False
                self.latched = True
            return py_trees.common.Status.SUCCESS

        if self.latched:
            self.bus.put_motor("R")  # servos stay off; a lower branch sends S to stand again
            self.latched = False
        return py_trees.common.Status.FAILURE


class FallenGuard(py_trees.behaviour.Behaviour):
    """After a fall the Nano has switched the servos off. Ask for help once and wait for "stand"."""

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
        self.state.legs_standing = self.state.walking = False
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
            self.state.legs_standing = self.state.walking = False
            self.sent = True
        return py_trees.common.Status.SUCCESS


class CheckConversationActive(py_trees.behaviour.Behaviour):
    def __init__(self, state: SharedState):
        super().__init__(name="Check Conversation Active")
        self.state = state

    def update(self) -> py_trees.common.Status:
        return py_trees.common.Status.SUCCESS if self.state.conversation_active else py_trees.common.Status.FAILURE


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
            self.bus.put_motor(GESTURE_BOB)
            self.bus.put_audio(AUDIO_POSTURE_WARNING)
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

        if not self.state.legs_standing and self.bus.put_motor("S"):
            self.state.legs_standing = True
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


def _clamp_int(text: str, low: int, high: int) -> int:
    return max(low, min(high, int(float(text))))


def apply_locomotion_command(state: SharedState, payload: str) -> None:
    """robot/locomotion/cmd payloads:

    stand | rest | stop | walk,<speed>,<turn>[,<seconds>] | gesture | telemetry,<0|1> |
    gains,<kp>,<ki>,<kd> (floats) | calibrate
    """
    parts = [p.strip() for p in payload.strip().lower().split(",")]
    cmd, args = parts[0], parts[1:]
    try:
        if cmd == "stand":
            state.fallen = state.resting = False
            state.walk_until = 0.0
        elif cmd == "rest":
            state.resting = True
            state.walk_until = 0.0
        elif cmd == "stop":
            state.walk_until = 0.0
        elif cmd == "walk":
            state.walk_speed = _clamp_int(args[0], -100, 100)
            state.walk_turn = _clamp_int(args[1], -100, 100) if len(args) > 1 else 0
            seconds = float(args[2]) if len(args) > 2 else WALK_DEFAULT_S
            state.walk_until = time.monotonic() + max(0.0, min(WALK_MAX_S, seconds))
            state.resting = False
        elif cmd == "gesture":
            state.outbox.append(GESTURE_BOB)
        elif cmd == "telemetry":
            state.outbox.append(f"T,{1 if parse_bool(args[0]) else 0}")
        elif cmd == "gains":
            kp, ki, kd = (round(float(a) * 100) for a in args[:3])
            state.outbox.append(f"K,{kp},{ki},{kd}")
        elif cmd == "calibrate":
            # Calibration needs the balance loop off: relax, measure, then stay relaxed until "stand".
            state.resting = True
            state.walk_until = 0.0
            state.outbox.extend(["O", "C"])
        else:
            logger.warning("Unknown locomotion command %r", payload)
    except (IndexError, ValueError):
        logger.warning("Malformed locomotion command %r", payload)


def apply_serial_line(state: SharedState, line: str) -> None:
    """React to unsolicited lines from the Nano (events, refusals)."""
    if line in ("EVT,FALLEN", "NACK,TILTED"):
        state.fallen = True
        state.legs_standing = state.walking = False
    elif line == "EVT,WATCHDOG":
        state.walking = False
    elif line.startswith("READY"):
        on_nano_reset(state)


def on_nano_reset(state: SharedState) -> None:
    """The Nano (re)booted with its servos off: stand again on the next tick."""
    state.legs_standing = state.walking = False


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
            state.face_error = (float(x_str), float(y_str))
        except ValueError:
            return
        state.face_error_seq += 1
        state.last_face_error_ts = time.monotonic()


def on_mqtt_message(state: SharedState, _client: mqtt.Client, _userdata, msg: mqtt.MQTTMessage) -> None:
    payload = msg.payload.decode("utf-8", errors="ignore").strip()
    apply_topic_payload(state, msg.topic, payload)


def build_tree(state: SharedState, bus: CommandBus) -> py_trees.trees.BehaviourTree:
    root = py_trees.composites.Selector(name="Priority Selector", memory=False)

    conversation_seq = py_trees.composites.Sequence(name="Conversation Active", memory=False)
    conversation_seq.add_children([CheckConversationActive(state), ConversationAction(state, bus)])

    posture_seq = py_trees.composites.Sequence(name="Posture Correction", memory=False)
    posture_seq.add_children([CheckPosturePoor(state), PostureCorrectionAction(state, bus)])

    root.add_children([
        ServiceCommands(state, bus),
        EStopGuard(state, bus),
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


def build_mqtt_client(state: SharedState) -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="robot-behavior-tree")

    def on_connect(client: mqtt.Client, _userdata, _flags, reason_code, _properties) -> None:
        if reason_code == 0:
            for topic in SUBSCRIBED_TOPICS:
                client.subscribe(topic)
        else:
            logger.error("MQTT connect failed: %s", reason_code)

    client.on_connect = on_connect
    client.on_message = lambda c, u, m: on_mqtt_message(state, c, u, m)
    client.connect_async(config.MQTT_HOST, config.MQTT_PORT, keepalive=30)
    client.loop_start()
    return client


def drain_queues(bus: CommandBus) -> None:
    # Standalone mode: print what would be sent to the Nano / audio pipeline.
    while not bus.motor_queue.empty():
        print(f"MOTOR_CMD: {bus.motor_queue.get_nowait()}")
    while not bus.audio_queue.empty():
        print(f"AUDIO_CMD: {bus.audio_queue.get_nowait()}")


def main() -> None:
    state = SharedState()
    bus = CommandBus()
    tree = build_tree(state, bus)
    mqtt_client = build_mqtt_client(state)

    print("Behavior tree running at 10Hz (Ctrl+C to stop)")
    try:
        period = 0.1
        next_tick = time.monotonic()
        while True:
            tree.tick()
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
        mqtt_client.loop_stop()
        mqtt_client.disconnect()


if __name__ == "__main__":
    main()
