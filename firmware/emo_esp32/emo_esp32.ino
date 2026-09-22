// EMO-Bot biped firmware (ESP32): IMU pitch balance + walking gait on 4 leg servos.
// Port of firmware/emo_nano/emo_nano.ino with the same serial protocol, so the Pi side is unchanged.
//
// Hardware: ESP32 DevKit (ESP32-WROOM-32, 3.3 V logic). PCA9685 (0x40) drives the servos; MPU6050
//   (0x68) on the same I2C bus (SDA = GPIO21, SCL = GPIO22; change I2C_SDA_PIN / I2C_SCL_PIN below).
//   Channel 0 = left hip, 1 = right hip, 2 = left knee, 3 = right knee.
//   Mount the MPU6050 flat on the pelvis with its X arrow pointing forward.
//   Pi link: the board's USB port (Serial) by default; see LINK_UART2 below for the Pi's GPIO UART.
// Flash it with arduino-cli (FQBN esp32:esp32:esp32; RUNNING.md, section 4), set it up with the robot held
// in the air (section 5) and tune it (section 11).
//
// A 100 Hz control loop reads the IMU (complementary filter), runs a PID on torso pitch that
// offsets both hips (hip strategy), adds a sinusoidal gait, slew-limits every joint and writes the
// PCA9685. If the robot tips past FALL_DEG, all servos are switched off.
//
// Protocol (ASCII, one command per line, 115200 baud):
//   S                   stand and balance                         -> ACK,S  (ACK,S,NOIMU without IMU)
//   W,<speed>,<turn>    walk, -100..100 each; resend at least every second, or it stops
//                                                                 -> ACK,W,<speed>,<turn>
//   G,1                 gesture 1: knee bob                       -> ACK,G,1
//   O                   relax: all servos off (not latched)       -> ACK,O
//   C                   calibrate level (robot upright + still, not balancing) -> ACK,C,<offset x100>
//   K,<kp>,<ki>,<kd>    balance gains x100, saved to EEPROM       -> ACK,K,<kp>,<ki>,<kd>
//   T,<0|1>             telemetry off/on (20 Hz: T,<pitch x10>,<rate x10>,<correction x10>,<mode>)
//   J,<joint>,<angle>   raw servo angle, for setup; leaves balance mode -> ACK,<joint>,<requested>,<applied>
//   E / R               emergency stop (latched, all off) / release    -> ACK,E / ACK,R
//   P                   ping                                      -> ACK,P
// Errors: NACK,<cmd>,<reason>: <cmd> is the command letter it answers ('?' if unknown, e.g. for an
//         overflowed line), <reason> is FORMAT|CMD|PARSE|JOINT|ESTOP|MODE|TILTED|NOIMU|OVERFLOW.
//         The letter lets the Pi match every reply to its command even if one arrives late.
// Events: EVT,FALLEN  EVT,WATCHDOG (walk stopped: no W for 1 s)
//         EVT,IMU_FAIL (IMU stopped answering: walking stops, balance and fall detection are off)
// Boot:   READY,IMU or READY,NOIMU

#include <Wire.h>
#include <EEPROM.h>
#include <Adafruit_PWMServoDriver.h>
#include <math.h>

Adafruit_PWMServoDriver pwm = Adafruit_PWMServoDriver(0x40);

// ---------------------------------------------------------------- board
// Serial link to the Pi. 0 = USB (Serial; CP2102/CH340 on the DevKit, /dev/ttyUSB0 on the Pi).
// 1 = the Pi's GPIO UART (/dev/serial0) on Serial2, GPIO16 (RX) / GPIO17 (TX): both sides are 3.3 V,
// so no level shifter, and the ESP32's boot messages stay off the Pi's line. The pins are set
// explicitly: core 3.x defaults Serial2 to GPIO4/25. WROVER modules use GPIO16/17 for PSRAM: pick others.
#define LINK_UART2 0
#if LINK_UART2
#define LINK Serial2
static const int8_t LINK_RX_PIN = 16;
static const int8_t LINK_TX_PIN = 17;
#else
#define LINK Serial
static const int8_t LINK_RX_PIN = -1; // -1 = UART0 default pins (USB bridge)
static const int8_t LINK_TX_PIN = -1;
#endif
static const uint32_t SERIAL_BAUD = 115200;
static const int I2C_SDA_PIN = 21;
static const int I2C_SCL_PIN = 22;
static const uint16_t I2C_TIMEOUT_MS = 3; // never hang the control loop on a glitched I2C bus
static const size_t EEPROM_SIZE = 64;     // flash-emulated EEPROM; must hold Settings

