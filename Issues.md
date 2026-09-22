# EMO-Bot — Known Issues

Code review of `main` @ `b6aa2b2`. API layer (`api_routing_task.py`) out of scope. Line numbers refer to that commit.

**Status: issues 1-13 are fixed; round 2 (14-26, end of file) is open.** Each section ends with a *Resolution* note: what changed and which test covers it. Run `pytest` to check them (109 tests, including 11 firmware scenarios run against both the ESP32 and the Nano firmware, compiled for the PC). The firmware fixes are in both sketches (`firmware/emo_esp32`, `firmware/emo_nano`).

Severity: 🔴 **Critical** (safety / robot stops responding) · 🟠 **High** (wrong behaviour) · 🟡 **Medium** · ⚪ **Low**

| # | Severity | Area | Issue | Status |
|---|---|---|---|---|
| 1 | 🔴 Critical | MQTT / locomotion | `inf` payload kills the MQTT thread (E-stop included) | ✅ Fixed |
| 2 | 🔴 Critical | Serial | `C` (calibrate) outlasts reply timeout → replies shift by one | ✅ Fixed |
| 3 | 🔴 Critical | Safety | `EVT,IMU_FAIL` ignored → servos powered with no balance / fall detection | ✅ Fixed |
| 4 | 🟠 High | Safety | E-stop not prioritised in the serial FIFO | ✅ Fixed |
| 5 | 🟠 High | Behavior tree | `legs_standing` set on queue, not on ACK; `NACK,MODE` unhandled | ✅ Fixed |
| 6 | 🟠 High | Shutdown | Servos not parked on exit | ✅ Fixed |
| 7 | 🟠 High | Conversation | `conversation_active` stuck if the API task crashes | ✅ Fixed |
| 8 | 🟡 Medium | Vision | Posture alert fires on first poor frame after user returns | ✅ Fixed |
| 9 | 🟡 Medium | Vision | Camera read failure never reported or reopened | ✅ Fixed |
| 10 | 🟡 Medium | Concurrency | `SharedState` shared across threads without locks | ✅ Fixed |
| 11 | ⚪ Low | Vision | Face detection runs every frame; result unused | ✅ Fixed |
| 12 | ⚪ Low | Dev env | `numpy<2` has no wheel for Python 3.14 | ✅ Fixed |
| 13 | ⚪ Low | Docs | Nano sketch header points to wrong tuning section | ✅ Fixed |

---

## 🔴 1. `inf` payload kills the MQTT thread

- **Where:** `behavior_tree_module.py:256` (`_clamp_int`), `:281`, `:289`.
- **What:** `int(float("inf"))` and `round(float("inf") * 100)` raise `OverflowError`. Only `IndexError, ValueError` are caught (`:299`).
- **Effect:** exception escapes the paho callback → network thread dies → no further MQTT commands, **E-stop included**.
- **Repro:** publish `walk,inf,0` or `gains,inf,1,1` to `robot/locomotion/cmd`.
- **Fix:** catch `OverflowError`; reject non-finite values (`math.isfinite`) before converting.
- **Resolution:** Numbers are checked with `math.isfinite` before converting and `OverflowError` is caught
  (`behavior_tree_module._finite`). As a second layer, MQTT callbacks can no longer raise: the message handler
  catches and logs everything (`build_mqtt_client`, `safe_apply`). Tests: `test_malformed_commands_are_ignored`
  (inf / nan / out-of-range payloads), `test_mqtt_callback_never_raises`.


## 🔴 2. Calibrate outlasts the reply timeout

- **Where:** `serial_module.py:21` (`REPLY_TIMEOUT_S = 0.3`), `:126`.
- **What:** firmware `C` samples the IMU for ~340 ms before `ACK`. Pi times out at 300 ms and sends the next command.
- **Effect:** the late `ACK,C` is read as the reply to the next command; every later reply is matched to the wrong command (off by one) until reconnect.
- **Fix:** per-command timeout (e.g. 1 s for `C`), and match replies by command letter (`ACK,<cmd>`), discarding mismatches.
- **Resolution:** Every NACK now names the command it answers (`NACK,<cmd>,<reason>`, ESP32 and Nano firmware) and the Pi matches
  replies by command letter (`serial_module.is_reply_to`), so a late `ACK,C` is skipped instead of being taken as
  the next command's reply. Slow commands get longer timeouts (`C` 1.5 s, `S` 1 s). Tests: `test_reply_matching`,
  `test_late_reply_is_not_taken_for_the_next_commands_reply`, `test_slow_commands_get_longer_timeouts`, firmware
  `protocol` scenario.


