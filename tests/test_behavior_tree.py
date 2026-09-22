"""Tests for behavior_tree_module.py: standing, walking, E-stop, falls, IMU faults, rest, calibration,
priorities, and the Issues.md regressions (#1, #3, #5, #7, #10)."""
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


def nano(state, *lines):
    for line in lines:
        bt.apply_serial_line(state, line)


def standing(robot):
    """Tick until S is sent, then acknowledge it like a healthy Nano."""
    state, bus, tree = robot
    motor, _ = tick(tree, bus)
    assert motor == ["S"]
    nano(state, "ACK,S")
    return state, bus, tree


# ---------------------------------------------------------------- standing (#5)

def test_stand_is_confirmed_by_ack_not_by_sending(robot):
    state, bus, tree = robot
    motor, _ = tick(tree, bus, 5)
    assert motor == ["S"]  # sent once, then waits for the ACK
    assert state.legs_standing is False
    nano(state, "ACK,S")
    assert state.legs_standing is True
    assert tick(tree, bus, 20)[0] == []


def test_unanswered_stand_is_retried(robot):
    state, bus, tree = robot
    tick(tree, bus)
    state.stand_sent_at -= bt.STAND_RETRY_S + 0.1  # no ACK within the retry time
    motor, _ = tick(tree, bus)
    assert motor == ["S"]


def test_nack_mode_on_walk_makes_it_stand_again(robot):
    state, bus, tree = standing(robot)
    loco(state, "walk,50,0,5")
    assert tick(tree, bus)[0] == ["W,50,0"]
    nano(state, "NACK,W,MODE")  # the Nano wasn't balancing after all
    assert state.legs_standing is False
    state.stand_sent_at -= bt.STAND_RETRY_S + 0.1
    motor, _ = tick(tree, bus)
    assert motor == ["S"]


def test_nano_reset_stands_again(robot):
    state, bus, tree = standing(robot)
    nano(state, "READY,IMU")
    motor, _ = tick(tree, bus, 3)
    assert motor == ["S"]


# ---------------------------------------------------------------- walking

def test_walk_waits_for_stand_ack(robot):
    state, bus, tree = robot
    loco(state, "walk,60,0,5")
    motor, _ = tick(tree, bus, 3)
    assert motor == ["S"]  # no W until the robot is confirmed balancing
    nano(state, "ACK,S")
    motor, _ = tick(tree, bus)
    assert motor == ["W,60,0"]


def test_walk_sends_heartbeat_then_stops(robot):
    state, bus, tree = standing(robot)
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
    state, bus, tree = standing(robot)
    loco(state, "walk,50,0,5")
    tick(tree, bus)
    loco(state, "stop")
    motor, _ = tick(tree, bus)
    assert motor == ["W,0,0"]


def test_watchdog_event_forces_resend():
    state = bt.SharedState(walking=True)
    nano(state, "EVT,WATCHDOG")
    assert state.walking is False


# ---------------------------------------------------------------- bad input (#1)

@pytest.mark.parametrize("payload", [
    "walk", "walk,fast", "walk,inf,0", "walk,-inf,0", "walk,nan,0", "walk,50,0,inf",
    "gains,1", "gains,inf,1,1", "gains,nan,1,1", "gains,-1,0,0", "gains,1e9,0,0", "dance",
])
def test_malformed_commands_are_ignored(payload):
    state = bt.SharedState()
    bt.apply_topic_payload(state, config.TOPIC_LOCOMOTION_CMD, payload)  # must not raise
    assert state.walk_until == 0.0 and not state.outbox


def test_mqtt_callback_never_raises(monkeypatch):
    """An exception escaping a paho callback kills its network thread (and with it the E-stop)."""
    delivered = []
    client = bt.build_mqtt_client(lambda t, p: delivered.append((t, p)))
    client.loop_stop()

    class Msg:
        topic = config.TOPIC_LOCOMOTION_CMD
        payload = b"walk,inf,0"

    client.on_message(client, None, Msg())
    assert delivered == [(config.TOPIC_LOCOMOTION_CMD, "walk,inf,0")]

    def explode(t, p):
        raise RuntimeError("boom")

    client2 = bt.build_mqtt_client(explode)
    client2.loop_stop()
    client2.on_message(client2, None, Msg())  # logged, not raised

    state = bt.SharedState()
    bt.safe_apply(state, config.TOPIC_LOCOMOTION_CMD, "gains,inf,1,1")  # logged, not raised


# ---------------------------------------------------------------- E-stop