// ---------------------------------------------------------------- servo hardware
static const uint8_t JOINT_COUNT = 16;
static const uint16_t SERVO_MIN_TICK = 102; // ~= 0.5ms at 50Hz, 12-bit
static const uint16_t SERVO_MAX_TICK = 512; // ~= 2.5ms at 50Hz, 12-bit
// PCA9685 internal oscillators vary (~23-27 MHz). Measure the 50 Hz output with a scope or
// logic analyser and adjust this value if servo angles are consistently off.
static const uint32_t PCA9685_OSC_HZ = 27000000;

// Raw servo end-stops (servo degrees) and joint -> PCA9685 channel map.
static const uint8_t JOINT_MIN_DEG[JOINT_COUNT] = {
    10, 10, 10, 10, 10, 10, 10, 10,
    10, 10, 10, 10, 10, 10, 10, 10};
static const uint8_t JOINT_MAX_DEG[JOINT_COUNT] = {
    170, 170, 170, 170, 170, 170, 170, 170,
    170, 170, 170, 170, 170, 170, 170, 170};
static const uint8_t JOINT_TO_CHANNEL[JOINT_COUNT] = {
    0, 1, 2, 3, 4, 5, 6, 7,
    8, 9, 10, 11, 12, 13, 14, 15};

// ---------------------------------------------------------------- legs
// Leg angles are "logical" degrees: 0 = straight leg under the hip,
// hip > 0 = thigh swung forward, knee > 0 = knee bent (foot moves back).
// servo degrees = 90 + LEG_TRIM_DEG + LEG_DIR * logical angle
enum
{
    L_HIP = 0,
    R_HIP = 1,
    L_KNEE = 2,
    R_KNEE = 3,
    LEG_COUNT = 4
};
static const uint8_t LEG_JOINT[LEG_COUNT] = {0, 1, 2, 3};
static const int8_t LEG_DIR[LEG_COUNT] = {1, -1, 1, -1};  // mirrored mounting left/right: verify!
static const int8_t LEG_TRIM_DEG[LEG_COUNT] = {0, 0, 0, 0}; // makes logical 0 a straight leg
static const float LEG_MIN_DEG[LEG_COUNT] = {-30, -30, 0, 0};
static const float LEG_MAX_DEG[LEG_COUNT] = {45, 45, 75, 75};

// The feet have no ankle joint, so with a foot flat on the ground:
//     torso pitch = ground tilt + hip - knee
// Equal hip and knee angles keep the torso upright. A slight crouch leaves the servos room to correct
// both ways. Adjust for your foot geometry so the centre of mass sits over the middle of the feet.
static const float STAND_HIP_DEG = 15;
static const float STAND_KNEE_DEG = 15;

static const float GAIT_HZ = 1.0;        // steps per second per leg
static const float HIP_SWING_DEG = 12;   // stride amplitude at speed 100
static const float KNEE_LIFT_DEG = 12;   // foot clearance during swing
static const float GAIT_RAMP_PER_S = 1.5; // how fast stride changes (fraction of full per second)
static const float SLEW_DEG_PER_S = 250; // per-joint speed limit (MG996R ~ 350 deg/s)
static const float BOB_DEG = 10;
static const uint16_t BOB_MS = 600;
static const uint16_t WALK_TIMEOUT_MS = 1000;