## 🔴 3. `EVT,IMU_FAIL` ignored

- **Where:** firmware `emo_nano.ino:374` emits it; `behavior_tree_module.py:304` (`apply_serial_line`) only handles `EVT,FALLEN` / `NACK,TILTED` / `EVT,WATCHDOG`.
- **What:** on IMU loss the firmware keeps servos powered in balance mode; Pi keeps commanding stand/walk.
- **Effect:** no balance and no fall detection while the robot is live.
- **Fix:** treat `EVT,IMU_FAIL` (and `NACK,NOIMU`) as fault → send `O`/`R`, set a fault flag, block Stand/Walk until `READY,IMU`.
- **Resolution:** Firmware (ESP32 and Nano): on IMU loss walking stops at once, and `W` (and `S` from rest) is refused with
  `NACK,<cmd>,NOIMU` until the IMU answers again; `S` first tries to re-initialise it (e.g. reseated cable).
  Pi: `EVT,IMU_FAIL`, `NACK,*,NOIMU` and `READY,NOIMU` set an IMU fault that relaxes the servos (`O`), announces it,
  and blocks stand/walk; a `stand` command makes one re-init attempt. Booting without an IMU is only allowed with
  `ALLOW_NO_IMU=1` (bench tests). Tests: firmware `imu_fail` scenario,
  `test_imu_failure_relaxes_and_blocks_standing`, `test_stand_retries_imu_once_and_recovers`,
  `test_boot_without_imu_is_a_fault_unless_allowed`.


## 🟠 4. E-stop not prioritised

- **Where:** `behavior_tree_module.py:59` (`motor_queue`, FIFO, maxsize 200), `:112`.
- **What:** `E` is appended behind already-queued commands; each takes up to 0.3 s round trip.
- **Effect:** E-stop latency grows with queue depth; a full queue drops it.
- **Fix:** separate priority path (flush queue + send `E` immediately), or `PriorityQueue`.
- **Resolution:** `E` goes through `serial_module.offer_urgent`, which discards everything still queued and puts `E`
  next, so it waits for at most the one command in flight and can never be dropped. Test:
  `test_estop_jumps_the_queue`.


## 🟠 5. `legs_standing` set optimistically

- **Where:** `behavior_tree_module.py:83`, `:231–232`.
- **What:** flag set when `S` is queued, not when `ACK,S` arrives. `NACK,MODE` (and other `S` rejections) never clear it.
- **Effect:** Pi believes the robot is standing and walks/poses a relaxed robot; never retries `S`.
- **Fix:** set on `ACK,S`; clear on any `NACK` to `S`.
- **Resolution:** `legs_standing` is set only by `ACK,S` (or `ACK,S,NOIMU`) and cleared by `NACK,S,*`, `NACK,W,*`,
  `ACK,O`, `ACK,E`, falls, IMU faults and Nano resets. An unanswered `S` is retried after 1 s, and walking waits
  for the ACK. Every serial reply now reaches the behavior tree. Tests:
  `test_stand_is_confirmed_by_ack_not_by_sending`, `test_unanswered_stand_is_retried`,
  `test_nack_mode_on_walk_makes_it_stand_again`, `test_walk_waits_for_stand_ack`.


## 🟠 6. Servos not parked on shutdown

- **Where:** `main.py:250` (`finally`), `serial_module.py:148` (port closed, nothing sent).
- **What:** exit closes the port without `R`/`O`. Walk stops after the 1 s watchdog; balance mode keeps servos powered until power-off.
- **Fix:** on shutdown send `R` (rest) then `O` (servos off), wait for ACK, then close the port.
- **Resolution:** On shutdown (cancellation) `serial_task` sends `W,0,0` then `O` and waits for the replies before
  closing the port. `R` is deliberately not sent: in the protocol it releases a latched E-stop. Test:
  `test_park_stops_walking_and_relaxes`.


