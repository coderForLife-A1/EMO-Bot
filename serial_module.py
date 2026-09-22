"""Serial link between the Pi and the Arduino Nano.

Opens the port (device path, or a socket:// URL for tools/sim_nano.py), waits for the firmware's
READY banner, sends one command at a time and matches the ACK/NACK reply to it by command letter,
forwards every line (replies, events, telemetry) to a handler, pings when idle, parks the servos on
shutdown, and reconnects forever. SERIAL_PORT=sim logs commands and answers with fake ACKs.
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
# Commands that take longer on the Nano: C samples the IMU for ~0.35 s; S may re-initialise a lost IMU.
SLOW_REPLY_TIMEOUT_S = {"C": 1.5, "S": 1.0}
IDLE_POLL_S = 0.5  # ping when idle so events (EVT,FALLEN) are read promptly
PARK_COMMANDS = ("W,0,0", "O")  # sent on shutdown: stop walking, then switch the servos off

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


def offer_urgent(queue: asyncio.Queue, item) -> None:
	"""Discard everything still waiting and queue ``item`` next (used for the E-stop).

	The E-stop then waits for at most the one command already in flight, and can't be dropped.
	"""
	while True:
		try:
			queue.get_nowait()
			queue.task_done()
		except asyncio.QueueEmpty:
			break
	queue.put_nowait(item)


def reply_timeout(command: str) -> float:
	return SLOW_REPLY_TIMEOUT_S.get(command[:1], REPLY_TIMEOUT_S)


def is_reply_to(command: str, line: str) -> bool:
	"""True if ``line`` is the firmware's ACK or NACK for ``command``.

	ACK,<letter>[,...] for letter commands, ACK,<joint>,... for J; NACK,<letter>,<reason> for all.
	"""
	letter = command[:1]
	if line.startswith("NACK,"):
		return line[5:7] == f"{letter},"
	if line.startswith("ACK,"):
		rest = line[4:]
		if letter == "J":
			return rest[:1].isdigit()
		return rest == letter or rest.startswith(f"{letter},")
	return False


def wait_for_ready(ser, timeout_s: float = READY_TIMEOUT_S) -> Optional[str]:
	"""Wait for the firmware's READY banner ("READY,IMU" / "READY,NOIMU"), then drop boot noise."""
	deadline = time.monotonic() + timeout_s
	while time.monotonic() < deadline:
		line = ser.readline().decode("ascii", errors="ignore").strip()
		if line.startswith("READY"):
			ser.reset_input_buffer()
			return line
	return None


def send_and_wait(ser, payload: str, timeout_s: Optional[float] = None, on_line: Optional[LineHandler] = None) -> str:
	"""Write one command and return its reply (ACK/NACK), a "READY..." banner if the Nano rebooted,
	or "" on timeout.

	Every line read, including the reply, is passed to ``on_line``. A late reply to an earlier
	command is recognised by its command letter and skipped instead of being taken for this
	command's reply. Runs in a worker thread, so reads never overlap between commands.
	"""
	command = payload.strip()
	timeout_s = reply_timeout(command) if timeout_s is None else timeout_s
	ser.write(payload.encode("ascii", errors="ignore"))
	deadline = time.monotonic() + timeout_s
	while time.monotonic() < deadline:
		line = ser.readline().decode("ascii", errors="ignore").strip()
		if not line:
			continue
		if on_line is not None:
			on_line(line)
		if line.startswith("READY") or is_reply_to(command, line):
			return line
		if line.startswith(("ACK", "NACK")):
			logger.debug("Skipping late reply %r while waiting for %r", line, command)
	return ""


def sim_reply(command: str) -> str:
	"""The ACK the real firmware would send (used by SERIAL_PORT=sim)."""
	fields = command.split(",")
	if fields[0] == "J" and len(fields) == 3:
		return f"ACK,{fields[1]},{fields[2]},{fields[2]}"
	return "ACK," + command


async def _sim_serial(command_queue: asyncio.Queue, on_line: Optional[LineHandler]) -> None:
	logger.info("SERIAL_PORT=sim: logging motor commands instead of sending them")
	try:
		while True:
			command = await command_queue.get()
			logger.info("SIM serial -> %s", command.strip())
			if on_line is not None:
				on_line(sim_reply(command.strip()))
			command_queue.task_done()
	except asyncio.CancelledError:
		logger.info("SIM serial: parking servos (%s)", ", ".join(PARK_COMMANDS))
		raise


def park(ser, on_line: Optional[LineHandler] = None) -> None:
	"""Stop walking and switch the servos off before the port closes (shutdown)."""
	for command in PARK_COMMANDS:
		send_and_wait(ser, f"{command}\n", REPLY_TIMEOUT_S, on_line)


async def serial_task(
	command_queue: asyncio.Queue,
	port: Optional[str] = None,
	baudrate: Optional[int] = None,
	on_connect: Optional[Callable[[], None]] = None,
	on_line: Optional[LineHandler] = None,
) -> None:
	"""Send commands to the Nano, checking each reply. Reconnects forever on errors.

	``on_connect`` runs after every (re)connect. ``on_line`` receives every line from the Nano:
	replies (ACK/NACK), events (EVT,...), telemetry (T,...) and READY banners. Both run on the
	event loop thread. On cancellation (shutdown) the servos are parked before the port closes.
	"""
	port = port or config.SERIAL_PORT
	baudrate = baudrate or config.SERIAL_BAUD
	if port == "sim":
		await _sim_serial(command_queue, on_line)
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
				if on_line is not None:
					on_line(banner)
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
					reply = await asyncio.to_thread(send_and_wait, ser, payload, None, forward)
					if reply.startswith("READY"):
						logger.warning("Nano reset detected (%s); standing up again", reply)
						if on_connect:
							on_connect()
					elif reply.startswith("NACK") and ",ESTOP" not in reply:
						logger.warning("Command %r refused: %s", payload.strip(), reply)
					elif not reply:
						logger.warning("Command %r got no reply (timeout)", payload.strip())
				finally:
					if from_queue:
						command_queue.task_done()

		except asyncio.CancelledError:
			if ser is not None and ser.is_open:
				logger.info("Parking servos before shutdown (%s)", ", ".join(PARK_COMMANDS))
				with contextlib.suppress(Exception):
					await asyncio.to_thread(park, ser, forward)
			logger.info("serial_task cancelled; shutting down serial loop")
			raise
		except (SerialException, OSError) as exc:
			logger.warning("Serial unavailable/disconnected on %s: %s", port, exc)
			await asyncio.sleep(1.0)
		finally:
			if ser is not None:
				with contextlib.suppress(Exception):
					await asyncio.to_thread(ser.close)
