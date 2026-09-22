# EMO-Bot — Known Issues

Code review of `main` @ `b6aa2b2`. API layer (`api_routing_task.py`) out of scope. Line numbers refer to that commit.

**Status: issues 1-26 are fixed (1-13 in round 1, 14-26 in round 2, both on `Fixes`); round 3 (27-34, end of file) is open.** Each section ends with a *Resolution* note: what changed and which test covers it. Run `pytest` to check them (138 tests, including 11 firmware scenarios run against both the ESP32 and the Nano firmware, compiled for the PC). The firmware fixes are in both sketches (`firmware/emo_esp32`, `firmware/emo_nano`).

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

# Round 2: bug, security and efficiency review (Raspberry Pi side, fixed)

Review of `main` @ `6f69e19`, API layer included. Line numbers refer to that commit. Red-team pass on the MQTT,
serial, audio and API paths. The reviewer reported the Pi-side issues without changing that code; they are now
fixed on the `Fixes` branch, each section ending with a *Resolution* note (what changed, which test covers it).

| # | Severity | Area | Issue | Status |
|---|---|---|---|---|
| 14 | 🔴 Critical | Safety | E-stop silently dropped under an MQTT command flood | ✅ Fixed |
| 15 | 🟠 High | Safety | Controller stands without IMU while the Pi reports an IMU fault | ✅ Fixed |
| 16 | 🟠 High | Security | MQTT has no login or TLS; the Docker example exposes the broker to the LAN | ✅ Fixed |
| 17 | 🟠 High | Conversation | Speech cues can evict a queued recording → wake word dead until restart | ✅ Fixed |
| 18 | 🟡 Medium | Safety | E-stop doesn't cancel the active walk; walking resumes after `clear` | ✅ Fixed |
| 19 | 🟡 Medium | Security | Unthrottled `gains` / `calibrate` wear out the controller's flash | ✅ Fixed |
| 20 | 🟡 Medium | Security | No MQTT payload size limit | ✅ Fixed |
| 21 | ⚪ Low | Security | API keys can be sent over plain HTTP | ✅ Fixed |
| 22 | ⚪ Low | Privacy | What the user said is logged at INFO | ✅ Fixed |
| 23 | ⚪ Low | Dev tool | `sim_nano.py` builds to a predictable path in the shared temp dir | ✅ Fixed |
| 24 | ⚪ Low | Efficiency | Vision wastes CPU per frame | ✅ Fixed |
| 25 | ⚪ Low | Efficiency | Audio round-trips every WAV through temp files | ✅ Fixed |
| 26 | ⚪ Low | Cleanup | Dead and duplicated code | ✅ Fixed |

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
- **Resolution:** `EStopGuard` is now the first node of the tree, so nothing can fill the queue ahead of it. `E` goes
  through the new `CommandBus.put_urgent()`, which empties the motor queue before queueing it, and the guard
  latches only once `E` is really queued (otherwise it retries next tick). The outbox is capped at 16 commands;
  extra ones are dropped (one warning per second), and latching the E-stop discards anything still queued so
  nothing stale runs after `clear`. Tests: `test_estop_survives_a_command_flood`,
  `test_estop_latches_only_once_queued`, `test_estop_discards_queued_tuning_commands`, `test_command_flood_logs_once`.


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
- **Resolution:** `ACK,S,NOIMU` now sets `imu_fault = not ALLOW_NO_IMU`, and `ImuFaultGuard` sends `O` again whenever the
  robot is standing during a fault, so a retry that stands without the IMU is relaxed straight away. `ACK,S`
  (IMU answered; the `Fixes` firmware retries it on every `S`) clears the fault. `NACK,C,NOIMU` is no longer a
  fault (only `NACK,S,NOIMU` / `NACK,W,NOIMU` / `EVT,IMU_FAIL` are). Tests: `test_stood_up_without_imu_is_relaxed_again`,
  `test_bench_mode_calibrate_without_imu_is_not_a_fault`.


## 🟠 16. MQTT has no login or TLS; Docker example exposes the broker

- **Where:** `config.py:29-30` (host/port only); four client builders (`main.py:90`, `behavior_tree_module.py:507`,
  `audio_trigger_task.py:31`, `vision_posture_module.py:65`); `RUNNING.md:125`.
- **What:** every client connects anonymously in plain text. The Docker command publishes `-p 1883:1883` on all
  interfaces with `mosquitto-no-auth.conf`.
- **Effect:** anyone on the network can E-stop, walk, change gains, calibrate, or spoof the wake flag/posture
  events. (apt Mosquitto 2.x listens on localhost only by default, so only the Docker path is exposed out of the box.)