## 🟠 7. `conversation_active` can get stuck

- **Where:** set by `audio_trigger_task.py:151` (`wake_flag=1`), cleared only by `api_routing_task.py:214`; read at `behavior_tree_module.py:334`, `:168`.
- **What:** if the API task is dead (optional task, logged and skipped), nobody publishes `wake_flag=0`.
- **Effect:** Conversation branch stays active; posture and walk branches starve.
- **Fix:** timestamp + timeout on the flag, and clear it from the supervisor when the task dies.
- **Resolution:** The wake flag carries a timestamp and expires after 30 s (`CONVERSATION_TIMEOUT_S`), and when the API
  or wake-word task crashes the supervisor clears the conversation, releases the busy flag and publishes
  `wake_flag=0` (`main.py`, `end_conversation`). Tests: `test_stuck_conversation_flag_times_out`,
  `test_clear_conversation`.


## 🟡 8. Posture alert fires too early after absence

- **Where:** `vision_posture_module.py:229–243` (`PostureMonitor.update`).
- **What:** `poor_since` only reset when an alert was already raised. Absent without alert → old `poor_since` survives.
- **Effect:** user returns with one poor frame → `now - poor_since >= confirm_s` immediately → alert.
- **Fix:** reset `poor_since` (and `good_since`) whenever `poor is None`.
- **Resolution:** Once the user has been out of view for more than 1 s (a grace period, so single-frame dropouts don't
  count), both the poor and the good timers restart. Tests: `test_returning_user_needs_full_confirm_time`,
  `test_brief_dropout_does_not_restart_the_timer`.


## 🟡 9. Camera failure never handled

- **Where:** `main.py:104–106`.
- **What:** `cap.read()` failure → sleep 10 ms, retry forever. No log, no reopen, no state published.
- **Effect:** unplugged/crashed camera = silent vision outage.
- **Fix:** count consecutive failures → log, release and reopen with backoff; publish a vision-down state.
- **Resolution:** New `ResilientCamera` (`vision_posture_module.py`): after 30 failed reads it logs, releases and reopens
  the camera with backoff (1 s doubling to 30 s), also keeps retrying if the camera is missing at start, and
  publishes `robot/vision/state` `UP` / `DOWN` (retained). Tests: `test_camera_reopens_after_failures`,
  `test_camera_missing_at_start_keeps_retrying`.


## 🟡 10. `SharedState` without locks

- **Where:** `behavior_tree_module.py:37`.
- **What:** written from the paho thread (MQTT callbacks) and the asyncio tick; compound updates (`legs_standing = walking = False`, `walk_*` + `walk_until`) are not atomic.
- **Effect:** tick can read half-updated state (e.g. new `walk_until` with old speed).
- **Fix:** marshal MQTT callbacks onto the event loop (`loop.call_soon_threadsafe`) or guard with a lock.
- **Resolution:** MQTT callbacks no longer touch `SharedState`: they hand each message to the tick thread (`main.py`
  uses `loop.call_soon_threadsafe(safe_apply, ...)`, standalone mode an inbox queue). Serial lines already arrive
  on the event loop, so every state change now happens on one thread.


## ⚪ 11. Face detection result unused

- **Where:** `vision_posture_module.py:292`; consumed into `SharedState.face_error` (`behavior_tree_module.py:349`) but no behaviour reads it.
- **Effect:** CPU spent per frame on the Pi for nothing.
- **Fix:** use it (head tracking / attention) or run it at a lower rate / disable.
- **Resolution:** Face detection is optional and off by default (`FACE_DETECTION=0`); only pose estimation runs. Turn it
  on once something (e.g. future head tracking) uses `robot/vision/face_error`. Test:
  `test_face_detection_is_optional`.


## ⚪ 12. `numpy<2` has no Python 3.14 wheel