def test_estop_latches_once_and_restands_after_release(robot):
    state, bus, tree = standing(robot)
    loco(state, "walk,50,0,5")
    tick(tree, bus)
    bt.apply_topic_payload(state, config.TOPIC_ERROR, "error")
    motor, audio = tick(tree, bus, 20)
    assert motor == ["E"]
    assert audio == [bt.AUDIO_EMERGENCY_STOP]
    assert state.legs_standing is False

    # #18: no "stop" here on purpose: the E-stop itself must have cancelled the 5 s walk
    bt.apply_topic_payload(state, config.TOPIC_ERROR, "clear")
    motor, _ = tick(tree, bus, 2)
    assert motor == ["R", "S"]
    nano(state, "ACK,S")
    motor, _ = tick(tree, bus, 5)
    assert not any(m.startswith("W,50") for m in motor)  # does not walk off again after "clear"


# ---------------------------------------------------------------- round 2 (#14, #15, #19, #20)

def test_estop_survives_a_command_flood(robot):
    """#14: a flood of tuning commands used to fill the motor queue so E was silently dropped."""
    state, bus, tree = standing(robot)
    for _ in range(1000):
        loco(state, "gesture")
        loco(state, "telemetry,1")
    assert len(state.outbox) <= bt.OUTBOX_MAX  # the flood is capped, not buffered
    for _ in range(bus.motor_queue.maxsize):  # and even with the motor queue completely full...
        bus.put_motor("G,1")
    bt.apply_topic_payload(state, config.TOPIC_ERROR, "error")
    motor, _ = tick(tree, bus)
    assert motor[0] == "E"  # ...E goes out first
    assert "G,1" not in motor  # queued commands are discarded behind it


def test_estop_latches_only_once_queued(robot):
    state, bus, tree = standing(robot)
    bus.put_urgent = lambda command: False  # simulate E failing to queue
    bt.apply_topic_payload(state, config.TOPIC_ERROR, "error")
    tick(tree, bus)
    guard = tree.root.children[0]
    assert isinstance(guard, bt.EStopGuard) and guard.latched is False  # retried next tick


def test_stood_up_without_imu_is_relaxed_again(robot, monkeypatch):
    """#15: if the controller stands without its IMU anyway (ACK,S,NOIMU), it is relaxed at once."""
    monkeypatch.setattr(config, "ALLOW_NO_IMU", False)
    state, bus, tree = robot
    nano(state, "READY,NOIMU")
    assert tick(tree, bus)[0] == ["O"]
    nano(state, "ACK,S,NOIMU")  # e.g. an S sent by hand over the serial console
    assert state.imu_fault is True
    assert tick(tree, bus)[0] == ["O"]  # relaxed again


def test_imu_retry_never_stands_the_robot_blind(robot, monkeypatch):
    """#28: the retry used to be an S, which stands the robot with no balance if the IMU is still missing."""
    monkeypatch.setattr(config, "ALLOW_NO_IMU", False)
    state, bus, tree = robot
    nano(state, "READY,NOIMU")
    tick(tree, bus)
    loco(state, "stand")
    motor, _ = tick(tree, bus, 5)
    assert motor == ["I"]  # I re-initialises the IMU without moving a servo
    nano(state, "NACK,I,NOIMU")
    assert tick(tree, bus, 5)[0] == []  # still missing: nothing moves
    assert state.imu_fault is True


def test_bench_mode_calibrate_without_imu_is_not_a_fault(robot, monkeypatch):
    """#15: with ALLOW_NO_IMU=1, NACK,C,NOIMU (calibrate needs the IMU) must not lock the robot up."""
    monkeypatch.setattr(config, "ALLOW_NO_IMU", True)
    state, bus, tree = robot
    nano(state, "READY,NOIMU")
    nano(state, "NACK,C,NOIMU")
    assert state.imu_fault is False
    tick(tree, bus)
    nano(state, "ACK,S,NOIMU")
    assert state.imu_fault is False and state.legs_standing


def test_flash_writes_are_rate_limited(robot):
    """#19: every gains/calibrate rewrites the controller's flash; one of each per second at most."""
    state, bus, tree = standing(robot)
    for i in range(50):
        loco(state, f"gains,0.{i + 10},3,0.03")
    motor, _ = tick(tree, bus)
    assert [m for m in motor if m.startswith("K,")] == ["K,10,300,3"]
    state.last_flash_write["gains"] -= bt.FLASH_WRITE_MIN_S  # a second later
    loco(state, "gains,0.9,3,0.03")
    assert tick(tree, bus)[0] == ["K,90,300,3"]


