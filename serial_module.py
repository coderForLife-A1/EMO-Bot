import asyncio
import contextlib
import logging

import serial
from serial import SerialException


logger = logging.getLogger(__name__)


async def serial_task(
	command_queue: asyncio.Queue,
	port: str = "/dev/ttyUSB0",
	baudrate: int = 115200,
) -> None:
	"""Continuously send motor commands over serial and wait for ACK responses.

	Commands are read from ``command_queue`` as text frames (for example ``"J,0,90\n"``),
	sent to the Arduino, and followed by an ACK wait with a 500ms timeout.
	"""
	while True:
		ser: serial.Serial | None = None
		try:
			ser = serial.Serial(port=port, baudrate=baudrate, timeout=0.1, write_timeout=0.1)
			logger.info("Serial connected: %s @ %s", port, baudrate)

			while True:
				command = await command_queue.get()
				try:
					payload = command if command.endswith("\n") else f"{command}\n"
					await asyncio.to_thread(ser.write, payload.encode("ascii", errors="ignore"))

					try:
						ack_bytes = await asyncio.wait_for(asyncio.to_thread(ser.readline), timeout=0.5)
						ack = ack_bytes.decode("utf-8", errors="ignore").strip()
						if ack != "ACK":
							logger.warning("Unexpected serial response: %r", ack)
					except asyncio.TimeoutError:
						logger.warning("ACK timeout after sending command: %r", payload.strip())

				finally:
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
