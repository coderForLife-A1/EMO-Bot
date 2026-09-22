"""Tests for behavior_tree_module.py: standing, walking, E-stop, falls, rest, calibration, priorities."""
import time

import pytest

import behavior_tree_module as bt
import config


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


@pytest.fixture
def robot():
    state, bus = bt.SharedState(), bt.CommandBus()
    return state, bus, bt.build_tree(state, bus)


def tick(tree, bus, n=1):
    motor, audio = [], []
    for _ in range(n):
        tree.tick()
        motor += drain(bus.motor_queue)
        audio += drain(bus.audio_queue)
    return motor, audio


def loco(state, payload):
    bt.apply_topic_payload(state, config.TOPIC_LOCOMOTION_CMD, payload)


def test_stands_once_at_startup(robot):
    _, bus, tree = robot
    motor, audio = tick(tree, bus, 20)
    assert motor == ["S"]
    assert audio == []


def test_nano_reset_stands_again(robot):
    state, bus, tree = robot
    tick(tree, bus)
    bt.apply_serial_line(state, "READY,IMU")
    motor, _ = tick(tree, bus, 3)
    assert motor == ["S"]


def test_walk_sends_heartbeat_then_stops(robot, monkeypatch):
    state, bus, tree = robot
    tick(tree, bus)
    loco(state, "walk,60,-20,1")
    motor = []
    for _ in range(12):  # 1.2 s at 10 Hz
        m, _ = tick(tree, bus)
        motor += m
        time.sleep(0.1)
    walks = [m for m in motor if m == "W,60,-20"]
    assert 3 <= len(walks) <= 7  # heartbeat every ~0.2 s, well inside the Nano's 1 s watchdog
    assert motor[-1] == "W,0,0"  # explicit stop when the command expires
    assert state.walking is False


def test_walk_arguments_are_clamped():
    state = bt.SharedState()
    loco(state, "walk,500,-300,99")
    assert (state.walk_speed, state.walk_turn) == (100, -100)
    assert state.walk_until - time.monotonic() <= bt.WALK_MAX_S


def test_stop_command(robot):
    state, bus, tree = robot
    tick(tree, bus)
    loco(state, "walk,50,0,5")
    tick(tree, bus)
    loco(state, "stop")
    motor, _ = tick(tree, bus)
    assert motor == ["W,0,0"]


def test_malformed_commands_are_ignored(robot):
    state, bus, tree = robot
    for payload in ("walk", "walk,fast", "gains,1", "dance"):
        loco(state, payload)
    assert state.walk_until == 0.0 and not state.outbox


def test_estop_latches_once_and_restands_after_release(robot):
    state, bus, tree = robot
    tick(tree, bus)
    loco(state, "walk,50,0,5")
    tick(tree, bus)
    bt.apply_topic_payload(state, config.TOPIC_ERROR, "error")
    motor, audio = tick(tree, bus, 20)
    assert motor == ["E"]
    assert audio == [bt.AUDIO_EMERGENCY_STOP]

    loco(state, "stop")
    bt.apply_topic_payload(state, config.TOPIC_ERROR, "clear")
    motor, _ = tick(tree, bus, 2)
    assert motor == ["R", "S"]


def test_estop_ignores_unknown_payloads():
    state = bt.SharedState()
    bt.apply_topic_payload(state, config.TOPIC_ERROR, "error")
    bt.apply_topic_payload(state, config.TOPIC_ERROR, "overcurrent")
    assert state.estop_active is True


def test_fall_stops_everything_until_stand(robot):
    state, bus, tree = robot
    tick(tree, bus)
    loco(state, "walk,50,0,5")
    tick(tree, bus)
    bt.apply_serial_line(state, "EVT,FALLEN")
    motor, audio = tick(tree, bus, 20)
    assert motor == []  # the Nano already switched the servos off; no walking or standing
    assert audio == [bt.AUDIO_FALLEN]

    loco(state, "stand")
    motor, _ = tick(tree, bus)
    assert motor == ["S"]


def test_refused_stand_while_tilted_counts_as_fallen(robot):
    state, bus, tree = robot
    tick(tree, bus)
    bt.apply_serial_line(state, "NACK,TILTED")
    assert state.fallen and not state.legs_standing


def test_rest_relaxes_until_stand(robot):
    state, bus, tree = robot
    tick(tree, bus)
    loco(state, "rest")
    motor, _ = tick(tree, bus, 10)
    assert motor == ["O"]
    loco(state, "stand")
    motor, _ = tick(tree, bus)
    assert motor == ["S"]


def test_calibrate_relaxes_first(robot):
    state, bus, tree = robot
    tick(tree, bus)
    loco(state, "calibrate")
    motor, _ = tick(tree, bus, 3)
    assert motor[:2] == ["O", "C"] or motor == ["O", "C", "O"][: len(motor)]
    assert "S" not in motor


def test_tuning_commands_pass_through(robot):
    state, bus, tree = robot
    tick(tree, bus)
    loco(state, "telemetry,1")
    loco(state, "gains,0.8,3,0.03")
    loco(state, "gesture")
    motor, _ = tick(tree, bus)
    assert motor == ["T,1", "K,80,300,3", "G,1"]


def test_conversation_stops_walking(robot):
    state, bus, tree = robot
    tick(tree, bus)
    loco(state, "walk,50,0,5")
    tick(tree, bus)
    bt.apply_topic_payload(state, config.TOPIC_WAKE_FLAG, "1")
    motor, _ = tick(tree, bus, 10)
    assert motor == ["W,0,0"]


def test_posture_alert_bobs_once(robot):
    state, bus, tree = robot
    tick(tree, bus)
    bt.apply_topic_payload(state, config.TOPIC_STATE, "POSTURE_POOR")
    motor, audio = tick(tree, bus, 5)
    assert motor == [bt.GESTURE_BOB]
    assert audio == [bt.AUDIO_POSTURE_WARNING]

    bt.apply_topic_payload(state, config.TOPIC_STATE, "POSTURE_POOR")  # reminder
    motor, audio = tick(tree, bus, 5)
    assert motor == [bt.GESTURE_BOB] and audio == [bt.AUDIO_POSTURE_WARNING]


def test_posture_ok_clears_state():
    state = bt.SharedState()
    bt.apply_topic_payload(state, config.TOPIC_STATE, "POSTURE_POOR")
    bt.apply_topic_payload(state, config.TOPIC_STATE, "POSTURE_OK")
    assert state.posture_poor is False


def test_walking_resumes_after_posture_window(robot):
    state, bus, tree = robot
    tick(tree, bus)
    loco(state, "walk,40,0,5")
    bt.apply_topic_payload(state, config.TOPIC_STATE, "POSTURE_POOR")
    motor, _ = tick(tree, bus, 2)
    assert "W,40,0" not in motor
    state.posture_alert_ts -= bt.POSTURE_GESTURE_S + 0.1
    motor, _ = tick(tree, bus)
    assert motor == ["W,40,0"]


def test_watchdog_event_forces_resend():
    state = bt.SharedState(walking=True)
    bt.apply_serial_line(state, "EVT,WATCHDOG")
    assert state.walking is False