def test_calibrate_then_gains_both_go_through(robot):
    """#31: calibrate and gains used to share one timer, so gains right after calibrate was lost."""
    state, bus, tree = standing(robot)
    loco(state, "calibrate")
    loco(state, "gains,0.9,3,0.03")
    motor, _ = tick(tree, bus)
    assert motor[:3] == ["O", "C", "K,90,300,3"]


def test_dropped_command_does_not_spend_the_rate_limit(robot):
    """#31: a gains dropped because the outbox was full used to block the next valid one for 1 s."""
    state, bus, tree = standing(robot)
    for _ in range(bt.OUTBOX_MAX):
        loco(state, "gesture")
    loco(state, "gains,0.5,3,0.03")  # dropped: outbox full
    tick(tree, bus)
    loco(state, "gains,0.6,3,0.03")  # must not be refused by the rate limit
    assert tick(tree, bus)[0] == ["K,60,300,3"]


def test_commands_during_estop_never_run_later(robot):
    """#29: calibrate/gesture sent while E-stopped used to run after "clear" (a stale calibration in flash)."""
    state, bus, tree = standing(robot)
    bt.apply_topic_payload(state, config.TOPIC_ERROR, "error")
    tick(tree, bus)
    for payload in ("calibrate", "gesture", "gains,0.9,3,0.03", "telemetry,1", "walk,50,0,5"):
        loco(state, payload)
    bt.apply_topic_payload(state, config.TOPIC_ERROR, "clear")
    motor, _ = tick(tree, bus, 3)
    assert motor == ["R", "S"]
    assert state.walk_until == 0.0


def test_controller_reboot_during_estop_is_latched_again(robot):
    """#30: a controller that resets mid-E-stop boots unlatched; E is sent again."""
    state, bus, tree = standing(robot)
    bt.apply_topic_payload(state, config.TOPIC_ERROR, "error")
    assert tick(tree, bus)[0] == ["E"]
    nano(state, "READY,IMU")  # controller rebooted (e.g. power glitch)
    assert tick(tree, bus)[0] == ["E"]
    assert tick(tree, bus, 3)[0] == []  # once only


def test_oversized_mqtt_payloads_are_dropped_unread():
    """#20: payloads over MAX_PAYLOAD_BYTES are dropped before decoding."""
    from mqtt_client import MAX_PAYLOAD_BYTES

    delivered = []
    client = bt.build_mqtt_client(lambda t, p: delivered.append(p))
    client.loop_stop()

    class Msg:
        topic = config.TOPIC_LOCOMOTION_CMD
        payload = b"x" * (MAX_PAYLOAD_BYTES + 1)

    client.on_message(client, None, Msg())
    Msg.payload = b"stand"
    client.on_message(client, None, Msg())
    assert delivered == ["stand"]


def test_estop_ignores_unknown_payloads():
    state = bt.SharedState()
    bt.apply_topic_payload(state, config.TOPIC_ERROR, "error")
    bt.apply_topic_payload(state, config.TOPIC_ERROR, "overcurrent")
    assert state.estop_active is True


# ---------------------------------------------------------------- falls

def test_fall_stops_everything_until_stand(robot):
    state, bus, tree = standing(robot)
    loco(state, "walk,50,0,5")
    tick(tree, bus)
    nano(state, "EVT,FALLEN")
    motor, audio = tick(tree, bus, 20)
    assert motor == []  # the Nano already switched the servos off; no walking or standing
    assert audio == [bt.AUDIO_FALLEN]

    loco(state, "stand")
    motor, _ = tick(tree, bus)
    assert motor == ["S"]


def test_refused_stand_while_tilted_counts_as_fallen(robot):
    state, bus, tree = robot
    tick(tree, bus)
    nano(state, "NACK,S,TILTED")
    assert state.fallen and not state.legs_standing


# ---------------------------------------------------------------- IMU faults (#3)

def test_imu_failure_relaxes_and_blocks_standing(robot):
    state, bus, tree = standing(robot)
    loco(state, "walk,50,0,5")
    tick(tree, bus)
    nano(state, "EVT,IMU_FAIL")
    motor, audio = tick(tree, bus, 20)
    assert motor == ["O"]  # servos off once; no S, no W
    assert audio == [bt.AUDIO_IMU_FAULT]


