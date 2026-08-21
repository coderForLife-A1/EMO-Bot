# Personal Desk Robot

## Project Overview

Personal Desk Robot is a six-layer robotic system in which a Raspberry Pi 5 provides high-level compute and an Arduino Nano provides deterministic actuator control. The Pi runs Python `asyncio` tasks, a local MQTT broker, OpenCV/MediaPipe vision, Porcupine wake-word detection, Whisper transcription, GPT-4o routing, and ElevenLabs text-to-speech; the Nano receives newline-delimited UART joint commands and updates a PCA9685 servo controller over I2C. The event loop must remain non-blocking: blocking device APIs and subprocesses belong in worker threads or asynchronous subprocess interfaces, and runtime modules exchange data only through MQTT or `asyncio.Queue`.

## Architecture

1. **Physical**
	- 16 servo actuators, mechanical joints, external servo power, PCA9685 PWM expansion, desk enclosure, and audio/visual peripherals.
2. **Sensing**
	- ReSpeaker HAT audio input on ALSA `plughw:0`.
	- Camera on V4L2 `/dev/video0` for face tracking and posture estimation.
	- VL53L0X time-of-flight distance sensing over I2C.
3. **Compute**
	- Raspberry Pi 5: Linux host for asynchronous orchestration, networking, camera, audio, and AI calls.
	- Arduino Nano: fixed-rate servo command parser and PCA9685 controller.
4. **Software**
	- Python `asyncio`, local Mosquitto MQTT, `asyncio.Queue`, `httpx`, PySerial, OpenCV, MediaPipe, and the API service under `api_server/`.
	- Arduino firmware uses Wire, Adafruit PWM Servo Driver, and non-blocking serial line buffering.
5. **Intelligence**
	- `behavior_tree_module.py` runs a 10 Hz priority tree for E-stop, conversation, posture correction, face tracking, and idle motion.
	- Audio routing is wake word -> 5-second WAV -> Whisper -> GPT-4o -> ElevenLabs -> playback.
6. **Interaction**
	- Servo motion, audio playback, MQTT state topics, camera-derived tracking, posture alerts, and the GC9A01 TFT user display.

## Hardware BOM (Bill of Materials)

| Component | Function | Interface / electrical requirement |
| --- | --- | --- |
| Raspberry Pi 5 | SBC for asynchronous runtime and AI/network work | 5 V USB-C power; 3.3 V GPIO; Linux ALSA/V4L2 |
| Arduino Nano | MCU for servo command parsing and real-time output | USB/UART at 115200 baud; 5 V logic on typical Nano boards |
| PCA9685 | 16-channel, 12-bit PWM servo controller | I2C address `0x40`; Arduino A4/A5 in this firmware; separate servo supply |
| VL53L0X | Time-of-flight distance sensor | I2C, normally address `0x29`; 3.3 V-compatible breakout required |
| GC9A01 TFT | SPI graphical display | SPI clock/data plus chip-select, data/command, and reset GPIOs |
| ReSpeaker HAT | Microphone input and audio capture | ALSA device `plughw:0`; verify with `arecord -l` |
| CSI/USB camera | Face and posture sensing | V4L2 device `/dev/video0` |
| Servos and regulated power supply | Mechanical actuation | Do not power servo loads from the Pi or Nano 5 V rail |

## Wiring & Pinouts

The following pinout assumes the Pi owns the VL53L0X and GC9A01 buses while the Nano owns the PCA9685 bus. All grounds must be common. Confirm the exact ReSpeaker, TFT breakout, and Nano revision before energizing the system.

### Raspberry Pi 5 I2C

