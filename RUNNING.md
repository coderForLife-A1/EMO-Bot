# Running EMO-Bot

This guide covers everything from a laptop dry run with no hardware to a Raspberry Pi biped that
starts on boot. Work through it in order the first time. Each section checks one layer before the
next one depends on it.

1. [How the pieces fit together](#1-how-the-pieces-fit-together)
2. [Quick start on a laptop (no hardware)](#2-quick-start-on-a-laptop-no-hardware)
3. [Tests and checks](#3-tests-and-checks)
4. [Flash the controller (ESP32 or Nano)](#4-flash-the-controller-esp32-or-nano)
5. [First power-up of the legs](#5-first-power-up-of-the-legs)
6. [Set up the Raspberry Pi](#6-set-up-the-raspberry-pi)
7. [Configure `.env`](#7-configure-env)
8. [Check each subsystem on its own](#8-check-each-subsystem-on-its-own)
9. [Run the whole robot](#9-run-the-whole-robot)
10. [Start on boot (systemd)](#10-start-on-boot-systemd)
11. [Tuning the balance PID and gait](#11-tuning-the-balance-pid-and-gait)
12. [Troubleshooting](#12-troubleshooting)
13. [Secure MQTT](#13-secure-mqtt)

---

## 1. How the pieces fit together

EMO-Bot is a two-legged robot with four servos:

| PCA9685 channel | Joint | Logical angle |
| --- | --- | --- |
| 0 | Left hip (pelvis) | + = thigh swings forward |
| 1 | Right hip (pelvis) | + = thigh swings forward |
| 2 | Left knee | + = knee bends (foot moves back) |
| 3 | Right knee | + = knee bends |

An MPU6050 on the pelvis measures torso pitch. Control is split in two:

- **ESP32 (100 Hz, `firmware/emo_esp32/emo_esp32.ino`)** does the fast, safety-critical work (the
  Nano sketch `firmware/emo_nano/emo_nano.ino` is kept as an alternative, same protocol):
  - reads the IMU through a complementary filter;
  - runs a **PID on torso pitch** that offsets both hips to keep the torso level;
  - generates the walking gait and slew-limits every joint;
  - switches every servo off if the robot falls past 45°, and stops walking if the Pi goes quiet for 1 s.
- **Raspberry Pi (`main.py`)** decides *what* to do and sends high-level commands (stand, walk, gesture).

What this mechanism can do: with hip and knee pitch only (no ankles or hip-roll), the legs can only
move forward and backward. The feet are flat and rigidly fixed to the shins, so the robot stands on
its own. The balance PID keeps the **torso level** against slopes, pushes and the rocking of the gait.
It can't shift weight sideways, so side-to-side stability comes from wide feet, walking is a short
shuffle, and turning uses different stride lengths per leg.

`main.py` runs everything in one asyncio process:

| Task | Module | Critical? | What it does |
| --- | --- | --- | --- |
| `serial_task` | `serial_module.py` | yes | Sends commands to the controller, matches each reply to its command, gives the E-stop priority, forwards events/telemetry, parks the servos on shutdown, reconnects forever. |
| `behavior_tree_task` | `behavior_tree_module.py` | yes | 10 Hz priority tree: E-stop > IMU fault > fallen > rest > conversation > posture reminder > walk > stand. |
| `vision_task` | `vision_posture_module.py` | no | Camera → MediaPipe pose → posture events; reopens the camera if it drops out (face detection optional). |
| `audio_trigger_task` | `audio_trigger_task.py` | no | Porcupine wake word → records until you stop talking (max 8 s; nothing sent if you don't speak within 3 s) → hands the WAV to the API task. |
| `api_routing_task` | `api_routing_task.py` | no | Whisper → GPT → ElevenLabs → `aplay`, plus spoken cues ("I fell over"). |

If a **critical** task crashes, the robot shuts down. If an **optional** task crashes (no camera,
no mic, bad API key), the error is logged and everything else keeps running.

### MQTT topics (Mosquitto on `127.0.0.1:1883`)

| Topic | Direction | Payload |
| --- | --- | --- |
| `robot/locomotion/cmd` | → robot | `stand`, `rest`, `stop`, `walk,<speed>,<turn>[,<seconds>]`, `gesture`, `telemetry,<0\|1>`, `gains,<kp>,<ki>,<kd>`, `calibrate`, `distance` |
| `robot/locomotion/event` | robot → | `EVT,FALLEN`, `EVT,WATCHDOG`, `EVT,IMU_FAIL`, `NACK,<cmd>,<reason>` and `READY,...` from the controller |
| `robot/locomotion/telemetry` | robot → | `T,<pitch x10>,<pitch rate x10>,<hip correction x10>,<mode>` at 20 Hz when enabled. Mode: `B` balancing, `O` off, `M` manual, `F` fallen |
| `robot/sensor/distance` | robot → | VL53L0X distance in mm, `-1` = nothing in range, once per `distance` command (ESP32 only) |
| `robot/error` | → robot | `error` = E-stop (all servos off), `clear` = release |
| `robot/state` | vision → | `POSTURE_POOR` / `POSTURE_OK`: knee-bob gesture and a spoken reminder |
| `robot/audio/wake_flag` | audio → | `1` during a conversation (robot stands still), then `0` |
| `robot/audio/state`, `robot/audio/intent` | robot → | Informational |
| `robot/vision/state` | vision → | `UP` / `DOWN` when the camera starts or stops delivering frames (retained) |
| `robot/vision/face_error` | vision → | Face offset in pixels, only with `FACE_DETECTION=1` (not used by the legs yet) |

`walk` speed and turn are -100..100. Positive speed walks forward, positive turn turns right
(the left leg takes longer strides; with speed 0 the legs stride in opposite directions). A walk lasts the given number of seconds (default 2, max 10, so the robot
never wanders off the desk on its own), then it stands.

### Controller serial protocol (115200 baud, one command per line)

| Send | Reply | Meaning |
| --- | --- | --- |
| *(on boot)* | `READY,IMU` / `READY,NOIMU` | Firmware started; says whether the MPU6050 answered |
| `S` | `ACK,S` (`ACK,S,NOIMU`) | Stand in the crouched stance and balance. Refused with `NACK,S,TILTED` when lying down. If the IMU failed earlier, it is re-initialised first (`NACK,S,NOIMU` if it still doesn't answer) |
| `W,<speed>,<turn>` | `ACK,W,<speed>,<turn>` | Walk (-100..100). Must be re-sent at least every second or the controller stops (`EVT,WATCHDOG`). Refused with `NACK,W,NOIMU` after an IMU failure |
| `G,1` | `ACK,G,1` | Knee-bob gesture |
| `O` | `ACK,O` | Relax: servos off (not latched) |
| `C` | `ACK,C,<offset x100>` | Calibrate level (robot held upright and still, not balancing); saved to EEPROM |
| `K,<kp>,<ki>,<kd>` | `ACK,K,...` | Balance gains x100; saved to EEPROM |
| `T,<0\|1>` | `ACK,T,<0\|1>` | Telemetry off/on |
| `J,<joint>,<angle>` | `ACK,<joint>,<requested>,<applied>` | Raw servo angle (setup only; leaves balance mode) |
| `E` / `R` | `ACK,E` / `ACK,R` | Emergency stop (all off, latched) / release |
| `P` | `ACK,P` | Ping |
| `I` | `ACK,I` / `NACK,I,NOIMU` | Retry the IMU without moving a servo (re-initialises it if it was missing). `NACK,I,MODE` while balancing: the gyro bias needs the robot still. The Pi uses it to recover from an IMU fault |
| `D` | `ACK,D,<mm>` / `NACK,D,NOTOF` | ESP32 only: latest VL53L0X distance, `-1` = nothing in range (~2 m). A missing sensor is re-initialised first, but only while not balancing. The Nano answers `NACK,D,CMD` |
| *(unsolicited)* | `EVT,FALLEN`, `EVT,WATCHDOG`, `EVT,IMU_FAIL`, `T,...` | Events and telemetry. On `EVT,IMU_FAIL` the controller stops walking; the Pi relaxes the servos |
| rejected | `NACK,<cmd>,<reason>` | `<cmd>` is the command letter being answered (`?` for an overflowed line). `<reason>`: `FORMAT`, `CMD`, `PARSE`, `JOINT`, `ESTOP`, `MODE`, `TILTED`, `NOIMU`, `NOTOF`, `OVERFLOW`. Nothing moves |

The Pi matches every reply to its command by that letter, so a late reply (e.g. calibration, which takes
~0.35 s) is never mistaken for the reply to the next command. Slow commands get longer timeouts
(`C` 1.5 s, `S`, `I` and `D` 1 s, others 0.3 s), and the E-stop skips ahead of anything already queued.

---

## 2. Quick start on a laptop (no hardware)

You need **Python 3.11** to run the robot (3.9-3.12 work; mediapipe 0.10.18 has no wheels for 3.13+).
The tests alone (`requirements-dev.txt`) work on any Python 3.9 or newer. For the
simulated controller you also need **g++** (Linux: `build-essential`, macOS: Xcode command line tools,
Windows: MinGW-w64 or MSYS2).

```bash
git clone https://github.com/coderForLife-A1/EMO-Bot.git
cd EMO-Bot
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env
```

Start an MQTT broker:

```bash
# Docker (any OS)
# localhost only: anyone who can reach the broker can drive the robot (see section 13)
docker run -d --name mosquitto -p 127.0.0.1:1883:1883 eclipse-mosquitto:2 mosquitto -c /mosquitto-no-auth.conf
# or Debian/Ubuntu: sudo apt install mosquitto mosquitto-clients
# or macOS: brew install mosquitto && brew services start mosquitto
```

**Option A: simulated controller (recommended).** This runs the real firmware code against a simulated
biped with a simulated MPU6050, and exposes it as a serial port:

```bash
python tools/sim_nano.py                  # terminal 1: compiles the firmware for your PC and serves it
```

```dotenv
# .env
SERIAL_PORT=socket://127.0.0.1:7777
ENABLE_AUDIO=0
ENABLE_VISION=0
```

```bash
python main.py                            # terminal 2
```

**Option B: no g++.** Set `SERIAL_PORT=sim` to log commands instead of sending them.

Drive it from a third terminal (`mosquitto_pub`/`mosquitto_sub` come with Mosquitto; MQTT Explorer also works):

```bash
mosquitto_sub -t 'robot/#' -v                                  # watch everything
mosquitto_pub -t robot/locomotion/cmd -m telemetry,1           # pitch/correction stream at 20 Hz
mosquitto_pub -t robot/locomotion/cmd -m walk,70,0,3           # walk forward for 3 s
mosquitto_pub -t robot/locomotion/cmd -m walk,0,80,2           # turn right in place (-80 = left)
mosquitto_pub -t robot/locomotion/cmd -m gesture               # knee bob
mosquitto_pub -t robot/error -m error                          # E-stop: all servos off
mosquitto_pub -t robot/error -m clear                          # release: stands again
```

> If you secured the broker with a login (section 13), add `-u <user> -P <password>` to every `mosquitto_pub` / `mosquitto_sub` command.

In the `sim_nano.py` terminal, type `tilt 8` to put the robot on an 8° slope (watch the correction
in telemetry), `tilt 70 200` to knock it over (`EVT,FALLEN`, servos off), then `tilt 0` and
`mosquitto_pub -t robot/locomotion/cmd -m stand` to stand it back up. Type `state` to see the
simulated joint angles.

---

## 3. Tests and checks

The test suite needs no hardware, broker or API keys:

```bash
python -m pip install -r requirements-dev.txt
ruff check .
pytest
```

| File | Covers |
| --- | --- |
| `tests/test_firmware.py` | Compiles both firmwares (ESP32, Nano) for your PC and runs 12 scenarios on each against a simulated biped + IMU: protocol, IMU sign, slope rejection, walking, watchdog, fall detection, wrong-sign safety, calibration + EEPROM, no-IMU fallback, IMU failure while walking + recovery, telemetry, ToF distance + ToF failure. Skipped without `g++`. |
| `tests/test_behavior_tree.py` | Stand/walk/stop, walk heartbeat and time limit, E-stop latch/release, fall handling, rest, calibration, tuning pass-through, conversation and posture priorities |
| `tests/test_serial.py` | ACK/NACK parsing, READY banner, event/telemetry forwarding, drop-oldest queueing, sim mode |
| `tests/test_vision.py` | Posture rules, personal baseline, alert hysteresis |
| `tests/test_api_routing.py` | Whisper → GPT → ElevenLabs with a mocked HTTP server, timeouts, fallback |
| `tests/test_main.py` | The whole runtime starts in sim mode and stands the legs up |

Run one firmware scenario by hand and see its output:

```bash
g++ -std=c++11 -I tests/firmware tests/firmware/harness.cpp -o fw_harness
./fw_harness list          # scenario names
./fw_harness slope         # e.g. "torso back within 1.5 deg of upright 1.5 s after an 8 deg slope"
./fw_harness all
```

Compile for the real board with `arduino-cli compile --fqbn esp32:esp32:esp32 firmware/emo_esp32`
(section 4). Run these checks yourself before pushing changes.

---

## 4. Flash the controller (ESP32 or Nano)

Wiring (full details in [README.md](README.md#wiring--pinouts)): the PCA9685 **and** the MPU6050
both connect to the controller's I2C bus (ESP32: GPIO21 = SDA, GPIO22 = SCL; Nano: A4 = SDA, A5 = SCL; shared).
On the ESP32 the VL53L0X time-of-flight sensor joins the same bus (`VIN` 3V3, `GND`, `SDA` GPIO21, `SCL` GPIO22). Leg servos go on PCA9685 channels
0-3, powered from the separate servo supply.

```bash
# Install arduino-cli (Linux/Pi; Windows/macOS: https://arduino.github.io/arduino-cli/latest/installation/)
curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR=$HOME/.local/bin sh
export PATH=$HOME/.local/bin:$PATH

ESP32_URL=https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core update-index --additional-urls $ESP32_URL
arduino-cli core install esp32:esp32 --additional-urls $ESP32_URL
arduino-cli lib install "Adafruit PWM Servo Driver Library"
arduino-cli lib install VL53L0X                          # Pololu's library, for the ToF sensor (ESP32)

arduino-cli board list                                  # find the port, usually /dev/ttyUSB0
arduino-cli compile --fqbn esp32:esp32:esp32 firmware/emo_esp32
arduino-cli upload  --fqbn esp32:esp32:esp32 -p /dev/ttyUSB0 firmware/emo_esp32
```

- Verified: compiles with no warnings on `esp32:esp32` core 3.3.11 (about 24% flash, 7% RAM), USB and UART links
  (checked before the VL53L0X support was added).
- No VL53L0X fitted: set `#define TOF_ENABLED 0` to build without the Pololu library (`D` then answers `NACK,D,NOTOF`).
- Pi GPIO UART instead of USB: set `#define LINK_UART2 1` (Serial2 on GPIO16 RX / GPIO17 TX), `SERIAL_PORT=/dev/serial0`.
- Upload fails with `Failed to connect to ESP32` / `Wrong boot mode`: hold **BOOT** while the upload starts.
- The ESP32 prints ROM boot text before `READY`; the Pi skips it.
- Some USB bridges don't reset the board when the port opens: the Pi logs `No READY ... continuing anyway` and carries on.

**Arduino Nano (alternative):**

```bash
arduino-cli core install arduino:avr
arduino-cli compile --fqbn arduino:avr:nano firmware/emo_nano
arduino-cli upload  --fqbn arduino:avr:nano -p /dev/ttyUSB0 firmware/emo_nano
```

If upload fails with `stk500_getsync` / `not in sync`, the board has the old bootloader (common on
clones): use `--fqbn arduino:avr:nano:cpu=atmega328old` for compile and upload.

Open a serial terminal and check the banner:

```bash
python -m serial.tools.miniterm /dev/ttyUSB0 115200 --eol LF
# press the board's reset (EN/RST) button -> READY,IMU     (READY,NOIMU = check MPU6050 wiring/power)
# type P -> ACK,P
# type D -> ACK,D,<mm>  (ESP32; hold a hand in front of the ToF; NACK,D,NOTOF = check VL53L0X wiring)
# Ctrl+] to exit
```

> **Keep the servo power switched off** until section 5.

---

## 5. First power-up of the legs

Do this once per build, with the robot **held in the air or on a stand** so the legs hang free.
All of it happens in the serial terminal from section 4.

**1. Find each servo's straight-leg position.** With servo power on, move one joint at a time with
raw commands and note the servo angle where the leg is straight (thigh in line with the torso,
shin in line with the thigh):

```
J,0,90      left hip  -> adjust (J,0,85, J,0,95, ...) until the thigh hangs straight down
J,1,90      right hip
J,2,90      left knee -> until the shin is in line with the thigh
J,3,90      right knee
```

**2. Enter the results in the firmware** (`firmware/emo_esp32/emo_esp32.ino`, or the Nano sketch, "legs" section):

- `LEG_TRIM_DEG[i]` = (straight-leg servo angle) - 90. For example, a left hip straight at 97 gives `LEG_TRIM_DEG[0] = 7`.
- `LEG_DIR[i]`: send `J,0,110`. If the left thigh swung **forward**, `LEG_DIR[0] = 1`, otherwise `-1`.
  For knees, `+1` if a larger angle **bends** the knee (foot moves back). Mirrored left/right servos
  usually have opposite signs (the default is `{1, -1, 1, -1}`).
- `LEG_MIN_DEG` / `LEG_MAX_DEG`: logical limits where nothing collides.

Reflash, then type `S`. The legs should go to the crouched stance (hips and knees both bent 15°),
with both feet level. Type `O` to relax.

**3. Check the IMU direction.** Type `T,1` to stream telemetry (`T,<pitch x10>,...`). Tilt the robot
forward by hand: the first number must go **positive**. If it goes negative, set `PITCH_SIGN = -1.0`
and reflash. The sensor is expected flat on the pelvis with its X arrow pointing forward; mounted
another way, swap the axes in `imuReadRaw()`.

**4. Calibrate level.** Stand the robot upright on a flat table, holding it still with the servos
relaxed (`O`), and type `C`. The offset is stored in EEPROM and survives reboots. Telemetry pitch
should now read about 0.

**5. Check the balance direction.** Put it down, type `S`, and gently tilt the table (or press on the
back of the torso). The hips should move to keep the torso **level**. If they make it worse, the
hip `LEG_DIR` signs are wrong. That case is also covered by fall detection (servos off past 45°),
and the `wrong_sign` test scenario shows it.

**6. First steps.** Type `W,40,0`, then `W,0,0` within a second (or let the 1 s watchdog stop it).
Hold a hand near the robot. Continue with section 11 to tune.

---

## 6. Set up the Raspberry Pi

Use **Raspberry Pi OS Bookworm (64-bit)**. It ships Python 3.11, which the pinned mediapipe
needs; Trixie ships Python 3.13, which has no mediapipe 0.10.18 wheel.

```bash
sudo raspi-config        # Interface Options: enable I2C, SPI, Serial Port (no login shell), then reboot

sudo apt update
sudo apt install -y git mosquitto mosquitto-clients python3-venv python3-dev \
    python3-picamera2 portaudio19-dev alsa-utils v4l-utils i2c-tools
sudo systemctl enable --now mosquitto
sudo usermod -aG dialout,video,audio,i2c "$USER"     # serial, camera, sound, I2C; log out and back in

git clone https://github.com/coderForLife-A1/EMO-Bot.git ~/EMO-Bot
cd ~/EMO-Bot
# --system-site-packages lets the venv see apt's picamera2 (it can't be pip-installed reliably)
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

The first time vision runs, MediaPipe downloads its pose model (~3 MB), so the Pi needs internet
access once.

---

## 7. Configure `.env`

`config.py` loads `.env` from the repository root before anything else starts.

| Variable | Default | Notes |
| --- | --- | --- |
| `SERIAL_PORT` | `/dev/ttyUSB0` | `/dev/serial0` for GPIO UART, `socket://127.0.0.1:7777` for `tools/sim_nano.py`, `sim` to only log |
| `SERIAL_BAUD` | `115200` | Must match the firmware |
| `OPENAI_API_KEY` | – | Whisper + chat. Without it, conversations play the fallback sound |
| `ELEVENLABS_API_KEY` | – | Text to speech |
| `PORCUPINE_ACCESS_KEY` | – | Free key at [console.picovoice.ai](https://console.picovoice.ai). Without it, wake word is disabled |
| `PORCUPINE_KEYWORD_PATH` | *(built-in "porcupine")* | Custom `.ppn` wake word (Linux aarch64 build for the Pi) |
| `ELEVENLABS_VOICE_ID` | `EXAVITQu4vr4xnSDxMaL` | Any voice ID from your ElevenLabs library |
| `CHAT_MODEL` / `WHISPER_MODEL` | `gpt-4o` / `whisper-1` | `CHAT_MODEL` is the cloud reply model: the fallback when `LOCAL_LLM_URL` is set |
| `LOCAL_LLM_URL` | empty | Laptop Ollama, e.g. `http://192.168.43.20:11434` (section 7a). Empty = cloud replies only |
| `LOCAL_LLM_MODEL` / `LOCAL_LLM_ESCALATE_MODEL` | `qwen3:4b` / `gemma4:e4b` | Second model is asked when the first is unsure; `off` = go to the cloud instead |
| `LOCAL_LLM_CONNECT_TIMEOUT` / `LOCAL_LLM_TIMEOUT` | `1` / `5` | Seconds: to connect, and longest wait for the next piece of a reply. Past either, the cloud answers |
| `LOCAL_LLM_KEEP_ALIVE` | `30m` | How long Ollama keeps the model loaded |
| `WHISPER_LANGUAGE` / `WHISPER_PROMPT` | `en` / a line naming EMO | Language (`auto` = guess) and a vocabulary hint (`off` = none) |
| `LLM_TEMPERATURE` / `LLM_MAX_TOKENS` | `0.4` / `150` | Reply tuning; a reply cut by the limit is trimmed to its last full sentence |
| `CONVERSATION_TURNS` / `CONVERSATION_MEMORY_SECONDS` | `3` / `120` | Exchanges remembered for follow-up questions (`0` = off), forgotten after that long without talking |
| `SPEECH_RMS_THRESHOLD` | `500` | Mic level that counts as speech after the wake word. Raise it if room noise keeps recordings going; lower it if "No speech after the wake word" is logged while you talk |
| `ROBOT_LOCATION` | empty | Told to the model with the date and time, e.g. `Chennai, India` |
| `LOG_CONVERSATIONS` | `0` | `1` = log what was said and the reply at INFO (they land in the journal) |
| `OPENAI_BASE_URL`, `ELEVENLABS_TTS_URL` | official endpoints | Change for a proxy / compatible API |
| `API_TIMEOUT_SECONDS` | `15` | Budget for Whisper → LLM → TTS; playback isn't counted |
| `CAMERA_SOURCE` | `/dev/video0` | `picamera2` (Pi 5 CSI camera), `/dev/video0` (USB), `0` (laptop webcam) |
| `AUDIO_INPUT_DEVICE` | system default | Mic name substring or index from `python -m sounddevice`, e.g. `seeed` |
| `AUDIO_OUTPUT_DEVICE` | ALSA default | `aplay -D` device, e.g. `plughw:0` |
| `ENABLE_VISION` / `ENABLE_AUDIO` | `1` | Set to `0` to skip a subsystem |
| `FACE_DETECTION` | `0` | Run MediaPipe face detection and publish `robot/vision/face_error`. Off by default: nothing uses it yet and it costs Pi CPU |
| `ALLOW_NO_IMU` | `0` | Let the robot stand and walk when the controller booted without an IMU (no balance, no fall detection). Bench tests only |
| `MQTT_HOST` / `MQTT_PORT` | `127.0.0.1` / empty | Empty port = 1883, or 8883 with `MQTT_TLS=1` |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | empty | Broker login (section 13) |
| `MQTT_TLS` / `MQTT_CA_CERTS` | `0` / empty | Connect with TLS; CA file for a self-signed broker certificate |

Never commit `.env` (it is in `.gitignore`).

### 7a. Laptop LLM over Wi-Fi (optional)

The reply model runs on a laptop with Ollama; the Pi streams replies from it over a shared network (for example
a phone hotspot). Speech to text stays on the cloud API.

1. Laptop: `ollama pull qwen3:4b` and `ollama pull gemma4:e4b`.
2. Laptop: `setx OLLAMA_HOST 0.0.0.0:11434`, then quit and restart Ollama so it listens on the network.
3. Laptop: set the hotspot network to **Private** in Windows, and allow TCP 11434 only from the hotspot's subnet
   (Ollama has no login: anyone who can reach the port can use it). Admin PowerShell, for a `192.168.43.x` hotspot:
   `New-NetFirewallRule -DisplayName "Ollama (robot)" -Direction Inbound -Protocol TCP -LocalPort 11434 -RemoteAddress 192.168.43.0/24 -Profile Private -Action Allow`
4. Pi: `curl http://<laptop-ip>:11434/api/tags` should list the models.
5. Pi `.env`: `LOCAL_LLM_URL=http://<laptop-ip>:11434`.
6. Pi: set the timezone (`sudo timedatectl set-timezone Asia/Kolkata`); the model is told the Pi's local time.

A phone hotspot may hand out a new IP on reconnect. If the laptop can't be reached the cloud model answers,
so nothing breaks; update `LOCAL_LLM_URL` (or use `http://<laptop-name>.local:11434` if mDNS resolves on the Pi).

---

## 8. Check each subsystem on its own

Run these on the Pi with the venv active. Keep a terminal on `mosquitto_sub -t 'robot/#' -v`.

| What | Command | Expect |
| --- | --- | --- |
| Controller + legs | `python -m serial.tools.miniterm /dev/ttyUSB0 115200 --eol LF`, then `S`, `T,1`, `W,40,0` | Section 5 behaviour |
| Behavior tree | `python behavior_tree_module.py` + `mosquitto_pub -t robot/locomotion/cmd -m walk,50,0,2` | Prints `MOTOR_CMD: S`, `W,50,0` every 0.2 s, then `W,0,0` |
| Buses and devices | `i2cdetect -y 1`, `rpicam-hello -t 3000`, `arecord -l && aplay -l` | Pi I2C devices, camera preview, sound cards |
| Speaker | `aplay assets/network_error.wav` | Three descending beeps |
| API keys + TTS | `python api_routing_task.py "Hello, I am EMO"` | Speech, or beeps and a logged reason |
| Mic + wake word | `python -m sounddevice`, then `python audio_trigger_task.py` | Say "porcupine": "Wake word detected" |
| Vision | `python vision_posture_module.py` | Slouch for 3 s → `robot/state POSTURE_POOR` |

---

## 9. Run the whole robot

```bash
cd ~/EMO-Bot && source .venv/bin/activate
python main.py
```

Healthy startup looks like:

```
INFO [serial_module] Controller ready: READY,IMU
INFO [serial_module] Serial connected: /dev/ttyUSB0 @ 115200
INFO [__main__] EMO-Bot running: serial_task, behavior_tree_task, api_routing_task, vision_task, audio_trigger_task
```

> If you secured the broker with a login (section 13), add `-u <user> -P <password>` to every `mosquitto_pub` / `mosquitto_sub` command.

What the robot does:

- **Idle**: stands in the crouched stance and keeps its torso level.
- **`walk,<speed>,<turn>,<seconds>`**: walks, then stands. The Pi re-sends the command every 0.2 s,
  so if the Pi crashes the controller stops within 1 s.
- **Wake word / conversation**: stops walking and stands still while listening and answering.
- **Slouching for 3 s**: one knee-bob and a spoken reminder, repeated every 60 s while it lasts.
- **Falls over**: all servos go limp, `EVT,FALLEN` is published, and the robot says
  "Whoops, I fell over. Could you stand me back up?". Stand it up, then send
  `mosquitto_pub -t robot/locomotion/cmd -m stand`.
- **E-stop**: `mosquitto_pub -t robot/error -m error` turns everything off; `clear` stands it up again.
- **Rest**: `mosquitto_pub -t robot/locomotion/cmd -m rest` relaxes the servos until `stand`.
- **IMU stops answering** (`EVT,IMU_FAIL`): without it there is no balance and no fall detection, so the
  robot stops walking, relaxes its servos and says so. Fix the wiring, then send `stand`: the controller
  re-initialises the IMU and stands only if it answers. A controller that *booted* without an IMU
  (`READY,NOIMU`) is treated the same way unless `ALLOW_NO_IMU=1`.
- **Camera unplugged or crashed**: vision logs it, publishes `robot/vision/state DOWN`, and keeps trying
  to reopen it (1 s, 2 s, 4 s ... up to 30 s apart). `UP` is published when frames arrive again.
- **Speech task crashes mid-conversation**: the conversation flag is cleared so the robot doesn't
  stay frozen. It also expires on its own after 30 s.

Stop with **Ctrl+C** (or `SIGTERM`). The Pi stops any walk and switches the servos off (`W,0,0`,
then `O`) before closing the serial port, so the servos don't stay powered after the program exits.

---

## 10. Start on boot (systemd)

```bash
# Edit User= and the paths in the unit file if your user isn't "pi" or the repo isn't ~/EMO-Bot
sudo cp deploy/emo-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now emo-bot

journalctl -u emo-bot -f            # live logs
sudo systemctl restart emo-bot      # after pulling new code or editing .env
sudo systemctl disable --now emo-bot
```

---

## 11. Tuning the balance PID and gait

The balance loop is `hip offset = -(Kp·pitch + Ki·∫pitch + Kd·pitch_rate)`, applied equally to both
hips, clamped to ±20°, with anti-windup and a 10 Hz filter on the D term. Defaults:
**Kp 0.8, Ki 3.0, Kd 0.03**. In simulation they're stable for servo lags from 30 to 200 ms. Real
servos add backlash and flex, so tune on the robot:

> If you secured the broker with a login (section 13), add `-u <user> -P <password>` to every `mosquitto_pub` / `mosquitto_sub` command.

1. Stream telemetry: `mosquitto_pub -t robot/locomotion/cmd -m telemetry,1` and
   `mosquitto_sub -t robot/locomotion/telemetry` (fields: pitch x10, rate x10, correction x10, mode).
2. Start with **Ki = 0, Kd = 0**: `mosquitto_pub -t robot/locomotion/cmd -m gains,0.5,0,0`.
   Raise Kp until pressing the torso makes it spring back quickly. If it starts to buzz or oscillate,
   back off by a third.
3. Add **Ki** (try 2-4) so the torso ends up exactly level on a slope instead of slightly off.
4. Add a little **Kd** (0.02-0.05) only if it overshoots after a push. Too much Kd causes jitter.
5. Gains are saved in the controller's EEPROM (flash on the ESP32) as you send them. Once happy, copy them into
   `loadSettings()` defaults so a freshly flashed board starts with them.

Gait constants are at the top of the firmware ("legs" section):

| Constant | Default | Effect |
| --- | --- | --- |
| `STAND_HIP_DEG`, `STAND_KNEE_DEG` | 15, 15 | Stance. With no ankle joint, torso pitch = ground + hip - knee, so keep them equal. Adjust together so the centre of mass is over the middle of the feet |
| `GAIT_HZ` | 1.0 | Steps per second |
| `HIP_SWING_DEG` | 12 | Stride length at speed 100 |
| `KNEE_LIFT_DEG` | 12 | Foot clearance during swing. Too high tips the robot sideways on a single foot |
| `GAIT_RAMP_PER_S` | 1.5 | How quickly walking starts and stops |
| `SLEW_DEG_PER_S` | 250 | Joint speed limit |
| `FALL_DEG`, `FALL_MS` | 45, 250 | Fall detection threshold and debounce |

Tips: wide, flat feet with grippy pads (e.g. rubber) do more for stability than any gain. Start
walking tests at `walk,30,0` on a table with a raised edge or a hand ready.

Also worth tuning:

| What | Where |
| --- | --- |
| Servo angle accuracy | `PCA9685_OSC_HZ` in the firmware, if every servo is off by a similar amount |
| Posture sensitivity | constants at the top of `vision_posture_module.py` (the vision log prints the metrics at each alert) |
| Spoken cues | `CUE_PHRASES` in `main.py` |

---

## 12. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Banner says `READY,NOIMU` | MPU6050 not answering on I2C | Check VCC/GND/SDA(A4)/SCL(A5) and AD0 low (address 0x68). The Pi keeps the servos relaxed until it answers (or set `ALLOW_NO_IMU=1` for bench tests) |
| `EVT,IMU_FAIL` while running | I2C glitch from servo noise | Shorter I2C wires, twisted with ground, servo power on a separate supply, 1000 µF at the PCA9685 |
| Torso tilts further instead of correcting | Hip direction or IMU sign wrong | Section 5, steps 3 and 5 (`PITCH_SIGN`, `LEG_DIR`) |
| Buzzing / oscillation while standing | Kp or Kd too high, or loose servo horns | Lower gains (section 11), tighten horn screws |
| Stands leaning forward or back | Calibration or stance | Recalibrate (`calibrate`) on a flat surface; adjust `STAND_HIP_DEG`/`STAND_KNEE_DEG` together |
| `NACK,S,TILTED` when standing | IMU reads more than 30° | Robot is lying down; or run `calibrate` if it is upright |
| `NACK,S,NOIMU` / `NACK,W,NOIMU` | IMU failed and still isn't answering | Check the MPU6050 wiring, then send `stand` again |
| `NACK,D,NOTOF` | VL53L0X not answering, or it stopped measuring | Check `VIN`/`GND`/`SDA` (GPIO21)/`SCL` (GPIO22). Send `rest`, then `distance`: the ESP32 re-initialises it only while not balancing |
| `robot/vision/state DOWN` | Camera unplugged or not delivering frames | Check the cable/`CAMERA_SOURCE`; vision reopens it automatically |
| Walking stops after a second with `EVT,WATCHDOG` | `W` not being repeated | Normal when sending `W` by hand; via `main.py` it means the Pi stalled |
| Robot falls sideways when walking | Single-foot phase too long or feet too narrow | Lower `KNEE_LIFT_DEG`, widen the feet, slower `GAIT_HZ` |
| `No READY from /dev/ttyUSB0` | Wrong port/baud, firmware not flashed | `arduino-cli board list`, reflash, check with `miniterm` |
| `Permission denied: '/dev/ttyUSB0'` | User not in `dialout` | `sudo usermod -aG dialout $USER`, log out and back in |
| Servos jitter or the Pi reboots when they move | Servo power sag | Separate servo supply, bulk capacitor at the PCA9685, common ground |
| `sim_nano.py`: `g++ not found` | No C++ compiler | Install build-essential / MinGW-w64 / Xcode CLT, or use `SERIAL_PORT=sim` |
| `mediapipe ... has no 'solutions' module` | A newer mediapipe got installed | `pip install mediapipe==0.10.18` |
| `No matching distribution found for mediapipe==0.10.18` | Python 3.13 or 32-bit OS | 64-bit Bookworm (Python 3.11) |
| `Failed to open camera via V4L2 at /dev/video0` on a Pi 5 | CSI cameras go through libcamera | `CAMERA_SOURCE=picamera2` and the `--system-site-packages` venv |
| `API ... job failed: ... 401` / `TimeoutError` | Bad key / slow network | Check `.env`; raise `API_TIMEOUT_SECONDS` |
| `PORCUPINE_ACCESS_KEY is not set` | No wake-word key | Add it, or `ENABLE_AUDIO=0` |
| Nothing reacts to `mosquitto_pub` | Broker not running / wrong host | `systemctl status mosquitto`, `MQTT_HOST` |

---

## 13. Secure MQTT

Anyone who can publish to the broker can drive the robot: E-stop it, make it walk, change its gains or
recalibrate it. Keep the broker private.

**Default (recommended): localhost only.** apt's Mosquitto 2.x listens only on `127.0.0.1` until you add a
listener, and the Docker example in section 2 binds to `127.0.0.1` too. Never publish port 1883 on all
interfaces without a login.

**Remote control from another machine.** Keep plain MQTT on localhost for the robot's own programs, add a TLS
listener for everything else, and require a login on both. The robot and the remote controller get separate
users, and the remote one can only do what it needs.

1. Create the two users (each prompts for a password):

   ```bash
   sudo mosquitto_passwd -c /etc/mosquitto/passwd robot
   sudo mosquitto_passwd /etc/mosquitto/passwd remote
   ```

2. `/etc/mosquitto/conf.d/emo-bot.conf` (Mosquitto only allows comments on their own lines):

   ```
   # Robot's own programs: plain text, loopback only. Defining any listener removes Mosquitto's
   # default one, so this line is what keeps the robot's local connection working.
   listener 1883 127.0.0.1

   # Remote control: TLS. The certificate must name the Pi's hostname or IP address (subjectAltName),
   # because clients check it.
   listener 8883
   certfile /etc/mosquitto/certs/server.crt
   keyfile /etc/mosquitto/certs/server.key

   # Both listeners need a login (these settings apply to every listener).
   allow_anonymous false
   password_file /etc/mosquitto/passwd
   acl_file /etc/mosquitto/acl

   # Every EMO-Bot message is tiny; the robot also drops anything over 256 bytes itself.
   message_size_limit 1024
   ```

3. `/etc/mosquitto/acl`:

   ```
   # The robot's programs publish and subscribe everything under robot/
   user robot
   topic readwrite robot/#

   # A remote controller can send commands and the E-stop, and watch everything
   user remote
   topic write robot/locomotion/cmd
   topic write robot/error
   topic read robot/#
   ```

4. Robot's `.env` (plain text is fine on loopback; TLS to `127.0.0.1` would fail the certificate's hostname
   check unless the certificate also names `127.0.0.1`):

   ```dotenv
   MQTT_HOST=127.0.0.1
   MQTT_TLS=0
   MQTT_USERNAME=robot
   MQTT_PASSWORD=<robot's password>
   ```

5. `sudo systemctl restart mosquitto emo-bot` and check the log: every client logs
   `broker ... refused the connection` if the login is wrong (`journalctl -u emo-bot -f`).

**`mosquitto_pub` / `mosquitto_sub` once the broker needs a login.** Every command in this guide then needs
the login too:

```bash
# on the Pi (plain, loopback)
mosquitto_pub -u robot -P '<password>' -t robot/locomotion/cmd -m stand
mosquitto_sub -u robot -P '<password>' -t 'robot/#' -v

# from another machine (TLS; ca.crt is the CA that signed the broker's certificate)
mosquitto_pub -h <pi-address> -p 8883 --cafile ca.crt -u remote -P '<password>' -t robot/error -m error
```

A remote *Python* client using this repo's code sets `MQTT_HOST=<pi-address>`, `MQTT_TLS=1` (port 8883),
`MQTT_CA_CERTS=ca.crt`, `MQTT_USERNAME=remote` and `MQTT_PASSWORD`.

The API keys in `.env` are only ever sent over HTTPS: an `http://` `OPENAI_BASE_URL` or `ELEVENLABS_TTS_URL`
is refused unless it points at this machine (`localhost` or any loopback address). Cached phrases need no
request, so they still play. `LOCAL_LLM_URL` gets no API key; it may use plain `http://` only to a private-network
IP or a `.local` name, so a transcript never crosses the internet unencrypted.
