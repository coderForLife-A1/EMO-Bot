# EMO-Bot — Known Issues

Code review of `main` @ `b6aa2b2`. API layer (`api_routing_task.py`) out of scope. Line numbers refer to that commit.

**Status: all 13 issues are fixed.** Each section ends with a *Resolution* note: what changed and which test covers it. Run `pytest` to check them (109 tests, including 11 firmware scenarios run against both the ESP32 and the Nano firmware, compiled for the PC). The firmware fixes are in both sketches (`firmware/emo_esp32`, `firmware/emo_nano`).

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