- **Fix:** `MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_TLS`, `MQTT_CA_CERTS` in config, applied by one shared client
  factory; Docker example `-p 127.0.0.1:1883:1883`; document `password_file` + ACLs for remote access.
- **Resolution:** New `mqtt_client.py` is the one place MQTT clients are made. It applies `MQTT_USERNAME` / `MQTT_PASSWORD`,
  `MQTT_TLS` / `MQTT_CA_CERTS` (port defaults to 8883 with TLS), attaches callbacks before connecting, and warns
  when talking plain text to a non-local broker. The Docker example now binds `127.0.0.1:1883`, and RUNNING.md
  section 13 documents `password_file`, ACLs, TLS and `message_size_limit`. Tests: `test_mqtt_login_and_tls_are_applied`,
  `test_every_module_uses_the_shared_mqtt_factory`, `test_tls_port_default`.


## 🟠 17. Speech cues can evict a queued recording

- **Where:** `main.py:189` (`offer(speech_queue, ...)`), `main.py:234` (`maxsize=20`).
- **What:** `offer` drops the **oldest** job when the queue is full. That can be a `LISTEN_JOB`; `busy_event` is
  cleared only after a `LISTEN_JOB` is processed.
- **Effect:** `busy_event` stays set → the wake-word worker skips every frame → **no wake word until restart**.
  The wake flag stays `1` until the 30 s timeout and the recording leaks in `/dev/shm`.
- **Repro:** while one API job is running (up to 15 s + playback), toggle `robot/error` `error`/`clear` 20 times
  (each latch queues `AUDIO,EMERGENCY_STOP`).
