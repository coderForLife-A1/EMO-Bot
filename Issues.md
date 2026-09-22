# EMO-Bot — Known Issues

Code review of `main` @ `b6aa2b2`. API layer (`api_routing_task.py`) out of scope. Line numbers refer to that commit.

Severity: 🔴 **Critical** (safety / robot stops responding) · 🟠 **High** (wrong behaviour) · 🟡 **Medium** · ⚪ **Low**

| # | Severity | Area | Issue |
|---|---|---|---|
| 1 | 🔴 Critical | MQTT / locomotion | `inf` payload kills the MQTT thread (E-stop included) |
| 2 | 🔴 Critical | Serial | `C` (calibrate) outlasts reply timeout → replies shift by one |
| 3 | 🔴 Critical | Safety | `EVT,IMU_FAIL` ignored → servos powered with no balance / fall detection |
| 4 | 🟠 High | Safety | E-stop not prioritised in the serial FIFO |
| 5 | 🟠 High | Behavior tree | `legs_standing` set on queue, not on ACK; `NACK,MODE` unhandled |
| 6 | 🟠 High | Shutdown | Servos not parked on exit |
| 7 | 🟠 High | Conversation | `conversation_active` stuck if the API task crashes |
| 8 | 🟡 Medium | Vision | Posture alert fires on first poor frame after user returns |
| 9 | 🟡 Medium | Vision | Camera read failure never reported or reopened |
| 10 | 🟡 Medium | Concurrency | `SharedState` shared across threads without locks |
| 11 | ⚪ Low | Vision | Face detection runs every frame; result unused |
| 12 | ⚪ Low | Dev env | `numpy<2` has no wheel for Python 3.14 |
| 13 | ⚪ Low | Docs | Nano sketch header points to wrong tuning section |

---

## 🔴 1. `inf` payload kills the MQTT thread

- **Where:** `behavior_tree_module.py:256` (`_clamp_int`), `:281`, `:289`.
- **What:** `int(float("inf"))` and `round(float("inf") * 100)` raise `OverflowError`. Only `IndexError, ValueError` are caught (`:299`).
- **Effect:** exception escapes the paho callback → network thread dies → no further MQTT commands, **E-stop included**.
- **Repro:** publish `walk,inf,0` or `gains,inf,1,1` to `robot/locomotion/cmd`.
- **Fix:** catch `OverflowError`; reject non-finite values (`math.isfinite`) before converting.

## 🔴 2. Calibrate outlasts the reply timeout

- **Where:** `serial_module.py:21` (`REPLY_TIMEOUT_S = 0.3`), `:126`.
- **What:** firmware `C` samples the IMU for ~340 ms before `ACK`. Pi times out at 300 ms and sends the next command.
- **Effect:** the late `ACK,C` is read as the reply to the next command; every later reply is matched to the wrong command (off by one) until reconnect.
- **Fix:** per-command timeout (e.g. 1 s for `C`), and match replies by command letter (`ACK,<cmd>`), discarding mismatches.

## 🔴 3. `EVT,IMU_FAIL` ignored

- **Where:** firmware `emo_nano.ino:374` emits it; `behavior_tree_module.py:304` (`apply_serial_line`) only handles `EVT,FALLEN` / `NACK,TILTED` / `EVT,WATCHDOG`.
- **What:** on IMU loss the firmware keeps servos powered in balance mode; Pi keeps commanding stand/walk.
- **Effect:** no balance and no fall detection while the robot is live.
- **Fix:** treat `EVT,IMU_FAIL` (and `NACK,NOIMU`) as fault → send `O`/`R`, set a fault flag, block Stand/Walk until `READY,IMU`.

## 🟠 4. E-stop not prioritised

- **Where:** `behavior_tree_module.py:59` (`motor_queue`, FIFO, maxsize 200), `:112`.
- **What:** `E` is appended behind already-queued commands; each takes up to 0.3 s round trip.
- **Effect:** E-stop latency grows with queue depth; a full queue drops it.
- **Fix:** separate priority path (flush queue + send `E` immediately), or `PriorityQueue`.

## 🟠 5. `legs_standing` set optimistically

- **Where:** `behavior_tree_module.py:83`, `:231–232`.
- **What:** flag set when `S` is queued, not when `ACK,S` arrives. `NACK,MODE` (and other `S` rejections) never clear it.
- **Effect:** Pi believes the robot is standing and walks/poses a relaxed robot; never retries `S`.
- **Fix:** set on `ACK,S`; clear on any `NACK` to `S`.

## 🟠 6. Servos not parked on shutdown

- **Where:** `main.py:250` (`finally`), `serial_module.py:148` (port closed, nothing sent).
- **What:** exit closes the port without `R`/`O`. Walk stops after the 1 s watchdog; balance mode keeps servos powered until power-off.
- **Fix:** on shutdown send `R` (rest) then `O` (servos off), wait for ACK, then close the port.

## 🟠 7. `conversation_active` can get stuck

- **Where:** set by `audio_trigger_task.py:151` (`wake_flag=1`), cleared only by `api_routing_task.py:214`; read at `behavior_tree_module.py:334`, `:168`.
- **What:** if the API task is dead (optional task, logged and skipped), nobody publishes `wake_flag=0`.
- **Effect:** Conversation branch stays active; posture and walk branches starve.
- **Fix:** timestamp + timeout on the flag, and clear it from the supervisor when the task dies.

## 🟡 8. Posture alert fires too early after absence

- **Where:** `vision_posture_module.py:229–243` (`PostureMonitor.update`).
- **What:** `poor_since` only reset when an alert was already raised. Absent without alert → old `poor_since` survives.
- **Effect:** user returns with one poor frame → `now - poor_since >= confirm_s` immediately → alert.
- **Fix:** reset `poor_since` (and `good_since`) whenever `poor is None`.

## 🟡 9. Camera failure never handled

- **Where:** `main.py:104–106`.
- **What:** `cap.read()` failure → sleep 10 ms, retry forever. No log, no reopen, no state published.
- **Effect:** unplugged/crashed camera = silent vision outage.
- **Fix:** count consecutive failures → log, release and reopen with backoff; publish a vision-down state.

## 🟡 10. `SharedState` without locks

- **Where:** `behavior_tree_module.py:37`.
- **What:** written from the paho thread (MQTT callbacks) and the asyncio tick; compound updates (`legs_standing = walking = False`, `walk_*` + `walk_until`) are not atomic.
- **Effect:** tick can read half-updated state (e.g. new `walk_until` with old speed).
- **Fix:** marshal MQTT callbacks onto the event loop (`loop.call_soon_threadsafe`) or guard with a lock.

## ⚪ 11. Face detection result unused

- **Where:** `vision_posture_module.py:292`; consumed into `SharedState.face_error` (`behavior_tree_module.py:349`) but no behaviour reads it.
- **Effect:** CPU spent per frame on the Pi for nothing.
- **Fix:** use it (head tracking / attention) or run it at a lower rate / disable.

## ⚪ 12. `numpy<2` has no Python 3.14 wheel

- **Where:** `requirements-dev.txt:7`.
- **Effect:** dev install fails on Python 3.14 (source build).
- **Fix:** relax pin to match the MediaPipe version in use, or document the supported Python version.

## ⚪ 13. Wrong tuning-section reference

- **Where:** `firmware/emo_nano/emo_nano.ino:6` ("RUNNING.md, section 10"). Section 10 is systemd; tuning is section 11.
- **Fix:** change to section 11.