- **Where:** `requirements-dev.txt:7`.
- **Effect:** dev install fails on Python 3.14 (source build).
- **Fix:** relax pin to match the MediaPipe version in use, or document the supported Python version.
- **Resolution:** `requirements-dev.txt` no longer pins `numpy<2` (MediaPipe, which needs it, isn't installed for the
  tests), so the tests install on any Python 3.9+. The robot itself still needs Python 3.9-3.12 for
  MediaPipe 0.10.18, as documented in RUNNING.md.


## ⚪ 13. Wrong tuning-section reference

- **Where:** `firmware/emo_nano/emo_nano.ino:6` ("RUNNING.md, section 10"). Section 10 is systemd; tuning is section 11.
- **Fix:** change to section 11.
- **Resolution:** Both sketch headers (ESP32 and Nano) now point to RUNNING.md section 4 (flashing), 5 (first power-up)
  and 11 (tuning).


---

# Round 2: bug, security and efficiency review (Raspberry Pi side, open)

Review of `main` @ `6f69e19`, API layer included. Line numbers refer to that commit. Red-team pass on the MQTT,
serial, audio and API paths. **Not fixed yet**: Pi-side code is reported here, not changed by the reviewer; each section has a proposed fix for the Pi code owner.

| # | Severity | Area | Issue | Status |
|---|---|---|---|---|
| 14 | 🔴 Critical | Safety | E-stop silently dropped under an MQTT command flood | ⏳ Open |
| 15 | 🟠 High | Safety | Controller stands without IMU while the Pi reports an IMU fault | ⏳ Open |
| 16 | 🟠 High | Security | MQTT has no login or TLS; the Docker example exposes the broker to the LAN | ⏳ Open |
| 17 | 🟠 High | Conversation | Speech cues can evict a queued recording → wake word dead until restart | ⏳ Open |
| 18 | 🟡 Medium | Safety | E-stop doesn't cancel the active walk; walking resumes after `clear` | ⏳ Open |
| 19 | 🟡 Medium | Security | Unthrottled `gains` / `calibrate` wear out the controller's flash | ⏳ Open |
| 20 | 🟡 Medium | Security | No MQTT payload size limit | ⏳ Open |
| 21 | ⚪ Low | Security | API keys can be sent over plain HTTP | ⏳ Open |
| 22 | ⚪ Low | Privacy | What the user said is logged at INFO | ⏳ Open |
| 23 | ⚪ Low | Dev tool | `sim_nano.py` builds to a predictable path in the shared temp dir | ⏳ Open |
| 24 | ⚪ Low | Efficiency | Vision wastes CPU per frame | ⏳ Open |
| 25 | ⚪ Low | Efficiency | Audio round-trips every WAV through temp files | ⏳ Open |
| 26 | ⚪ Low | Cleanup | Dead and duplicated code | ⏳ Open |

Firmware findings from the same review are already fixed on the `Fixes` branch (see the end of this file).


## 🔴 14. E-stop silently dropped under an MQTT command flood

- **Where:** `behavior_tree_module.py:67` (unbounded `outbox`), `:121` (`ServiceCommands`), `:138-141` (`EStopGuard`).
- **What:** `ServiceCommands` is the first child of the tree and moves the whole outbox into `motor_queue`
  (max 200) every tick. `EStopGuard` runs after it; `put_motor("E")` returns `False` when the queue is full, but
  the result is ignored and `latched = True` is set anyway.
- **Effect:** E is never sent and never retried: **servos stay powered while the Pi believes it is E-stopped**.
- **Repro:** within one 100 ms tick publish > 200 `gesture` / `telemetry,1` messages to `robot/locomotion/cmd`, and
  `error` to `robot/error`.
- **Fix:** bound the outbox (e.g. 16, reject extra with a warning); add `CommandBus.put_urgent()` that empties
  `motor_queue` before queueing E; latch only once E is queued.


## 🟠 15. Controller stands without IMU while the Pi reports an IMU fault

- **Where:** `behavior_tree_module.py:166` (`ImuFaultGuard` sends O only once), `:384-388`, `:397`;
  firmware `emo_esp32.ino:614` (`ACK,S,NOIMU`).