// ---------------------------------------------------------------- IMU + balance
static const uint8_t MPU_ADDR = 0x68;
static const float PITCH_SIGN = 1.0;      // flip to -1 if pitch reads negative when leaning forward
static const float FILTER_ALPHA = 0.98;   // complementary filter: gyro weight
static const float GYRO_LSB_PER_DPS = 65.5; // matches the +/-500 deg/s range set in imuInit()
static const float FALL_DEG = 45;
static const uint16_t FALL_MS = 250;
static const float STAND_MAX_TILT_DEG = 30; // refuse S when lying down
static const float BALANCE_LIMIT_DEG = 20;  // max hip correction
static const uint32_t LOOP_US = 10000;      // 100 Hz
static const uint16_t TELEMETRY_MS = 50;
static const float D_FILTER_HZ = 10;        // low-pass on the D term: rejects vibration and step noise

struct Settings
{
    uint16_t magic;
    float pitchOffset; // degrees, measured by C
    float kp;          // hip degrees per degree of pitch
    float ki;          // per degree-second
    float kd;          // per degree/second of pitch rate
};
static const uint16_t SETTINGS_MAGIC = 0xB1D5; // change when the struct layout changes
static_assert(sizeof(Settings) <= EEPROM_SIZE, "EEPROM_SIZE too small for Settings");
Settings settings;

// ---------------------------------------------------------------- state
enum Mode : char
{
    MODE_OFF = 'O',
    MODE_MANUAL = 'M',
    MODE_BALANCE = 'B',
    MODE_FALLEN = 'F'
};
Mode mode = MODE_OFF;
bool estopLatched = false;

bool imuOk = false;
bool imuLost = false; // IMU answered at boot, then failed: walking refused until it is re-initialised
bool filterReady = false;
uint8_t imuFailCount = 0;
float gyroBiasRaw = 0;
float sensorPitch = 0; // filter state, sensor frame, degrees
float pitchDeg = 0;    // torso pitch, forward positive, degrees
float pitchRate = 0;   // deg/s, forward positive

float pidInteg = 0;
float lastCorrection = 0;
float rateFiltered = 0; // low-passed pitch rate for the D term

float legAngle[LEG_COUNT];
uint16_t lastLegTick[LEG_COUNT] = {0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF};

float gaitPhase = 0;
float stride[2] = {0, 0}; // current left/right stride, -1..1
float speedTarget = 0;    // -1..1
float turnTarget = 0;     // -1..1
uint32_t lastWalkMs = 0;
bool bobActive = false;
uint32_t bobStartMs = 0;

bool fallTiming = false;
uint32_t fallStartMs = 0;
bool telemetryOn = false;
uint32_t lastTelemetryMs = 0;
uint32_t lastTickUs = 0;

// Fast, non-blocking serial line buffer.
static const uint8_t RX_BUF_SIZE = 32;
char rxBuf[RX_BUF_SIZE];
uint8_t rxLen = 0;
bool rxDiscarding = false; // true after an overflow until the end of that line

// ---------------------------------------------------------------- helpers
float clampf(float v, float lo, float hi)
{
    return v < lo ? lo : (v > hi ? hi : v);
}

uint16_t angleToTick(uint8_t angleDeg)
{
    return map(angleDeg, 0, 180, SERVO_MIN_TICK, SERVO_MAX_TICK);
}

uint8_t clampServoDeg(uint8_t joint, long deg)
{
    if (deg < JOINT_MIN_DEG[joint])
    {
        deg = JOINT_MIN_DEG[joint];
    }
    if (deg > JOINT_MAX_DEG[joint])
    {
        deg = JOINT_MAX_DEG[joint];
    }
    return static_cast<uint8_t>(deg);
}

char currentCmd = '?'; // command letter being processed, echoed in NACKs

void sendNack(const __FlashStringHelper *reason)
{
    LINK.print(F("NACK,"));
    LINK.print(currentCmd);
    LINK.print(',');
    LINK.println(reason);
}

bool parseIntSafe(const char *s, long *out)
{
    if (s == nullptr || *s == '\0')
    {
        return false;
    }
    char *endPtr = nullptr;
    long value = strtol(s, &endPtr, 10);
    if (endPtr == s || *endPtr != '\0')
    {
        return false;
    }
    *out = value;
    return true;
}