- Pi physical pin 1 (`3V3`) -> VL53L0X `VIN`/`VCC` only when the breakout accepts 3.3 V.
- Pi physical pin 3, GPIO2/SDA -> VL53L0X `SDA`.
- Pi physical pin 5, GPIO3/SCL -> VL53L0X `SCL`.
- Pi physical pin 6 (`GND`) -> VL53L0X `GND`.
- The PCA9685 is connected to the Nano, not directly to these Pi I2C pins: Nano A4/SDA -> PCA9685 SDA; Nano A5/SCL -> PCA9685 SCL.
- PCA9685 `V+` is the external servo voltage rail. Tie its logic ground to the Nano ground and do not use the Pi 5 V rail for servo power.

### Raspberry Pi 5 SPI / GC9A01

- Pi physical pin 19, GPIO10/MOSI -> GC9A01 `SDA`/`MOSI`.
- Pi physical pin 23, GPIO11/SCLK -> GC9A01 `SCL`/`SCK`.
- Pi physical pin 24, GPIO8/CE0 -> GC9A01 `CS`.
- A free Pi 3.3 V GPIO -> GC9A01 `DC`.
- A second free Pi 3.3 V GPIO -> GC9A01 `RST`.
- Pi physical pin 17 (`3V3`) -> GC9A01 `VCC`; Pi physical pin 9 (`GND`) -> `GND`.
- GC9A01 MISO is normally unused for write-only display operation. Do not drive its logic pins at 5 V.

### Pi-to-Nano UART

- Pi physical pin 8, GPIO14/TXD -> Nano UART `RX` through a bidirectional level shifter or a verified 3.3 V-safe input.
- Nano UART `TX` -> Pi physical pin 10, GPIO15/RXD through a 5 V-to-3.3 V level shifter. Never connect a typical 5 V Nano TX directly to Pi GPIO15.
- Pi physical pin 6 (`GND`) -> Nano `GND`.
- Firmware framing is ASCII `J,<joint>,<angle>\n`; baud rate is `115200`, with joint indexes `0` through `15` and software limits of `10` through `170` degrees.

### Arduino Nano / PCA9685

- Nano A4/SDA -> PCA9685 `SDA`.
- Nano A5/SCL -> PCA9685 `SCL`.
- Nano `5V` or the PCA9685 logic supply -> PCA9685 `VCC`, according to the board's logic-voltage specification.
- Nano `GND` -> PCA9685 `GND`.
- External regulated servo supply -> PCA9685 `V+` and servo power ground.
- PCA9685 channels `0` through `15` map one-to-one to logical joints `0` through `15`; PWM frequency is 50 Hz and I2C address is `0x40`.

## Software Setup

Commands below target Raspberry Pi OS/Debian. Run hardware commands with the required privileges.

1. Enable I2C, SPI, serial, and camera interfaces with `sudo raspi-config`, then reboot. Disable the serial login shell while leaving the UART enabled.
2. Install system packages and Mosquitto:

	```bash
	sudo apt update
	sudo apt install -y mosquitto mosquitto-clients python3-venv python3-dev \
		 build-essential portaudio19-dev libsndfile1 alsa-utils v4l-utils
	sudo systemctl enable --now mosquitto
	```

3. Create the Python environment and install the repository dependencies:

	```bash
	cd ~/EMO-Bot
	python3 -m venv .venv
	source .venv/bin/activate
	python -m pip install --upgrade pip
	python -m pip install -r requirements.txt
	python -m pip install paho-mqtt pyserial opencv-python mediapipe py-trees \
		 pvporcupine sounddevice
	```

	The second install includes runtime packages imported by the robot scripts but not currently listed in `requirements.txt`.

4. Verify devices and buses before starting the robot:

	```bash
	arecord -l
	v4l2-ctl --list-devices
	i2cdetect -y 1
	```

5. Install Arduino CLI and flash the Nano. Select the correct port and Nano bootloader variant for the board:

	```bash
	arduino-cli core update-index
	arduino-cli core install arduino:avr
	arduino-cli board list
	arduino-cli compile --fqbn arduino:avr:nano arduino_nano_pca9685_firmware.ino
	arduino-cli upload -p /dev/ttyUSB0 --fqbn arduino:avr:nano arduino_nano_pca9685_firmware.ino
	```

	For a classic Nano that fails upload, retry with the board's old bootloader profile: `arduino:avr:nano:cpu=atmega328old`.