- **What:** with `ALLOW_NO_IMU=0` and no IMU at boot: `READY,NOIMU` → `imu_fault` → guard sends O once. A `stand`
  command sets `imu_retry` → S. The firmware stands in balance mode without the IMU and answers `ACK,S,NOIMU`.
  `apply_serial_line` sets `legs_standing = True` but leaves `imu_fault` set; the guard has already sent its O.
- **Effect:** servos powered with no balance and no fall detection, which `ALLOW_NO_IMU=0` is meant to prevent.
  Second case, with `ALLOW_NO_IMU=1`: `NACK,C,NOIMU` (calibrate on the bench) sets `imu_fault`, and
  `ACK,S,NOIMU` never clears it → stuck in the fault branch.
- **Fix:** guard re-sends O whenever `imu_fault and legs_standing`; `ACK,S,NOIMU` sets
  `imu_fault = not ALLOW_NO_IMU`; don't count `NACK,C,NOIMU` as a fault (calibration only).
  The `Fixes` firmware now retries IMU init on every S, so `ACK,S` means the IMU really came back.


## 🟠 16. MQTT has no login or TLS; Docker example exposes the broker

- **Where:** `config.py:29-30` (host/port only); four client builders (`main.py:90`, `behavior_tree_module.py:507`,
  `audio_trigger_task.py:31`, `vision_posture_module.py:65`); `RUNNING.md:125`.
- **What:** every client connects anonymously in plain text. The Docker command publishes `-p 1883:1883` on all
  interfaces with `mosquitto-no-auth.conf`.
- **Effect:** anyone on the network can E-stop, walk, change gains, calibrate, or spoof the wake flag/posture
  events. (apt Mosquitto 2.x listens on localhost only by default, so only the Docker path is exposed out of the box.)
- **Fix:** `MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_TLS`, `MQTT_CA_CERTS` in config, applied by one shared client
  factory; Docker example `-p 127.0.0.1:1883:1883`; document `password_file` + ACLs for remote access.


## 🟠 17. Speech cues can evict a queued recording

- **Where:** `main.py:189` (`offer(speech_queue, ...)`), `main.py:234` (`maxsize=20`).
- **What:** `offer` drops the **oldest** job when the queue is full. That can be a `LISTEN_JOB`; `busy_event` is
  cleared only after a `LISTEN_JOB` is processed.
- **Effect:** `busy_event` stays set → the wake-word worker skips every frame → **no wake word until restart**.
  The wake flag stays `1` until the 30 s timeout and the recording leaks in `/dev/shm`.
- **Repro:** while one API job is running (up to 15 s + playback), toggle `robot/error` `error`/`clear` 20 times
  (each latch queues `AUDIO,EMERGENCY_STOP`).
- **Fix:** queue cues with `put_nowait` and drop the **cue** when full; never evict listen jobs.


## 🟡 18. E-stop doesn't cancel the active walk

- **Where:** `behavior_tree_module.py:136-141`.
- **What:** `EStopGuard` doesn't reset `walk_until`.
- **Effect:** `walk,50,0,10` → `error` → `clear` within 10 s: R, S, then W — the robot walks off again after an
  emergency stop. `test_estop_latches_once_and_restands_after_release` sends `stop` before `clear`, masking it.
- **Fix:** `state.walk_until = 0.0` when the E-stop latches; test without the `stop`.


## 🟡 19. Unthrottled `gains` / `calibrate` wear out the flash

- **Where:** `behavior_tree_module.py:366`, `:371`; firmware `saveSettings()` (`emo_esp32.ino:278`).
- **What:** every K and C saves settings; on the ESP32 that is a flash sector erase + write
  (`EEPROM.commit()`), with no rate limit on the Pi.
- **Effect:** a flood of `gains` at serial speed (~50/s) reaches the ~100k erase-cycle rating in well under an
  hour → settings corrupt / lost. (The `Fixes` firmware skips unchanged writes; alternating values still write.)
- **Fix:** rate-limit K and C on the Pi (e.g. 1 per second, warn and drop the rest).


## 🟡 20. No MQTT payload size limit