// Split on commas in place without collapsing empty fields (unlike strtok).
// Returns the number of fields, or maxFields + 1 if there are too many.
uint8_t splitFields(char *line, char **fields, uint8_t maxFields)
{
    uint8_t count = 0;
    char *start = line;
    while (true)
    {
        if (count == maxFields)
        {
            return maxFields + 1;
        }
        fields[count++] = start;
        char *comma = strchr(start, ',');
        if (comma == nullptr)
        {
            return count;
        }
        *comma = '\0';
        start = comma + 1;
    }
}

// ---------------------------------------------------------------- settings
void loadSettings()
{
    EEPROM.get(0, settings);
    const bool valid = settings.magic == SETTINGS_MAGIC && settings.kp == settings.kp &&
                       settings.ki == settings.ki && settings.kd == settings.kd &&
                       settings.pitchOffset == settings.pitchOffset; // x == x is false for NaN
    if (!valid)
    {
        settings.magic = SETTINGS_MAGIC;
        settings.pitchOffset = 0;
        settings.kp = 0.8;
        settings.ki = 3.0;
        settings.kd = 0.03;
    }
}

void saveSettings()
{
    settings.magic = SETTINGS_MAGIC;
    EEPROM.put(0, settings);
    EEPROM.commit(); // ESP32 EEPROM is a RAM copy of a flash sector until committed
}

// ---------------------------------------------------------------- servos
void allOutputsOff()
{
    for (uint8_t ch = 0; ch < 16; ch++)
    {
        pwm.setPWM(ch, 0, 4096); // full-off bit: no pulses, servo goes limp
    }
    for (uint8_t i = 0; i < LEG_COUNT; i++)
    {
        lastLegTick[i] = 0xFFFF;
    }
}

void writeLeg(uint8_t leg)
{
    // Map to PCA9685 ticks directly from the float angle: ~0.44 deg resolution instead of 1 deg,
    // which avoids a small quantisation limit cycle in the balance loop.
    const uint8_t joint = LEG_JOINT[leg];
    const float servoDeg = clampf(90 + LEG_TRIM_DEG[leg] + LEG_DIR[leg] * legAngle[leg],
                                  JOINT_MIN_DEG[joint], JOINT_MAX_DEG[joint]);
    const uint16_t tick = static_cast<uint16_t>(
        lround(SERVO_MIN_TICK + servoDeg * (SERVO_MAX_TICK - SERVO_MIN_TICK) / 180.0));
    if (tick != lastLegTick[leg])
    {
        pwm.setPWM(JOINT_TO_CHANNEL[joint], 0, tick);
        lastLegTick[leg] = tick;
    }
}

void stopGait()
{
    speedTarget = turnTarget = 0;
    stride[0] = stride[1] = 0;
    gaitPhase = 0;
    bobActive = false;
}

// ---------------------------------------------------------------- IMU
bool imuWrite(uint8_t reg, uint8_t value)
{
    Wire.beginTransmission(MPU_ADDR);
    Wire.write(reg);
    Wire.write(value);
    return Wire.endTransmission() == 0;
}

bool imuReadRaw(int16_t *ax, int16_t *az, int16_t *gy)
{
    Wire.beginTransmission(MPU_ADDR);
    Wire.write(0x3B); // ACCEL_XOUT_H
    if (Wire.endTransmission(false) != 0)
    {
        return false;
    }
    if (Wire.requestFrom(MPU_ADDR, static_cast<uint8_t>(14)) != 14)
    {
        return false;
    }
    int16_t v[7];
    for (uint8_t i = 0; i < 7; i++)
    {
        const uint8_t hi = Wire.read();
        v[i] = static_cast<int16_t>((hi << 8) | Wire.read());
    }
    *ax = v[0]; // v[1] = ay, v[3] = temperature, v[4] = gx, v[6] = gz
    *az = v[2];
    *gy = v[5];
    return true;
}

float accelPitchDeg(int16_t ax, int16_t az)
{
    // X forward, Z up: leaning forward makes the X axis read -g*sin(pitch).
    return atan2(-static_cast<float>(ax), static_cast<float>(az)) * RAD_TO_DEG;
}