def test_stand_retries_imu_once_and_recovers(robot):
    state, bus, tree = standing(robot)
    nano(state, "EVT,IMU_FAIL")
    tick(tree, bus)
    loco(state, "stand")
    motor, _ = tick(tree, bus, 5)
    assert motor == ["I"]  # a single attempt, without moving: the controller re-initialises the IMU
    nano(state, "NACK,I,NOIMU")
    assert tick(tree, bus, 5)[0] == []  # still broken: stays relaxed

    loco(state, "stand")
    assert tick(tree, bus)[0] == ["I"]
    nano(state, "ACK,I")  # IMU back: now the normal branches stand the robot up
    assert state.imu_fault is False
    assert tick(tree, bus)[0] == ["S"]
    nano(state, "ACK,S")
    loco(state, "walk,30,0,2")
    assert tick(tree, bus)[0] == ["W,30,0"]


def test_boot_without_imu_is_a_fault_unless_allowed(robot, monkeypatch):
    state, bus, tree = robot
    nano(state, "READY,NOIMU")
    assert state.imu_fault is True
    monkeypatch.setattr(config, "ALLOW_NO_IMU", True)
    nano(state, "READY,NOIMU")
    assert state.imu_fault is False


# ---------------------------------------------------------------- rest, calibration, tuning

def test_rest_relaxes_until_stand(robot):
    state, bus, tree = standing(robot)
    loco(state, "rest")
    motor, _ = tick(tree, bus, 10)
    assert motor == ["O"]
    nano(state, "ACK,O")
    loco(state, "stand")
    motor, _ = tick(tree, bus)
    assert motor == ["S"]


def test_calibrate_relaxes_first(robot):
    state, bus, tree = standing(robot)
    loco(state, "calibrate")
    motor, _ = tick(tree, bus, 3)
    assert motor[:2] == ["O", "C"]
    assert "S" not in motor


def test_tuning_commands_pass_through(robot):
    state, bus, tree = standing(robot)
    loco(state, "telemetry,1")
    loco(state, "gains,0.8,3,0.03")
    loco(state, "gesture")
    motor, _ = tick(tree, bus)
    assert motor == ["T,1", "K,80,300,3", "G,1"]


# ---------------------------------------------------------------- conversation (#7) and posture

def test_conversation_stops_walking(robot):
    state, bus, tree = standing(robot)
    loco(state, "walk,50,0,5")
    tick(tree, bus)
    bt.apply_topic_payload(state, config.TOPIC_WAKE_FLAG, "1")
    motor, _ = tick(tree, bus, 10)
    assert motor == ["W,0,0"]


def test_stuck_conversation_flag_times_out(robot):
    state, bus, tree = standing(robot)
    bt.apply_topic_payload(state, config.TOPIC_WAKE_FLAG, "1")
    loco(state, "walk,40,0,5")
    assert tick(tree, bus)[0] == []  # talking: no walking
    state.conversation_since -= bt.CONVERSATION_TIMEOUT_S + 1  # the API task died, flag never lowered
    assert tick(tree, bus)[0] == ["W,40,0"]
    assert state.conversation_active is False


def test_clear_conversation():
    state = bt.SharedState(conversation_active=True)
    bt.clear_conversation(state)
    assert state.conversation_active is False


def test_posture_alert_bobs_once(robot):
    state, bus, tree = standing(robot)
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
    state, bus, tree = standing(robot)
    loco(state, "walk,40,0,5")
    bt.apply_topic_payload(state, config.TOPIC_STATE, "POSTURE_POOR")
    motor, _ = tick(tree, bus, 2)
    assert "W,40,0" not in motor
    state.posture_alert_ts -= bt.POSTURE_GESTURE_S + 0.1
    motor, _ = tick(tree, bus)
    assert motor == ["W,40,0"]


def test_estop_discards_queued_tuning_commands(robot):
    state, bus, tree = standing(robot)
    for _ in range(5):
        loco(state, "gesture")
    bt.apply_topic_payload(state, config.TOPIC_ERROR, "error")
    tick(tree, bus)
    bt.apply_topic_payload(state, config.TOPIC_ERROR, "clear")
    motor, _ = tick(tree, bus, 3)
    assert "G,1" not in motor  # nothing stale fires after the emergency stop


def test_command_flood_logs_once(robot, caplog):
    import logging

    state, _, _ = robot
    with caplog.at_level(logging.WARNING, logger="behavior_tree_module"):
        for _ in range(500):
            loco(state, "gesture")
    assert caplog.text.count("Too many queued tuning commands") == 1
