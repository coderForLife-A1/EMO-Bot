"""Simulated controller: runs the real firmware code against a simulated biped + MPU6050, served as a TCP serial port.

    python tools/sim_nano.py                     # ESP32 firmware; needs g++ (build-essential / MinGW / Xcode CLT)
    python tools/sim_nano.py --firmware nano     # Arduino Nano firmware
    SERIAL_PORT=socket://127.0.0.1:7777 python main.py

Type into this console while it runs:
    tilt <deg> [deg/s]   tilt the ground/robot (tilt 8 = slope, tilt 70 200 = knock it over, tilt 0 = upright)
    state                print the simulated body state
    quit

The firmware is compiled from firmware/emo_<firmware>/emo_<firmware>.ino with the stub Arduino headers in
tests/firmware. Time runs in real time (10 ms steps). Each new connection is a power-on, like the board
resetting when its USB port opens.
"""
import argparse
import queue
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS_DIR = ROOT / "tests" / "firmware"
STEP_MS = 10


FIRMWARES = ("esp32", "nano")


def build(firmware: str) -> Path:
    if shutil.which("g++") is None:
        sys.exit("g++ not found: install build-essential (Linux), MinGW (Windows) or Xcode CLT (macOS)")
    sketch = ROOT / "firmware" / f"emo_{firmware}" / f"emo_{firmware}.ino"
    exe = Path(tempfile.gettempdir()) / (f"emo_sim_{firmware}" + (".exe" if sys.platform == "win32" else ""))
    subprocess.run(["g++", "-std=c++11", "-O1", f'-DFIRMWARE_SKETCH="{sketch.as_posix()}"',
                    "-I", str(HARNESS_DIR), str(HARNESS_DIR / "harness.cpp"), "-o", str(exe)], check=True)
    return exe


class Simulator:
    def __init__(self, exe: Path):
        self.proc = subprocess.Popen([str(exe), "serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True, bufsize=1)

    def send(self, line: str) -> str:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        out = []
        for reply in self.proc.stdout:
            if reply == "@@\n":
                break
            out.append(reply)
        return "".join(out)

    def close(self) -> None:
        self.proc.kill()


def console(commands: "queue.Queue[str]") -> None:
    for raw in sys.stdin:
        words = raw.split()
        if not words:
            continue
        if words[0] == "tilt" and len(words) >= 2:
            commands.put(f"@tilt {words[1]} {words[2] if len(words) > 2 else 40}")
        elif words[0] == "state":
            commands.put("@state")
        elif words[0] in ("quit", "exit"):
            commands.put("quit")
            return
        else:
            print("commands: tilt <deg> [deg/s] | state | quit")


def serve_client(conn: socket.socket, sim: Simulator, commands: "queue.Queue[str]") -> bool:
    """Returns False when the user asked to quit."""
    conn.sendall(sim.send("@boot").encode())
    buf = b""
    next_step = time.monotonic()
    while True:
        while not commands.empty():
            cmd = commands.get()
            if cmd == "quit":
                return False
            out = sim.send(cmd)
            if cmd == "@state":
                print(out, end="", flush=True)
            else:
                conn.sendall(out.encode())

        timeout = max(0.0, next_step - time.monotonic())
        readable, _, _ = select.select([conn], [], [], timeout)
        if readable:
            data = conn.recv(4096)
            if not data:
                print("client disconnected", flush=True)
                return True
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                conn.sendall(sim.send(line.decode("ascii", errors="ignore").strip()).encode())

        now = time.monotonic()
        if now >= next_step:
            conn.sendall(sim.send(f"@t {STEP_MS}").encode())
            next_step += STEP_MS / 1000
            if now - next_step > 0.5:  # fell far behind (e.g. debugger pause): don't fast-forward
                next_step = now


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument("--firmware", choices=FIRMWARES, default="esp32")
    args = parser.parse_args()

    exe = build(args.firmware)
    commands: "queue.Queue[str]" = queue.Queue()
    threading.Thread(target=console, args=(commands,), daemon=True).start()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.host, args.port))
        server.listen(1)
        print(f"Simulated {args.firmware} on socket://{args.host}:{args.port} (Ctrl+C or 'quit' to stop)", flush=True)
        keep_going = True
        while keep_going:
            conn, addr = server.accept()
            print(f"client connected from {addr[0]}:{addr[1]}; powering on", flush=True)
            sim = Simulator(exe)
            try:
                with conn:
                    keep_going = serve_client(conn, sim, commands)
            except (ConnectionError, OSError) as exc:
                print(f"connection closed: {exc}", flush=True)
            finally:
                sim.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