// Average the resting gyro (and optionally the level pitch). The robot must be still.
bool imuCalibrate(bool level)
{
    float gyroSum = 0;
    float pitchSum = 0;
    const uint8_t samples = 100;
    for (uint8_t i = 0; i < samples; i++)
    {
        int16_t ax, az, gy;
        if (!imuReadRaw(&ax, &az, &gy))
        {
            return false;
        }
        gyroSum += gy;
        pitchSum += accelPitchDeg(ax, az);
        delay(3);
    }
    gyroBiasRaw = gyroSum / samples;
    if (level)
    {
        settings.pitchOffset = PITCH_SIGN * (pitchSum / samples);
    }
    filterReady = false;
    return true;
}

bool imuInit()
{
    if (!imuWrite(0x6B, 0x00)) // PWR_MGMT_1: wake up
    {
        return false;
    }
    imuWrite(0x1A, 0x03); // CONFIG: 44 Hz low-pass filter to reject servo vibration
    imuWrite(0x1B, 0x08); // GYRO_CONFIG: +/-500 deg/s (a fall can exceed 250) -> 65.5 LSB per deg/s
    imuWrite(0x1C, 0x00); // ACCEL_CONFIG: +/-2 g
    delay(50);
    return imuCalibrate(false);
}

void imuUpdate(float dt)
{
    int16_t ax, az, gy;
    if (!imuReadRaw(&ax, &az, &gy))
    {
        if (++imuFailCount >= 10)
        {
            imuOk = false;
            imuLost = true;
            stopGait(); // never keep walking blind; the Pi relaxes the servos on this event
            LINK.println(F("EVT,IMU_FAIL"));
        }
        return;
    }
    imuFailCount = 0;

    const float rate = (gy - gyroBiasRaw) / GYRO_LSB_PER_DPS;
    const float accPitch = accelPitchDeg(ax, az);
    if (!filterReady)
    {
        sensorPitch = accPitch;
        filterReady = true;
    }
    else
    {
        sensorPitch = FILTER_ALPHA * (sensorPitch + rate * dt) + (1 - FILTER_ALPHA) * accPitch;
    }
    pitchDeg = PITCH_SIGN * sensorPitch - settings.pitchOffset;
    pitchRate = PITCH_SIGN * rate;
}

// ---------------------------------------------------------------- control
float balanceCorrection(float dt)
{
    if (!imuOk)
    {
        pidInteg = 0;
        return 0;
    }
    // Anti-windup: the integral alone can never ask for more than the output limit.
    const float iLimit = settings.ki > 0.001 ? BALANCE_LIMIT_DEG / settings.ki : 0;
    pidInteg = clampf(pidInteg + pitchDeg * dt, -iLimit, iLimit);
    const float a = dt / (dt + 1.0 / (TWO_PI * D_FILTER_HZ));
    rateFiltered += (pitchRate - rateFiltered) * a;
    const float u = settings.kp * pitchDeg + settings.ki * pidInteg + settings.kd * rateFiltered;
    return clampf(u, -BALANCE_LIMIT_DEG, BALANCE_LIMIT_DEG);
}

void computeLegTargets(float dt, float target[LEG_COUNT])
{
    // Leaning forward -> extend both hips, which rotates the torso back (feet are planted).
    lastCorrection = balanceCorrection(dt);

    const float strideTarget[2] = {clampf(speedTarget + turnTarget, -1, 1),
                                   clampf(speedTarget - turnTarget, -1, 1)};
    const float rampStep = GAIT_RAMP_PER_S * dt;
    for (uint8_t s = 0; s < 2; s++)
    {
        stride[s] += clampf(strideTarget[s] - stride[s], -rampStep, rampStep);
    }

    const bool moving = fabs(stride[0]) > 0.01 || fabs(stride[1]) > 0.01;
    if (moving)
    {
        gaitPhase += TWO_PI * GAIT_HZ * dt;
        if (gaitPhase >= TWO_PI)
        {
            gaitPhase -= TWO_PI;
        }
    }
    else
    {
        gaitPhase = 0;
    }

    float bob = 0;
    if (bobActive)
    {
        const uint32_t t = millis() - bobStartMs;
        if (t >= BOB_MS)
        {
            bobActive = false;
        }
        else
        {
            bob = BOB_DEG * sin(PI * t / BOB_MS);
        }
    }

    for (uint8_t s = 0; s < 2; s++)
    {
        const float phi = gaitPhase + (s == 0 ? 0 : PI); // legs half a cycle apart
        const float swing = HIP_SWING_DEG * stride[s] * sin(phi);
        // Lift the knee while the leg swings (cos > 0), never during stance.
        const float c = cos(phi);
        const float lift = KNEE_LIFT_DEG * clampf(fabs(stride[s]), 0, 1) * (c > 0 ? c : 0);
        target[s == 0 ? L_HIP : R_HIP] = STAND_HIP_DEG - lastCorrection + swing + bob; // hip = knee: torso stays level
        target[s == 0 ? L_KNEE : R_KNEE] = STAND_KNEE_DEG + lift + bob;
    }
}