- **Fix:** queue cues with `put_nowait` and drop the **cue** when full; never evict listen jobs.
- **Resolution:** Cues go through `main.queue_cue()`: `put_nowait`, and when the queue is full the **cue** is dropped (with
  a warning). Older jobs, including recordings, are never evicted. (Recordings also no longer touch `/dev/shm`,
  see #25.) Test: `test_speech_cue_never_evicts_a_recording`.


## 🟡 18. E-stop doesn't cancel the active walk

- **Where:** `behavior_tree_module.py:136-141`.
- **What:** `EStopGuard` doesn't reset `walk_until`.
- **Effect:** `walk,50,0,10` → `error` → `clear` within 10 s: R, S, then W — the robot walks off again after an
  emergency stop. `test_estop_latches_once_and_restands_after_release` sends `stop` before `clear`, masking it.
- **Fix:** `state.walk_until = 0.0` when the E-stop latches; test without the `stop`.
- **Resolution:** Latching the E-stop sets `walk_until = 0`, so `clear` stands the robot up but doesn't resume the walk.
  `test_estop_latches_once_and_restands_after_release` no longer sends `stop` and checks that no `W` follows.


## 🟡 19. Unthrottled `gains` / `calibrate` wear out the flash

- **Where:** `behavior_tree_module.py:366`, `:371`; firmware `saveSettings()` (`emo_esp32.ino:278`).
- **What:** every K and C saves settings; on the ESP32 that is a flash sector erase + write
  (`EEPROM.commit()`), with no rate limit on the Pi.
- **Effect:** a flood of `gains` at serial speed (~50/s) reaches the ~100k erase-cycle rating in well under an
  hour → settings corrupt / lost. (The `Fixes` firmware skips unchanged writes; alternating values still write.)
- **Fix:** rate-limit K and C on the Pi (e.g. 1 per second, warn and drop the rest).
- **Resolution:** `gains` and `calibrate` share a limit of one per second (`FLASH_WRITE_MIN_S`); extra ones are dropped with
  a warning. Together with the firmware skipping unchanged writes and capping gains. Test:
  `test_flash_writes_are_rate_limited`.


## 🟡 20. No MQTT payload size limit

- **Where:** `behavior_tree_module.py:524`.
- **What:** any payload is decoded and stripped on paho's thread; Mosquitto accepts up to 256 MB by default.
- **Effect:** memory/CPU spike on the Pi from one large message.
- **Fix:** drop payloads over 256 bytes before decoding; set `message_size_limit` in the broker config.
- **Resolution:** The behavior tree's MQTT handler drops any payload over 256 bytes (`mqtt_client.MAX_PAYLOAD_BYTES`)
  before decoding it (warning at most once per second). RUNNING.md section 13 sets `message_size_limit` on the
  broker too. Test: `test_oversized_mqtt_payloads_are_dropped_unread`.


## ⚪ 21. API keys can be sent over plain HTTP

- **Where:** `api_routing_task.py:48`, `:62`, `:93`.
- **What:** `OPENAI_BASE_URL` / `ELEVENLABS_TTS_URL` are used as given; an `http://` value sends the Bearer token
  and `xi-api-key` in clear text.
- **Fix:** refuse non-HTTPS URLs unless the host is `localhost` / `127.0.0.1` / `::1` (fall back to the error sound).
- **Resolution:** `api_routing_task.require_https()` refuses any non-HTTPS API URL unless the host is `localhost`,
  `127.0.0.1` or `::1`; the job fails before a request is made and the error sound plays. Tests:
  `test_require_https`, `test_plain_http_api_url_never_receives_keys`.


## ⚪ 22. What the user said is logged at INFO

- **Where:** `api_routing_task.py:156` (`Heard`), `:158` (`Replying`).
- **Effect:** journald on the Pi keeps a transcript of every conversation.
- **Fix:** log both at DEBUG.
- **Resolution:** `Heard` / `Replying` are logged at DEBUG. Test: `test_conversation_text_is_not_logged_at_info`.


## ⚪ 23. Predictable simulator build path

- **Where:** `tools/sim_nano.py:40`.
- **What:** the executable is always `<tmp>/emo_sim_<firmware>` in the shared temp directory.
- **Effect:** on a multi-user Linux box another user can pre-create or swap that file (symlink / TOCTOU) and have
  their binary run.
- **Fix:** build into `tempfile.mkdtemp()` and remove it on exit.
- **Resolution:** `sim_nano.py` builds into a private `tempfile.mkdtemp()` directory and removes it on exit. Test:
  `test_simulator_builds_into_a_private_directory`.


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
- **Resolution:** Pose runs on a timer (`POSE_PERIOD_S = 0.2`, ~5 Hz). Frames the pipeline won't use are taken with
  `grab()` (no decode; free on picamera2), BGR->RGB conversion only runs when a detector runs, and posture
  metrics are computed once per pose frame (`judge_posture`). Tests: `test_pose_runs_at_about_5_hz`,
  `test_grab_skips_decoding`, `test_shared_camera_loop_grabs_unwanted_frames`, `test_posture_metrics_computed_once`.


## ⚪ 25. Audio round-trips every WAV through temp files

- **Where:** `audio_trigger_task.py:71` (recording → `/dev/shm` file), `api_routing_task.py:108` (TTS → file → aplay),
  `audio_trigger_task.py:102`.
- **What:** WAVs are written to disk and read back / deleted; the `struct` format string (`"h" * 512`) is rebuilt
  for every audio frame (~31/s).
- **Fix:** pass WAV bytes in memory, pipe playback into `aplay -` stdin; precompile one `struct.Struct`.
- **Resolution:** Recordings stay in memory: `LISTEN_JOB` carries WAV bytes, which are uploaded directly; TTS audio is piped
  into `aplay -` on stdin. No temp files remain (only the bundled fallback sound is played from disk). The PCM
  unpacker is a `struct.Struct` built once per stream. Tests: `test_full_cascade_plays_valid_wav_from_memory`,
  `test_recording_stays_in_memory`.


## ⚪ 26. Dead and duplicated code

- `behavior_tree_module.py:468` `on_mqtt_message`: unused since MQTT messages go through `build_mqtt_client`.
- `vision_posture_module.py:24-25` `TOPIC_STATE` / `TOPIC_FACE_ERROR` aliases: unused.
- Four copies of the MQTT client setup (see #16) and two copies of the camera loop (`main._vision_worker`,
  `vision_posture_module.run`).
- **Fix:** delete the unused code; one MQTT factory, one shared camera loop.
- **Resolution:** Removed `on_mqtt_message` and the `TOPIC_STATE` / `TOPIC_FACE_ERROR` aliases. All four MQTT client copies
  use `mqtt_client.make_client`, and main.py and standalone vision share one camera loop
  (`vision_posture_module.run_vision`). Tests: `test_every_module_uses_the_shared_mqtt_factory`,
  `test_shared_camera_loop_grabs_unwanted_frames`.


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


---

# Round 3: review of the round-2 fixes (Raspberry Pi side, open)

Review of `Fixes` @ `6a6a43e` (Pi fixes for 14-26). Line numbers refer to that commit. All 138 tests pass there;
the findings below are what the tests don't cover. **Not fixed yet**: Pi-side code is reported here, not changed by
the reviewer; each section has a proposed fix for the Pi code owner.

| # | Severity | Area | Issue | Status |
|---|---|---|---|---|
| 27 | 🟠 High | Efficiency | Pi camera (Picamera2): vision thread busy-loops on `grab()`, one CPU core at ~100% | ⏳ Open |
| 28 | 🟡 Medium | Safety | `stand` during an IMU fault stands the robot blind for a moment before relaxing it | ⏳ Open (firmware side ready) |
| 29 | 🟡 Medium | Safety | Tuning commands sent during an E-stop (incl. `calibrate`) run after `clear` | ⏳ Open |
| 30 | 🟡 Medium | Safety | Commands queued while the controller is unplugged are replayed on reconnect | ⏳ Open |
| 31 | ⚪ Low | Behaviour | Flash rate limit is spent by commands that are then dropped; gains and calibrate share it | ⏳ Open |
| 32 | ⚪ Low | Docs | Secure-MQTT setup breaks the robot's own local connection and every `mosquitto_pub` example | ⏳ Open |
| 33 | ⚪ Low | Consistency | `require_https` blocks cached phrases; its "local" check differs from `mqtt_client._is_local` | ⏳ Open |
| 34 | ⚪ Low | Diagnostics | Broker login failures are silent for the publisher and audio clients | ⏳ Open |


## 🟠 27. Picamera2: vision thread busy-loops on `grab()`

- **Where:** `vision_posture_module.py:78` (`_Picamera2Capture.grab` returns `True` at once), `:437` (`run_vision`).
- **What:** between pose frames `run_vision` calls `camera.grab()` in a tight loop. `cv2.VideoCapture.grab()` blocks
  until the next frame, so V4L2/USB cameras are paced. The Picamera2 wrapper's `grab()` returns immediately and
  never waits for a frame, so the loop spins for the whole 0.2 s gap.
- **Effect:** with `CAMERA_SOURCE=picamera2` (the Pi 5 CSI camera) and face detection off: one core at ~100%, and
  the spinning thread competes for the GIL with the asyncio loop (behavior tree, serial link). Measured with a
  Picamera2-like fake camera: **~3.7 million `grab()` calls and 1.7 s of CPU in 2 s**, 10 frames decoded.
  The #24 resolution note ("free on picamera2") is wrong.
- **Fix:** make `_Picamera2Capture.grab()` wait for and drop one frame (e.g. `self._cam.capture_request().release()`),
  or have `run_vision` sleep until `last_pose + POSE_PERIOD_S` when the pipeline doesn't want a frame. Add a test
  with a non-blocking fake camera that counts `grab()` calls.


## 🟡 28. `stand` during an IMU fault stands the robot blind

- **Where:** `behavior_tree_module.py:190-192` (`ImuFaultGuard` sends `S` as the IMU retry), `:428-433`.
- **What:** with `ALLOW_NO_IMU=0` and a controller that **booted** without an IMU, `stand` → `S`. The firmware retries
  the IMU; if it is still missing it stands anyway (booting without an IMU is allowed for bench tests) and answers
  `ACK,S,NOIMU`. Only then does the guard send `O`.
- **Effect:** each `stand` drives all four servos to the stand pose with no balance and no fall detection for one
  serial round trip plus one tick (~100-200 ms), then drops them limp: the robot can jolt up and fall over.
- **Fix:** use the new firmware `I` command (on `Fixes`, both sketches): it retries the IMU **without moving a servo**
  and answers `ACK,I` (IMU working) or `NACK,I,NOIMU` (`NACK,I,MODE` while balancing). In `ImuFaultGuard` send `I`
  instead of `S`; in `apply_serial_line` clear `imu_fault` on `ACK,I`, keep it on `NACK,I,NOIMU`, and let the
  normal branches send `S` once the fault is gone. Add `"I": 1.0` to `SLOW_REPLY_TIMEOUT_S` (`serial_module.py:24`),
  as the retry re-measures the gyro bias like `S` does.


## 🟡 29. Tuning commands sent during an E-stop run after `clear`

- **Where:** `behavior_tree_module.py:134-137` (`ServiceCommands`), `:149-159` (`EStopGuard`), `:410-415`.
- **What:** latching the E-stop clears the outbox once. While it stays latched, `EStopGuard` returns SUCCESS first,
  so `ServiceCommands` never runs, but `apply_locomotion_command` keeps filling the outbox. On `clear` it is flushed.
- **Effect:** `error` → `calibrate` + `gesture` → `clear` sends `R, O, C, G,1, O`. A calibration asked for while the
  robot was being handled runs later and stores that level offset in flash; this contradicts the #14 resolution
  ("nothing stale runs after `clear`").
- **Fix:** while `estop_active`, reject outbox commands (`gesture`, `telemetry`, `gains`, `calibrate`) with a
  warning, or clear the outbox again when the E-stop is released. Test: `error`, `calibrate`, `clear` → no `C`.


## 🟡 30. Commands queued while the controller is unplugged are replayed on reconnect

- **Where:** `serial_module.py:141-202` (`serial_task`), `main.py:154-162`.
- **What:** while the serial port is down, the behavior tree keeps queueing (`S` retries, `W` heartbeats, `K`, `C`,
  `G,1`) into `serial_queue` (200, oldest dropped). After reconnecting, `serial_task` sends that backlog in order;
  `on_connect` only resets the leg state.
- **Effect:** stale commands reach a freshly booted controller: a `calibrate` or `gains` sent minutes earlier is
  written to flash, and old walk commands or gestures run. They also delay the fresh `S`.
- **Fix:** drop everything in `serial_queue` when the port (re)opens and when a `READY` reset is detected, before
  `on_connect()` runs, so the tree rebuilds the state from scratch. Test: queue `C` while disconnected, then
  reconnect → no `C` sent.


## ⚪ 31. Flash rate limit spent by dropped commands; gains and calibrate share it

- **Where:** `behavior_tree_module.py:362-370`, `:404-415`.
- **What:** `_flash_write_allowed` records the time before `_queue_raw`, which can still drop the command when the
  outbox is full. `gains` and `calibrate` share one timer.
- **Effect:** a dropped `gains` still blocks the next valid one for 1 s; `calibrate` followed by `gains` within 1 s
  (a normal tuning sequence) silently loses the gains (warning only).
- **Fix:** record the time only once the command is queued; either document that the two share the limit or give
  each its own timer (both write the same small settings block).


## ⚪ 32. Secure-MQTT setup breaks the local connection and the examples

- **Where:** `RUNNING.md:516-533` (section 13), `:152-161`, `:399-401`, `:438-440`.
- **What:**
  - The example config defines only `listener 8883`. In Mosquitto 2.x, defining any listener removes the default
    localhost 1883 one, so the robot's own clients must use TLS too. With `MQTT_HOST=127.0.0.1`,
    `ssl.create_default_context()` checks the hostname, so the certificate needs an IP SAN for `127.0.0.1` or the
    connection fails (see #34 for why nobody notices).
  - Every `mosquitto_pub` example has no `-u/-P`, `--cafile` or `-p 8883`, so none work after section 13.
  - The ACL gives the robot and any remote controller the same `robot` login with `readwrite robot/#`.
- **Fix:** add `listener 1883 127.0.0.1` for the robot's own clients (plain text on loopback) and keep 8883 + TLS
  for remote use; show the `mosquitto_pub` flags for a secured broker; suggest a separate remote user with only the
  topics it needs.


## ⚪ 33. `require_https` blocks cached phrases; two different "local" checks

- **Where:** `api_routing_task.py:43`, `:171-173`; `mqtt_client.py:23`.
- **What:** `handle_job` checks `ELEVENLABS_TTS_URL` for every job, including `SAY_JOB` phrases already in the cache
  that send nothing. `require_https` accepts only `localhost` / `127.0.0.1` / `::1`; `mqtt_client._is_local` accepts
  any loopback address (`127.0.0.2`, ...).
- **Effect:** with an `http://` URL, cues that need no request still play the error sound instead. The two modules
  disagree on what counts as local.
- **Fix:** check the URL only right before a request is sent (in `_transcribe` / `_request_response` / `_synthesize`);
  share one `is_local_host()` helper.


## ⚪ 34. Broker login failures are silent

- **Where:** `mqtt_client.py:55`, `main.py:216`, `audio_trigger_task.py:31`.
- **What:** only the behavior tree's client has an `on_connect` that logs a refused connection. The publisher
  (`robot-main`) and the audio client get none, and paho retries in the background without logging.
- **Effect:** a wrong `MQTT_PASSWORD` or certificate problem gives no telemetry, events, wake flag or camera state
  and nothing in the log from those clients.
- **Fix:** give `make_client` a default `on_connect` / `on_connect_fail` that logs the reason code once per failure
  streak.


## Firmware (round 3, on the `Fixes` branch)

- **`I` command:** retries the IMU without moving a servo → `ACK,I` / `NACK,I,NOIMU` / `NACK,I,MODE` (while balancing).
  Both sketches; host tests cover all three replies and recovery without moving. Needed for the #28 fix.
