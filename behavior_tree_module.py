import math
import queue
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

import paho.mqtt.client as mqtt
import py_trees


MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883

TOPIC_STATE = "robot/state"
TOPIC_WAKE_FLAG = "robot/audio/wake_flag"
TOPIC_FACE_ERROR = "robot/vision/face_error"
TOPIC_ERROR = "robot/error"


@dataclass
class SharedState:
    estop_active: bool = False
    conversation_active: bool = False
    posture_poor: bool = False
    face_error: Optional[Tuple[float, float]] = None
    last_face_error_ts: float = 0.0


@dataclass
class CommandBus:
    motor_queue: "queue.Queue[str]" = field(default_factory=lambda: queue.Queue(maxsize=200))
    audio_queue: "queue.Queue[str]" = field(default_factory=lambda: queue.Queue(maxsize=50))

    def put_motor(self, command: str) -> None:
        try:
            self.motor_queue.put_nowait(command)
        except queue.Full:
            pass

    def put_audio(self, command: str) -> None:
        try:
            self.audio_queue.put_nowait(command)
        except queue.Full:
            pass


class CheckEStop(py_trees.behaviour.Behaviour):
    def __init__(self, state: SharedState):
        super().__init__(name="Check E-Stop")
        self.state = state

    def update(self) -> py_trees.common.Status:
        return py_trees.common.Status.SUCCESS if self.state.estop_active else py_trees.common.Status.FAILURE


class EStopAction(py_trees.behaviour.Behaviour):
    def __init__(self, bus: CommandBus):
        super().__init__(name="E-Stop Action")
        self.bus = bus

    def update(self) -> py_trees.common.Status:
        self.bus.put_motor("J,0,90")
        self.bus.put_motor("J,1,90")
        self.bus.put_audio("AUDIO,EMERGENCY_STOP")
        return py_trees.common.Status.SUCCESS


class CheckConversationActive(py_trees.behaviour.Behaviour):
    def __init__(self, state: SharedState):
        super().__init__(name="Check Conversation Active")
        self.state = state

    def update(self) -> py_trees.common.Status:
        return py_trees.common.Status.SUCCESS if self.state.conversation_active else py_trees.common.Status.FAILURE


class ConversationAction(py_trees.behaviour.Behaviour):
    def __init__(self, bus: CommandBus):
        super().__init__(name="Conversation Action")
        self.bus = bus

    def update(self) -> py_trees.common.Status:
        self.bus.put_motor("J,2,90")
        return py_trees.common.Status.SUCCESS


class CheckPosturePoor(py_trees.behaviour.Behaviour):
    def __init__(self, state: SharedState):
        super().__init__(name="Check Posture Poor")
        self.state = state

    def update(self) -> py_trees.common.Status:
        return py_trees.common.Status.SUCCESS if self.state.posture_poor else py_trees.common.Status.FAILURE


class PostureCorrectionAction(py_trees.behaviour.Behaviour):
    def __init__(self, bus: CommandBus):
        super().__init__(name="Posture Correction Action")
        self.bus = bus

    def update(self) -> py_trees.common.Status:
        self.bus.put_motor("J,3,120")
        self.bus.put_audio("AUDIO,POSTURE_WARNING")
        return py_trees.common.Status.SUCCESS


class FaceTrackingAction(py_trees.behaviour.Behaviour):
    def __init__(self, state: SharedState, bus: CommandBus):
        super().__init__(name="Face Tracking")
        self.state = state
        self.bus = bus
        self.kp = 0.04
        self.ki = 0.005
        self.kd = 0.01
        self.integral_x = 0.0
        self.integral_y = 0.0
        self.prev_x = 0.0
        self.prev_y = 0.0
        self.last_t = time.monotonic()
        self.pan = 90.0
        self.tilt = 90.0

    def update(self) -> py_trees.common.Status:
        if self.state.face_error is None:
            return py_trees.common.Status.FAILURE

        if (time.monotonic() - self.state.last_face_error_ts) > 0.5:
            return py_trees.common.Status.FAILURE

        ex, ey = self.state.face_error
        now = time.monotonic()
        dt = max(0.001, now - self.last_t)
        self.last_t = now

        self.integral_x += ex * dt
        self.integral_y += ey * dt

        dx = (ex - self.prev_x) / dt
        dy = (ey - self.prev_y) / dt
        self.prev_x = ex
        self.prev_y = ey

        out_x = (self.kp * ex) + (self.ki * self.integral_x) + (self.kd * dx)
        out_y = (self.kp * ey) + (self.ki * self.integral_y) + (self.kd * dy)

        self.pan = max(10.0, min(170.0, self.pan - out_x))
        self.tilt = max(10.0, min(170.0, self.tilt + out_y))

        self.bus.put_motor(f"J,0,{int(self.pan)}")
        self.bus.put_motor(f"J,1,{int(self.tilt)}")
        return py_trees.common.Status.SUCCESS