void driveLegs(const float target[LEG_COUNT], float dt)
{
    const float maxStep = SLEW_DEG_PER_S * dt;
    for (uint8_t i = 0; i < LEG_COUNT; i++)
    {
        legAngle[i] += clampf(target[i] - legAngle[i], -maxStep, maxStep);
        legAngle[i] = clampf(legAngle[i], LEG_MIN_DEG[i], LEG_MAX_DEG[i]);
        writeLeg(i);
    }
}

void enterFallen()
{
    allOutputsOff();
    stopGait();
    mode = MODE_FALLEN;
    LINK.println(F("EVT,FALLEN"));
}

void controlTick(float dt)
{
    if (imuOk)
    {
        imuUpdate(dt);
    }

    if (mode == MODE_BALANCE)
    {
        if (imuOk && fabs(pitchDeg) > FALL_DEG)
        {
            if (!fallTiming)
            {
                fallTiming = true;
                fallStartMs = millis();
            }
            else if (millis() - fallStartMs >= FALL_MS)
            {
                fallTiming = false;
                enterFallen();
                return;
            }
        }
        else
        {
            fallTiming = false;
        }

        if ((speedTarget != 0 || turnTarget != 0) && millis() - lastWalkMs > WALK_TIMEOUT_MS)
        {
            speedTarget = turnTarget = 0;
            LINK.println(F("EVT,WATCHDOG"));
        }

        float target[LEG_COUNT];
        computeLegTargets(dt, target);
        driveLegs(target, dt);
    }

    if (telemetryOn && millis() - lastTelemetryMs >= TELEMETRY_MS)
    {
        lastTelemetryMs = millis();
        LINK.print(F("T,"));
        LINK.print(lround(pitchDeg * 10));
        LINK.print(',');
        LINK.print(lround(pitchRate * 10));
        LINK.print(',');
        LINK.print(lround(lastCorrection * 10));
        LINK.print(',');
        LINK.println(static_cast<char>(mode));
    }
}

// ---------------------------------------------------------------- commands
void cmdStand()
{
    if (estopLatched)
    {
        sendNack(F("ESTOP"));
        return;
    }
    if (imuLost && mode != MODE_BALANCE)
    {
        // The IMU worked at boot and then stopped answering: try to bring it back (e.g. reseated cable).
        imuOk = imuInit();
        imuLost = !imuOk;
        imuFailCount = 0;
        if (imuLost)
        {
            sendNack(F("NOIMU"));
            return;
        }
        imuUpdate(LOOP_US * 1e-6); // fresh pitch (the filter re-seeds from the accelerometer)
    }
    if (imuOk && fabs(pitchDeg) > STAND_MAX_TILT_DEG)
    {
        sendNack(F("TILTED"));
        return;
    }
    if (mode != MODE_BALANCE)
    {
        stopGait();
        pidInteg = 0;
        rateFiltered = 0;
        fallTiming = false;
        legAngle[L_HIP] = legAngle[R_HIP] = STAND_HIP_DEG;
        legAngle[L_KNEE] = legAngle[R_KNEE] = STAND_KNEE_DEG;
        for (uint8_t i = 0; i < LEG_COUNT; i++)
        {
            lastLegTick[i] = 0xFFFF;
            writeLeg(i);
        }
        mode = MODE_BALANCE;
    }
    LINK.println(imuOk ? F("ACK,S") : F("ACK,S,NOIMU"));
}

