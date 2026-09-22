"""Tests for serial_module.py: reply matching, READY banner, line forwarding, E-stop priority,
parking on shutdown, sim mode, and the Issues.md regressions (#2, #4, #6)."""
import asyncio
import logging

import pytest

import serial_module


class FakeSerial:
    def __init__(self, replies):
        self.replies = list(replies)
        self.written = []
        self.flushed = False
        self.is_open = True

    def write(self, data):
        self.written.append(data)
        return len(data)

    def readline(self):
        return self.replies.pop(0) if self.replies else b""

    def reset_input_buffer(self):
        self.flushed = True


@pytest.mark.parametrize("command, line, expected", [
    ("S", "ACK,S", True),
    ("S", "ACK,S,NOIMU", True),
    ("S", "NACK,S,TILTED", True),
    ("S", "ACK,W,0,0", False),
    ("W,50,0", "ACK,W,50,0", True),
    ("W,50,0", "NACK,W,MODE", True),
    ("C", "ACK,C,503", True),
    ("P", "ACK,C,503", False),  # a late calibration reply is not the ping's reply
    ("J,0,90", "ACK,0,90,90", True),
    ("J,0,90", "NACK,J,JOINT", True),
    ("J,0,90", "ACK,P", False),
    ("P", "NACK,?,OVERFLOW", False),
])
def test_reply_matching(command, line, expected):
    assert serial_module.is_reply_to(command, line) is expected


def test_ack_with_fields_is_accepted():
    ser = FakeSerial([b"ACK,0,90,90\r\n"])
    assert serial_module.send_and_wait(ser, "J,0,90\n") == "ACK,0,90,90"
    assert ser.written == [b"J,0,90\n"]


def test_nack_and_timeout():
    assert serial_module.send_and_wait(FakeSerial([b"NACK,J,JOINT\n"]), "J,99,0\n") == "NACK,J,JOINT"
    assert serial_module.send_and_wait(FakeSerial([]), "J,0,90\n", timeout_s=0.05) == ""


def test_late_reply_is_not_taken_for_the_next_commands_reply():
    """Issue #2: calibration's ACK arriving after its timeout used to shift every later reply by one."""
    seen = []
    ser = FakeSerial([b"ACK,C,503\n", b"ACK,P\n"])
    assert serial_module.send_and_wait(ser, "P\n", on_line=seen.append) == "ACK,P"
    assert seen == ["ACK,C,503", "ACK,P"]  # the late reply still reaches the handler


def test_slow_commands_get_longer_timeouts():
    assert serial_module.reply_timeout("C") > 0.34  # the Nano samples the IMU for ~0.35 s
    assert serial_module.reply_timeout("S") >= 1.0  # S may re-initialise a lost IMU
    assert serial_module.reply_timeout("W,10,0") == serial_module.REPLY_TIMEOUT_S


def test_skips_noise_until_reply():
    ser = FakeSerial([b"", b"garbage\n", b"ACK,E\n"])
    assert serial_module.send_and_wait(ser, "E\n") == "ACK,E"


def test_nano_reset_is_reported():
    assert serial_module.send_and_wait(FakeSerial([b"READY,IMU\n"]), "S\n") == "READY,IMU"


def test_every_line_is_forwarded():
    seen = []
    ser = FakeSerial([b"T,12,-3,40,B\n", b"EVT,WATCHDOG\n", b"ACK,P\n"])
    assert serial_module.send_and_wait(ser, "P\n", on_line=seen.append) == "ACK,P"
    assert seen == ["T,12,-3,40,B", "EVT,WATCHDOG", "ACK,P"]


def test_wait_for_ready_flushes_boot_noise():
    ser = FakeSerial([b"\x00\xff", b"READY,NOIMU\r\n"])
    assert serial_module.wait_for_ready(ser, timeout_s=1.0) == "READY,NOIMU"
    assert ser.flushed
    assert serial_module.wait_for_ready(FakeSerial([]), timeout_s=0.05) is None


def test_offer_drops_oldest_when_full():
    async def run():
        q = asyncio.Queue(maxsize=2)
        for item in ("a", "b", "c"):
            serial_module.offer(q, item)
        return [q.get_nowait(), q.get_nowait()]
    assert asyncio.run(run()) == ["b", "c"]


def test_estop_jumps_the_queue():
    """Issue #4: E used to wait behind every queued command (and a full queue dropped it)."""
    async def run():
        q = asyncio.Queue(maxsize=3)
        for item in ("W,50,0", "W,50,0", "G,1"):
            q.put_nowait(item)
        serial_module.offer_urgent(q, "E")
        return q.qsize(), q.get_nowait()
    assert asyncio.run(run()) == (1, "E")


def test_park_stops_walking_and_relaxes():
    """Issue #6: servos used to stay powered after the Pi software exited."""
    ser = FakeSerial([b"ACK,W,0,0\n", b"ACK,O\n"])
    serial_module.park(ser)
    assert ser.written == [b"W,0,0\n", b"O\n"]


def test_sim_mode_acks_commands(caplog):
    async def run():
        q = asyncio.Queue()
        seen = []
        task = asyncio.create_task(serial_module.serial_task(q, port="sim", on_line=seen.append))
        for command in ("S", "J,0,90", "W,20,0"):
            await q.put(command)
        await asyncio.wait_for(q.join(), 1.0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return seen

    with caplog.at_level(logging.INFO, logger="serial_module"):
        seen = asyncio.run(run())
    assert seen == ["ACK,S", "ACK,0,90,90", "ACK,W,20,0"]
    assert "SIM serial -> S" in caplog.text
    assert "parking servos" in caplog.text
