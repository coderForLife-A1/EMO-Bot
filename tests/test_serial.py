"""Tests for serial_module.py: reply parsing, READY banner, event forwarding, queueing, sim mode."""
import asyncio
import logging

import serial_module


class FakeSerial:
    def __init__(self, replies):
        self.replies = list(replies)
        self.written = []
        self.flushed = False

    def write(self, data):
        self.written.append(data)
        return len(data)

    def readline(self):
        return self.replies.pop(0) if self.replies else b""

    def reset_input_buffer(self):
        self.flushed = True


def test_ack_with_fields_is_accepted():
    ser = FakeSerial([b"ACK,0,90,90\r\n"])
    assert serial_module.send_and_wait(ser, "J,0,90\n") == "ACK,0,90,90"
    assert ser.written == [b"J,0,90\n"]


def test_nack_and_timeout():
    assert serial_module.send_and_wait(FakeSerial([b"NACK,JOINT\n"]), "J,99,0\n") == "NACK,JOINT"
    assert serial_module.send_and_wait(FakeSerial([]), "J,0,90\n", timeout_s=0.05) == ""


def test_skips_noise_until_reply():
    ser = FakeSerial([b"", b"garbage\n", b"ACK,E\n"])
    assert serial_module.send_and_wait(ser, "E\n") == "ACK,E"


def test_nano_reset_is_reported():
    assert serial_module.send_and_wait(FakeSerial([b"READY,IMU\n"]), "S\n") == "READY,IMU"


def test_events_and_telemetry_are_forwarded():
    seen = []
    ser = FakeSerial([b"T,12,-3,40,B\n", b"EVT,WATCHDOG\n", b"ACK,P\n"])
    assert serial_module.send_and_wait(ser, "P\n", on_line=seen.append) == "ACK,P"
    assert seen == ["T,12,-3,40,B", "EVT,WATCHDOG"]


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


def test_sim_mode_consumes_commands(caplog):
    async def run():
        q = asyncio.Queue()
        task = asyncio.create_task(serial_module.serial_task(q, port="sim"))
        await q.put("J,0,90")
        await asyncio.wait_for(q.join(), 1.0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    with caplog.at_level(logging.INFO, logger="serial_module"):
        asyncio.run(run())
    assert "SIM serial -> J,0,90" in caplog.text