class IdleBreathingAction(py_trees.behaviour.Behaviour):
    def __init__(self, bus: CommandBus):
        super().__init__(name="Idle Breathing")
        self.bus = bus
        self.start_t = time.monotonic()

    def update(self) -> py_trees.common.Status:
        t = time.monotonic() - self.start_t
        # Slow, low-amplitude motion to simulate breathing.
        angle = 90.0 + 8.0 * math.sin(2.0 * math.pi * 0.2 * t)
        self.bus.put_motor(f"J,4,{int(angle)}")
        return py_trees.common.Status.SUCCESS


def parse_bool(payload: str) -> bool:
    return payload.strip().lower() in {"1", "true", "on", "yes"}


def apply_topic_payload(state: SharedState, topic: str, payload: str) -> None:
    if topic == TOPIC_ERROR:
        state.estop_active = payload.lower() == "error"
        return

    if topic == TOPIC_WAKE_FLAG:
        state.conversation_active = parse_bool(payload)
        return

    if topic == TOPIC_STATE:
        state.posture_poor = payload == "POSTURE_POOR"
        return

    if topic == TOPIC_FACE_ERROR:
        try:
            x_str, y_str = payload.split(",", 1)
            state.face_error = (float(x_str), float(y_str))
            state.last_face_error_ts = time.monotonic()
        except ValueError:
            pass


def on_mqtt_message(state: SharedState, _client: mqtt.Client, _userdata, msg: mqtt.MQTTMessage) -> None:
    payload = msg.payload.decode("utf-8", errors="ignore").strip()
    apply_topic_payload(state, msg.topic, payload)


def build_tree(state: SharedState, bus: CommandBus) -> py_trees.trees.BehaviourTree:
    root = py_trees.composites.Selector(name="Priority Selector", memory=False)

    estop_seq = py_trees.composites.Sequence(name="E-Stop", memory=False)
    estop_seq.add_children([CheckEStop(state), EStopAction(bus)])

    conversation_seq = py_trees.composites.Sequence(name="Conversation Active", memory=False)
    conversation_seq.add_children([CheckConversationActive(state), ConversationAction(bus)])

    posture_seq = py_trees.composites.Sequence(name="Posture Correction", memory=False)
    posture_seq.add_children([CheckPosturePoor(state), PostureCorrectionAction(bus)])

    face_tracking = FaceTrackingAction(state, bus)
    idle = IdleBreathingAction(bus)

    root.add_children([estop_seq, conversation_seq, posture_seq, face_tracking, idle])
    return py_trees.trees.BehaviourTree(root=root)


def build_mqtt_client(state: SharedState) -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="robot-behavior-tree")

    def on_connect(client: mqtt.Client, _userdata, _flags, reason_code, _properties) -> None:
        if reason_code == 0:
            client.subscribe(TOPIC_ERROR)
            client.subscribe(TOPIC_WAKE_FLAG)
            client.subscribe(TOPIC_STATE)
            client.subscribe(TOPIC_FACE_ERROR)
        else:
            print(f"MQTT connect failed: {reason_code}")

    client.on_connect = on_connect
    client.on_message = lambda c, u, m: on_mqtt_message(state, c, u, m)
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()
    return client


def run_tick_loop(tree: py_trees.trees.BehaviourTree, hz: float = 10.0) -> None:
    period = 1.0 / hz
    next_tick = time.monotonic()
    while True:
        tree.tick()
        next_tick += period
        sleep_time = next_tick - time.monotonic()
        if sleep_time > 0:
            time.sleep(sleep_time)
        else:
            next_tick = time.monotonic()


def drain_queues(bus: CommandBus) -> None:
    # Skeleton output drain: in production, forward these to serial/audio tasks.
    while not bus.motor_queue.empty():
        cmd = bus.motor_queue.get_nowait()
        print(f"MOTOR_CMD: {cmd}")
    while not bus.audio_queue.empty():
        cmd = bus.audio_queue.get_nowait()
        print(f"AUDIO_CMD: {cmd}")


def main() -> None:
    state = SharedState()
    bus = CommandBus()
    tree = build_tree(state, bus)
    mqtt_client = build_mqtt_client(state)

    print("Behavior tree running at 10Hz")
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