void cmdWalk(char **fields)
{
    long speed = 0;
    long turn = 0;
    if (!parseIntSafe(fields[1], &speed) || !parseIntSafe(fields[2], &turn))
    {
        sendNack(F("PARSE"));
        return;
    }
    if (estopLatched)
    {
        sendNack(F("ESTOP"));
        return;
    }
    if (mode != MODE_BALANCE)
    {
        sendNack(F("MODE"));
        return;
    }
    if (imuLost) // never walk blind after the IMU failed (booting without one is allowed for bench tests)
    {
        sendNack(F("NOIMU"));
        return;
    }
    speed = speed < -100 ? -100 : (speed > 100 ? 100 : speed);
    turn = turn < -100 ? -100 : (turn > 100 ? 100 : turn);
    speedTarget = speed / 100.0;
    turnTarget = turn / 100.0;
    lastWalkMs = millis();
    LINK.print(F("ACK,W,"));
    LINK.print(speed);
    LINK.print(',');
    LINK.println(turn);
}

void cmdGesture(char **fields)
{
    long id = 0;
    if (!parseIntSafe(fields[1], &id) || id != 1)
    {
        sendNack(F("PARSE"));
        return;
    }
    if (mode != MODE_BALANCE)
    {
        sendNack(F("MODE"));
        return;
    }
    bobActive = true;
    bobStartMs = millis();
    LINK.println(F("ACK,G,1"));
}

void cmdCalibrate()
{
    if (mode == MODE_BALANCE)
    {
        sendNack(F("MODE"));
        return;
    }
    if (!imuOk)
    {
        sendNack(F("NOIMU"));
        return;
    }
    if (!imuCalibrate(true))
    {
        sendNack(F("NOIMU"));
        return;
    }
    saveSettings();
    LINK.print(F("ACK,C,"));
    LINK.println(lround(settings.pitchOffset * 100));
}

void cmdGains(char **fields)
{
    long kp, ki, kd;
    if (!parseIntSafe(fields[1], &kp) || !parseIntSafe(fields[2], &ki) || !parseIntSafe(fields[3], &kd) ||
        kp < 0 || ki < 0 || kd < 0)
    {
        sendNack(F("PARSE"));
        return;
    }
    settings.kp = kp / 100.0;
    settings.ki = ki / 100.0;
    settings.kd = kd / 100.0;
    pidInteg = 0;
    saveSettings();
    LINK.print(F("ACK,K,"));
    LINK.print(kp);
    LINK.print(',');
    LINK.print(ki);
    LINK.print(',');
    LINK.println(kd);
}

void cmdTelemetry(char **fields)
{
    long on = 0;
    if (!parseIntSafe(fields[1], &on) || (on != 0 && on != 1))
    {
        sendNack(F("PARSE"));
        return;
    }
    telemetryOn = on == 1;
    LINK.print(F("ACK,T,"));
    LINK.println(on);
}

void cmdJoint(char **fields)
{
    long jointLong = -1;
    long angleLong = -1;
    if (!parseIntSafe(fields[1], &jointLong) || !parseIntSafe(fields[2], &angleLong))
    {
        sendNack(F("PARSE"));
        return;
    }
    if (jointLong < 0 || jointLong >= JOINT_COUNT)
    {
        sendNack(F("JOINT"));
        return;
    }
    if (estopLatched)
    {
        sendNack(F("ESTOP"));
        return;
    }

    if (mode != MODE_MANUAL) // raw joint control takes over from the balance loop
    {
        stopGait();
        mode = MODE_MANUAL;
        for (uint8_t i = 0; i < LEG_COUNT; i++)
        {
            lastLegTick[i] = 0xFFFF;
        }
    }

    const uint8_t joint = static_cast<uint8_t>(jointLong);
    const uint8_t appliedDeg = clampServoDeg(joint, angleLong); // clamp as long: no 16-bit wrap
    pwm.setPWM(JOINT_TO_CHANNEL[joint], 0, angleToTick(appliedDeg));
    LINK.print(F("ACK,"));
    LINK.print(joint);
    LINK.print(',');
    LINK.print(angleLong);
    LINK.print(',');
    LINK.println(appliedDeg);
}