- **Where:** `behavior_tree_module.py:524`.
- **What:** any payload is decoded and stripped on paho's thread; Mosquitto accepts up to 256 MB by default.
- **Effect:** memory/CPU spike on the Pi from one large message.
- **Fix:** drop payloads over 256 bytes before decoding; set `message_size_limit` in the broker config.


## ⚪ 21. API keys can be sent over plain HTTP

- **Where:** `api_routing_task.py:48`, `:62`, `:93`.
- **What:** `OPENAI_BASE_URL` / `ELEVENLABS_TTS_URL` are used as given; an `http://` value sends the Bearer token
  and `xi-api-key` in clear text.
- **Fix:** refuse non-HTTPS URLs unless the host is `localhost` / `127.0.0.1` / `::1` (fall back to the error sound).


## ⚪ 22. What the user said is logged at INFO

- **Where:** `api_routing_task.py:156` (`Heard`), `:158` (`Replying`).
- **Effect:** journald on the Pi keeps a transcript of every conversation.
- **Fix:** log both at DEBUG.


## ⚪ 23. Predictable simulator build path

- **Where:** `tools/sim_nano.py:40`.
- **What:** the executable is always `<tmp>/emo_sim_<firmware>` in the shared temp directory.
- **Effect:** on a multi-user Linux box another user can pre-create or swap that file (symlink / TOCTOU) and have
  their binary run.
- **Fix:** build into `tempfile.mkdtemp()` and remove it on exit.


## ⚪ 24. Vision wastes CPU per frame

- **Where:** `vision_posture_module.py:378`, `:389-394`, `:176`.
- **What:**
  - BGR→RGB conversion runs on every frame, pose only on every 2nd (face detection is off by default) → half the
    conversions are unused.
  - Skipped frames are still fully decoded (`read()` instead of `grab()`).
  - `posture_metrics()` runs twice per pose frame (directly and inside `is_poor_posture`).
  - Pose at ~15 Hz; posture timing (3 s confirm, 1 s clear) needs ~5 Hz.
- **Fix:** time-based pose throttle (~0.2 s), `grab()` for skipped frames, convert only when a detector runs, compute
  metrics once. Expected: roughly 3x less MediaPipe CPU on the Pi.


## ⚪ 25. Audio round-trips every WAV through temp files

- **Where:** `audio_trigger_task.py:71` (recording → `/dev/shm` file), `api_routing_task.py:108` (TTS → file → aplay),
  `audio_trigger_task.py:102`.
- **What:** WAVs are written to disk and read back / deleted; the `struct` format string (`"h" * 512`) is rebuilt
  for every audio frame (~31/s).
- **Fix:** pass WAV bytes in memory, pipe playback into `aplay -` stdin; precompile one `struct.Struct`.


## ⚪ 26. Dead and duplicated code

- `behavior_tree_module.py:468` `on_mqtt_message`: unused since MQTT messages go through `build_mqtt_client`.
- `vision_posture_module.py:24-25` `TOPIC_STATE` / `TOPIC_FACE_ERROR` aliases: unused.
- Four copies of the MQTT client setup (see #16) and two copies of the camera loop (`main._vision_worker`,
  `vision_posture_module.run`).
- **Fix:** delete the unused code; one MQTT factory, one shared camera loop.


---

## Firmware (fixed on the `Fixes` branch)

Both sketches, host tests extended, real ESP32 and Nano compiles pass.

- **E-stop latency:** `allOutputsOff()` made 16 I2C writes; now one `ALL_LED` write (full-off bit), with the
  per-channel writes as fallback if the bus NACKs.
- **Gain cap:** K accepted gains above 1000 when sent straight over serial (only the Pi checked); now `NACK,K,PARSE`.
- **IMU plugged in after boot:** S retried the IMU only after a runtime loss; now whenever the IMU is missing.
- **ESP32 double math:** `PI`, `TWO_PI`, `RAD_TO_DEG`, `sin`/`cos`/`atan2`/`lround` ran as software doubles on a
  single-precision FPU; now float versions (~5 KB less flash).
- **Flash writes:** ESP32 committed settings on every K/C even when unchanged; now compares first.
- **IMU read:** 14 bytes per sample, 12 used; now 12.