## Environment Variables

Create `.env` in the repository root. The API server loads it through `python-dotenv`; standalone runtime modules read process environment variables, so export or source the values before launching them.

```dotenv
OPENAI_API_KEY=replace_with_openai_key
ANTHROPIC_API_KEY=replace_with_anthropic_key
ELEVENLABS_API_KEY=replace_with_elevenlabs_key
PORCUPINE_ACCESS_KEY=replace_with_picovoice_key
PORCUPINE_KEYWORD_PATH=
ELEVENLABS_VOICE_ID=EXAVITQu4vr4xnSDxMaL
OPENAI_BASE_URL=https://api.openai.com/v1
WHISPER_MODEL=whisper-1
CHAT_MODEL=gpt-4o
API_TIMEOUT_SECONDS=15
```

`OPENAI_API_KEY`, `ELEVENLABS_API_KEY`, and `PORCUPINE_ACCESS_KEY` are checked by the API server health endpoint. `ANTHROPIC_API_KEY` is reserved configuration; no current module calls Anthropic. Never commit `.env` or API keys.

## Running the System

From the Pi, use separate terminals or a process supervisor. Start infrastructure before device consumers:

```bash
cd ~/EMO-Bot
source .venv/bin/activate

sudo systemctl start mosquitto
mosquitto_sub -h 127.0.0.1 -t 'robot/#' -v
```

In a second terminal, verify the Nano responds before starting the runtime:

```bash
stty -F /dev/ttyUSB0 115200 cs8 -cstopb -parenb
printf 'J,0,90\n' > /dev/ttyUSB0
```

In a third terminal, start the asynchronous robot process:

```bash
cd ~/EMO-Bot
source .venv/bin/activate
python main.py
```

The current `main.py` starts MQTT, opens `/dev/ttyUSB0`, and launches sensor, vision, intelligence, audio placeholder, serial, and MQTT-router tasks. The standalone `audio_trigger_task.py` and `api_routing_task.py` are isolated tasks and are not yet wired into `main.py`; integrate them into the application lifecycle before expecting wake-word-to-TTS operation.

The optional FastAPI service runs independently:

```bash
uvicorn api_server.main:app --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/health
```

## Troubleshooting

1. **Servo jitter or voltage drop**
	- Cause: servo current transients, inadequate regulator capacity, shared logic/servo supply noise, or missing common ground.
	- Fix: power servos from a separate regulated supply sized for stall current, connect grounds at a controlled common point, add bulk decoupling near the PCA9685/servo rail, and keep the Pi/Nano logic rail out of `V+`. Confirm the PCA9685 remains at 50 Hz and reduce mechanical load before widening software limits.

2. **I2C address conflicts or missing devices**
	- Cause: duplicate sensor addresses, incorrect pull-ups/voltage levels, disabled Pi I2C, or the PCA9685 connected to the wrong controller bus.
	- Fix: run `i2cdetect -y 1`; the Pi-side VL53L0X should normally appear at `0x29`, while the firmware expects the PCA9685 at `0x40` on the Nano's A4/A5 bus. Use XSHUT or an I2C multiplexer for multiple VL53L0X units, verify SDA/SCL continuity, and never expose Pi GPIO to 5 V.

3. **API timeouts**
	- Cause: DNS/connectivity failures, invalid keys, provider latency, or the three-second end-to-end limit in `api_routing_task.py`.
	- Fix: check `curl http://127.0.0.1:8000/health`, inspect the HTTP status and printed exception, verify `.env`, test outbound HTTPS and system time, and use `network_error.wav` as the expected local fallback. Do not replace asynchronous HTTP with blocking `requests` calls in the event loop.