void processCommand(char *line)
{
    char *fields[4];
    const uint8_t count = splitFields(line, fields, 4);
    currentCmd = (fields[0][0] != '\0' && fields[0][1] == '\0') ? fields[0][0] : '?';
    if (count > 4 || fields[0][0] == '\0' || fields[0][1] != '\0')
    {
        sendNack(count > 4 ? F("FORMAT") : F("CMD"));
        return;
    }

    const char cmd = fields[0][0];
    uint8_t expected;
    switch (cmd)
    {
    case 'S':
    case 'O':
    case 'C':
    case 'E':
    case 'R':
    case 'P':
        expected = 1;
        break;
    case 'G':
    case 'T':
        expected = 2;
        break;
    case 'W':
    case 'J':
        expected = 3;
        break;
    case 'K':
        expected = 4;
        break;
    default:
        sendNack(F("CMD"));
        return;
    }
    if (count != expected)
    {
        sendNack(F("FORMAT"));
        return;
    }

    switch (cmd)
    {
    case 'S':
        cmdStand();
        break;
    case 'W':
        cmdWalk(fields);
        break;
    case 'G':
        cmdGesture(fields);
        break;
    case 'O':
        allOutputsOff();
        stopGait();
        mode = MODE_OFF;
        LINK.println(F("ACK,O"));
        break;
    case 'C':
        cmdCalibrate();
        break;
    case 'K':
        cmdGains(fields);
        break;
    case 'T':
        cmdTelemetry(fields);
        break;
    case 'J':
        cmdJoint(fields);
        break;
    case 'E':
        allOutputsOff();
        stopGait();
        estopLatched = true;
        mode = MODE_OFF;
        LINK.println(F("ACK,E"));
        break;
    case 'R':
        estopLatched = false; // servos stay off until S
        LINK.println(F("ACK,R"));
        break;
    case 'P':
        LINK.println(F("ACK,P"));
        break;
    }
}

void handleSerialInput()
{
    while (LINK.available() > 0)
    {
        const char c = static_cast<char>(LINK.read());

        if (c == '\r')
        {
            continue;
        }

        if (c == '\n')
        {
            rxBuf[rxLen] = '\0';
            if (rxLen > 0 && !rxDiscarding)
            {
                processCommand(rxBuf);
            }
            rxLen = 0;
            rxDiscarding = false;
            continue;
        }

        if (rxDiscarding)
        {
            continue;
        }

        if (rxLen < (RX_BUF_SIZE - 1))
        {
            rxBuf[rxLen++] = c;
        }
        else
        {
            // Overflow protection: drop the rest of this line so its tail is never executed.
            rxLen = 0;
            rxDiscarding = true;
            currentCmd = '?';
            sendNack(F("OVERFLOW"));
        }
    }
}

// ---------------------------------------------------------------- Arduino entry points
void setup()
{
    LINK.begin(SERIAL_BAUD, SERIAL_8N1, LINK_RX_PIN, LINK_TX_PIN);
    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
    Wire.setClock(400000);
    Wire.setTimeOut(I2C_TIMEOUT_MS);
    EEPROM.begin(EEPROM_SIZE);

    pwm.begin();
    pwm.setOscillatorFrequency(PCA9685_OSC_HZ);
    pwm.setPWMFreq(50); // 50Hz servo update rate

    loadSettings();
    imuOk = imuInit();

    LINK.println(imuOk ? F("READY,IMU") : F("READY,NOIMU"));
    lastTickUs = micros();
}

void loop()
{
    handleSerialInput();

    const uint32_t now = micros();
    if (now - lastTickUs >= LOOP_US)
    {
        float dt = (now - lastTickUs) * 1e-6;
        if (dt > 0.05)
        {
            dt = 0.05; // after a stall, don't integrate a huge step
        }
        lastTickUs = now;
        controlTick(dt);
    }
}
