"""Serial link between the Pi and the Arduino Nano.

Opens the port (device path, or a socket:// URL for tools/sim_nano.py), waits for the firmware's
READY banner, sends one command at a time and checks the ACK/NACK reply, forwards unsolicited
events and telemetry, pings when idle, and reconnects forever. SERIAL_PORT=sim only logs commands.
"""
import asyncio
import contextlib
import logging
import time
from typing import Callable, Optional

import serial
from serial import SerialException

import config

logger = logging.getLogger(__name__)

READY_TIMEOUT_S = 3.0  # opening the port resets a USB Nano; its bootloader takes ~1-2 s
REPLY_TIMEOUT_S = 0.3
IDLE_POLL_S = 0.5  # ping when idle so events (EVT,FALLEN) are read promptly

LineHandler = Callable[[str], None]


def offer(queue: asyncio.Queue, item) -> None:
	"""Non-blocking put that drops the oldest item when full, so producers never stall."""
	while True:
		try:
			queue.put_nowait(item)
			return
		except asyncio.QueueFull:
			with contextlib.suppress(asyncio.QueueEmpty):
				queue.get_nowait()
				queue.task_done()


def wait_for_ready(ser, timeout_s: float = READY_TIMEOUT_S) -> Optional[str]:
	"""Wait for the firmware's READY banner ("READY,IMU" / "READY,NOIMU"), then drop boot noise."""
	deadline = time.monotonic() + timeout_s
	while time.monotonic() < deadline:
		line = ser.readline().decode("ascii", errors="ignore").strip()
		if line.startswith("READY"):
			ser.reset_input_buffer()
			return line
	return None


def send_and_wait(ser, payload: str, timeout_s: float = REPLY_TIMEOUT_S, on_line: Optional[LineHandler] = None) -> str:
	"""Write one frame and return the firmware's reply ("ACK,..."/"NACK,...") or "" on timeout.

	A "READY..." line means the Nano rebooted (brown-out or reset) and is returned as the reply.
	Unsolicited lines (EVT,..., T,... telemetry) are passed to ``on_line``.
	Runs in a worker thread, so reads never overlap between commands.
	"""
	ser.write(payload.encode("ascii", errors="ignore"))
	deadline = time.monotonic() + timeout_s
	while time.monotonic() < deadline:
		line = ser.readline().decode("ascii", errors="ignore").strip()
		if line.startswith(("ACK", "NACK", "READY")):
			return line
		if line and on_line is not None:
			on_line(line)
	return ""


async def _sim_serial(command_queue: asyncio.Queue) -> None:
	logger.info("SERIAL_PORT=sim: logging motor commands instead of sending them")
	while True:
		command = await command_queue.get()
		logger.info("SIM serial -> %s", command.strip())
		command_queue.task_done()


async def serial_task(
	command_queue: asyncio.Queue,
	port: Optional[str] = None,
	baudrate: Optional[int] = None,
	on_connect: Optional[Callable[[], None]] = None,
	on_line: Optional[LineHandler] = None,
) -> None:
	"""Send commands to the Nano, checking each reply. Reconnects forever on errors.

	``on_connect`` runs after every (re)connect or detected Nano reset (the Nano boots with its
	servos off). ``on_line`` receives every line that isn't a plain ACK: events, telemetry,
	NACKs and READY banners. Both run on the event loop thread.
	"""
	port = port or config.SERIAL_PORT
	baudrate = baudrate or config.SERIAL_BAUD
	if port == "sim":
		await _sim_serial(command_queue)
		return

	loop = asyncio.get_running_loop()

	def forward(line: str) -> None:  # called from the worker thread
		if on_line is not None:
			loop.call_soon_threadsafe(on_line, line)

	while True:
		ser: serial.Serial | None = None
		try:
			# serial_for_url accepts device paths and URLs such as socket://127.0.0.1:7777 (tools/sim_nano.py)
			ser = await asyncio.to_thread(
				serial.serial_for_url, port, baudrate=baudrate, timeout=0.05, write_timeout=0.5)
			banner = await asyncio.to_thread(wait_for_ready, ser)
			if banner is None:
				logger.warning("No READY from %s within %.0fs; continuing anyway", port, READY_TIMEOUT_S)
			else:
				logger.info("Nano ready: %s", banner)
				if banner == "READY,NOIMU":
					logger.warning("Nano reports no MPU6050: standing/walking without balance")
			logger.info("Serial connected: %s @ %s", port, baudrate)
			if on_connect:
				on_connect()

			while True:
				try:
					command = await asyncio.wait_for(command_queue.get(), timeout=IDLE_POLL_S)
					from_queue = True
				except asyncio.TimeoutError:
					command, from_queue = "P", False
				try:
					payload = command if command.endswith("\n") else f"{command}\n"
					reply = await asyncio.to_thread(send_and_wait, ser, payload, REPLY_TIMEOUT_S, forward)
					if reply.startswith("READY"):
						logger.warning("Nano reset detected (%s); standing up again", reply)
						if on_connect:
							on_connect()
					elif reply.startswith("NACK"):
						if on_line is not None:
							on_line(reply)
						if reply != "NACK,ESTOP":
							logger.warning("Command %r refused: %s", payload.strip(), reply)
					elif not reply:
						logger.warning("Command %r got no reply (timeout)", payload.strip())
				finally:
					if from_queue:
						command_queue.task_done()

		except asyncio.CancelledError:
			logger.info("serial_task cancelled; shutting down serial loop")
			raise
		except (SerialException, OSError) as exc:
			logger.warning("Serial unavailable/disconnected on %s: %s", port, exc)
			await asyncio.sleep(1.0)
		finally:
			if ser is not None:
				with contextlib.suppress(Exception):
					await asyncio.to_thread(ser.close)
