// Host-side tests for the robot firmware. Run via tests/test_firmware.py, or:
//   g++ -std=c++11 -I tests/firmware tests/firmware/harness.cpp -o harness && ./harness all
// This builds firmware/emo_esp32/emo_esp32.ino. For the Nano sketch add:
//   -DFIRMWARE_SKETCH='"../../firmware/emo_nano/emo_nano.ino"'
//
// Closed-loop scenarios use a simple planar model of the biped:
//   - each servo follows its command with a first-order lag (TAU_S), limp servos hold still
//   - feet are flat with no ankle joint, so torso pitch = ground tilt + mean(hip - knee) of the legs
//     on the ground (a leg with its knee lifted during swing doesn't count)
//   - the IMU sees that pitch through its mounting sign/offset, with gyro bias and noise
// This checks signs, filtering, stability and safety logic. It is not a substitute for tuning
// on the real robot (servo backlash, compliance and foot slip are not modelled).
#include "Adafruit_PWMServoDriver.h"
#include "Arduino.h"
#include "EEPROM.h"
#include "Wire.h"

FakeSerial Serial;
FakeWire Wire;
FakeEEPROM EEPROM;
SimImu g_imu;
uint32_t g_micros = 0;
std::vector<PwmCall> g_pwm;
uint16_t g_chan[16];
bool g_chanOff[16];
bool g_pcaAllFails = false;
int g_pcaAllWrites = 0;

#ifndef FIRMWARE_SKETCH
#define FIRMWARE_SKETCH "../../firmware/emo_esp32/emo_esp32.ino"
#endif
#include FIRMWARE_SKETCH

// ------------------------------------------------------------------ plant model
#ifndef SIM_TAU_S
#define SIM_TAU_S 0.06f
#endif
static const float TAU_S = SIM_TAU_S; // servo lag (MG996R under load: ~0.05-0.15 s)
struct Plant
{
    float groundTilt = 0; // deg, forward positive
    float tiltTarget = 0; // groundTilt moves toward this at tiltRate (smooth, like a real slope/fall)
    float tiltRate = 40;  // deg/s
    float gyroFiltered = 0;
    float imuSign = 1;    // -1 simulates an IMU mounted backwards
    float imuMountDeg = 0;
    float joint[LEG_COUNT] = {STAND_HIP_DEG, STAND_HIP_DEG, STAND_KNEE_DEG, STAND_KNEE_DEG};
    float pitch = 0, prevPitch = 0;
    uint32_t noise = 12345;
} P;

static float logicalFromTick(uint8_t leg, uint16_t tick)
{
    const float servoDeg = (tick - SERVO_MIN_TICK) * 180.0f / (SERVO_MAX_TICK - SERVO_MIN_TICK);
    return (servoDeg - 90 - LEG_TRIM_DEG[leg]) / LEG_DIR[leg];
}

static int noise(int amplitude)
{
    P.noise = P.noise * 1103515245u + 12345u;
    return static_cast<int>((P.noise >> 16) % (2 * amplitude + 1)) - amplitude;
}

static void setTilt(float target, float ratePerS)
{
    P.tiltTarget = target;
    P.tiltRate = ratePerS;
}

static void updateImu(float dt)
{
    // The MPU6050's 44 Hz digital low-pass filter averages the rate between samples.
    const float rawRate = dt > 0 ? (P.pitch - P.prevPitch) / dt : 0;
    P.gyroFiltered += (rawRate - P.gyroFiltered) * dt / (0.0036f + dt);
    const float rate = P.gyroFiltered;
    const float sensorPitch = P.imuSign * P.pitch + P.imuMountDeg;
    const float r = sensorPitch / RAD_TO_DEG;
    g_imu.ax = static_cast<int16_t>(-sin(r) * 16384 + noise(150));
    g_imu.az = static_cast<int16_t>(cos(r) * 16384 + noise(150));
    const float gy = P.imuSign * rate * GYRO_LSB_PER_DPS + 40 + noise(25); // +40 LSB gyro bias
    g_imu.gy = static_cast<int16_t>(gy > 32767 ? 32767 : (gy < -32768 ? -32768 : gy)); // sensor saturates
}

static void plantStep(float dt)
{
    const float maxTilt = P.tiltRate * dt;
    const float d = P.tiltTarget - P.groundTilt;
    P.groundTilt += d > maxTilt ? maxTilt : (d < -maxTilt ? -maxTilt : d);
    for (uint8_t i = 0; i < LEG_COUNT; i++)
    {
        const uint8_t ch = JOINT_TO_CHANNEL[LEG_JOINT[i]];
        if (!g_chanOff[ch] && g_chan[ch] != 0)
            P.joint[i] += (logicalFromTick(i, g_chan[ch]) - P.joint[i]) * dt / TAU_S;
    }
    float sum = 0;
    int contacts = 0;
    for (uint8_t s = 0; s < 2; s++)
    {
        const float hip = P.joint[s == 0 ? L_HIP : R_HIP];
        const float knee = P.joint[s == 0 ? L_KNEE : R_KNEE];
        const float otherKnee = P.joint[s == 0 ? R_KNEE : L_KNEE];
        if (knee <= otherKnee + 1.0f) // the lower (straighter) leg carries the weight
        {
            sum += hip - knee;
            contacts++;
        }
    }
    P.prevPitch = P.pitch;
    P.pitch = P.groundTilt + (contacts ? sum / contacts : 0);
    updateImu(dt);
}

// Advance simulated time in 1 ms steps, running the firmware loop and the plant.
static void run(float seconds)
{
    const int steps = static_cast<int>(seconds * 1000);
    for (int i = 0; i < steps; i++)
    {
        g_micros += 1000;
        loop();
        plantStep(0.001f);
    }
}

void delay(unsigned long ms) // firmware calibration waits: time passes, IMU keeps reporting
{
    for (unsigned long i = 0; i < ms; i++)
    {
        g_micros += 1000;
        plantStep(0.001f);
    }
}

static void send(const std::string &line)
{
    for (char c : line)
        Serial.in.push(c);
    Serial.in.push('\n');
    handleSerialInput();
}

static int failures = 0;
static void expect(bool ok, const char *what, const std::string &detail = "")
{
    std::printf("%s %s%s%s\n", ok ? "ok  " : "FAIL", what, detail.empty() ? "" : "  -> ", detail.c_str());
    if (!ok)
        failures++;
}

static std::string take()
{
    std::string out = Serial.out;
    Serial.out.clear();
    return out;
}

static bool has(const std::string &s, const char *needle) { return s.find(needle) != std::string::npos; }

static void boot()
{
    P.groundTilt = P.tiltTarget; // start at rest on the initial slope
    P.pitch = P.prevPitch = P.groundTilt;
    updateImu(0.01f);
    setup();
    run(0.05f);
}

static std::string fmt(const char *f, double a, double b = 0)
{
    char buf[128];
    std::snprintf(buf, sizeof(buf), f, a, b);
    return buf;
}

// ------------------------------------------------------------------ scenarios
static void scenarioProtocol()
{
    boot();
    const std::string banner = take();
    expect(has(banner, "READY,IMU"), "boot banner reports IMU", banner);

    struct Case
    {
        const char *name, *in, *out;
    } cases[] = {
        {"walk refused before standing", "W,50,0", "NACK,W,MODE\n"},
        {"gesture refused before standing", "G,1", "NACK,G,MODE\n"},
        {"raw joint move (setup mode)", "J,2,90", "ACK,2,90,90\n"},
        {"clamp high", "J,1,200", "ACK,1,200,170\n"},
        {"huge angle clamps, no 16-bit wrap", "J,0,40000", "ACK,0,40000,170\n"},
        {"joint out of range", "J,16,90", "NACK,J,JOINT\n"},
        {"unknown command", "X,1,90", "NACK,X,CMD\n"},
        {"non-numeric", "J,a,90", "NACK,J,PARSE\n"},
        {"too many fields", "K,1,2,3,4", "NACK,K,FORMAT\n"},
        {"wrong field count", "W,50", "NACK,W,FORMAT\n"},
        {"empty field rejected", "J,,1,90", "NACK,J,FORMAT\n"},
        {"negative gain rejected", "K,-1,0,0", "NACK,K,PARSE\n"},
        {"gain above 1000 rejected", "K,100001,0,0", "NACK,K,PARSE\n"},
        {"overflowing gain rejected", "K,99999999999999999999,0,0", "NACK,K,PARSE\n"},
        {"unknown gesture", "G,7", "NACK,G,PARSE\n"},
        {"ping", "P", "ACK,P\n"},
        {"stand", "S", "ACK,S\n"},
        {"walk clamps to +/-100", "W,250,-300", "ACK,W,100,-100\n"},
        {"calibration refused while balancing", "C", "NACK,C,MODE\n"},
        {"relax", "O", "ACK,O\n"},
        {"estop", "E", "ACK,E\n"},
        {"stand refused while latched", "S", "NACK,S,ESTOP\n"},
        {"release", "R", "ACK,R\n"},
        {"stand after release", "S", "ACK,S\n"},
        {"gesture", "G,1", "ACK,G,1\n"},
    };
    for (const Case &c : cases)
    {
        take();
        send(c.in);
        const std::string got = take();
        expect(got == c.out, c.name, got == c.out ? "" : "got " + got);
    }

    take();
    send(std::string(32, 'x') + "J,0,170");
    expect(take() == "NACK,?,OVERFLOW\n", "overflowed line is dropped whole (tail not executed)");

    send("E");
    int offWrites = 0;
    for (int ch = 0; ch < 16; ch++)
        offWrites += g_chanOff[ch];
    expect(offWrites == 16, "estop switches all 16 outputs off");
    expect(g_pcaAllWrites > 0, "estop uses one ALL_LED write");

    send("R");
    send("S");
    g_pcaAllFails = true; // ALL_LED write NACKed: per-channel fallback
    send("E");
    offWrites = 0;
    for (int ch = 0; ch < 16; ch++)
        offWrites += g_chanOff[ch];
    expect(offWrites == 16, "estop falls back to per-channel writes if the ALL_LED write fails");
    g_pcaAllFails = false;
    expect(angleToTick(10) == 124 && angleToTick(90) == 307 && angleToTick(170) == 489, "servo tick mapping");
}

static void scenarioImuSign()
{
    setTilt(10, 40); // the whole robot leans 10 deg forward
    boot();
    run(1.0f);
    expect(fabs(pitchDeg - 10) < 1.0f, "IMU reads forward lean as positive pitch", fmt("pitch=%.2f", pitchDeg));
}

static void scenarioSlopeRejection()
{
    boot();
    send("S");
    run(1.0f);
    setTilt(8, 40); // table tilts 8 deg forward over 0.2 s
    float peak = 0;
    for (int i = 0; i < 150; i++)
    {
        run(0.01f);
        peak = fabs(pitchDeg) > peak ? fabs(pitchDeg) : peak;
    }
    const float after = fabs(pitchDeg);
    run(1.0f);
    float corrSum = 0;
    for (int i = 0; i < 50; i++) // average over 0.5 s
    {
        run(0.01f);
        corrSum += lastCorrection;
    }
    const float corrAvg = corrSum / 50;
    expect(after < 1.5f, "torso back within 1.5 deg of upright 1.5 s after an 8 deg slope",
           fmt("pitch=%.2f peak=%.2f", after, peak));
    expect(fabs(pitchDeg) < 0.7f, "settles level (integral removes the offset)", fmt("pitch=%.2f", pitchDeg));
    expect(fabs(corrAvg - 8) < 1.0f, "hips carry the 8 deg correction", fmt("correction=%.2f", corrAvg));
}

static void scenarioWalking()
{
    boot();
    send("S");
    run(0.5f);
    float hipMin = 99, hipMax = -99, peak = 0;
    for (int i = 0; i < 50; i++) // 5 s of walking, heartbeat every 100 ms
    {
        send("W,70,0");
        run(0.1f);
        hipMin = legAngle[L_HIP] < hipMin ? legAngle[L_HIP] : hipMin;
        hipMax = legAngle[L_HIP] > hipMax ? legAngle[L_HIP] : hipMax;
        peak = fabs(pitchDeg) > peak ? fabs(pitchDeg) : peak;
    }
    const std::string log = take();
    expect(mode == MODE_BALANCE && !has(log, "FALLEN"), "walks 5 s without falling");
    expect(hipMax - hipMin > 12, "gait swings the hips", fmt("hip range=%.1f deg", hipMax - hipMin));
    expect(peak < 12, "balance keeps torso rocking bounded", fmt("peak pitch=%.1f deg", peak));

    send("W,0,0");
    run(1.5f);
    expect(fabs(stride[0]) < 0.01f && fabs(stride[1]) < 0.01f, "stops smoothly on W,0,0");

    send("W,0,80"); // turn in place: legs stride in opposite directions
    run(0.8f);
    expect(stride[0] > 0.5f && stride[1] < -0.5f, "turning uses opposite strides",
           fmt("L=%.2f R=%.2f", stride[0], stride[1]));
}

static void scenarioWatchdog()
{
    boot();
    send("S");
    send("W,80,0");
    take();
    run(1.3f); // no heartbeat
    expect(has(take(), "EVT,WATCHDOG"), "walk stops if the Pi goes quiet for 1 s");
    run(1.0f);
    expect(fabs(stride[0]) < 0.01f, "stride ramped to zero after watchdog");
    expect(mode == MODE_BALANCE, "keeps standing and balancing after watchdog");
}

static void scenarioFall()
{
    boot();
    send("S");
    run(0.5f);
    take();
    setTilt(70, 175); // knocked over: tips to 70 deg in 0.4 s
    run(0.3f);
    expect(mode == MODE_BALANCE, "no trigger before passing the tilt limit", fmt("pitch=%.1f", pitchDeg));
    run(0.5f);
    expect(mode == MODE_FALLEN && has(take(), "EVT,FALLEN"), "fall detected and reported");
    int off = 0;
    for (int ch = 0; ch < 4; ch++)
        off += g_chanOff[ch];
    expect(off == 4, "leg servos switched off after a fall");
    send("S");
    expect(has(take(), "NACK,S,TILTED"), "refuses to stand while lying down");
    setTilt(0, 100); // picked up
    run(0.5f);
    send("S");
    expect(has(take(), "ACK,S"), "stands again once upright");
}

static void scenarioWrongSignIsSafe()
{
    P.imuSign = -1; // IMU mounted backwards: the PID now pushes the wrong way
    boot();
    send("S");
    run(0.3f);
    setTilt(3, 40);
    run(3.0f);
    expect(mode == MODE_FALLEN || fabs(pitchDeg) >= BALANCE_LIMIT_DEG - 1,
           "wrong IMU sign runs away to the correction limit or trips the fall detector",
           std::string("mode=") + static_cast<char>(mode) + fmt(" pitch=%.1f", pitchDeg));
    expect(fabs(lastCorrection) <= BALANCE_LIMIT_DEG + 0.01f, "correction never exceeds its limit");
}

static void scenarioCalibration()
{
    P.imuMountDeg = 5; // IMU glued on 5 deg nose-down
    boot();
    run(0.5f);
    expect(fabs(pitchDeg - 5) < 1, "uncalibrated pitch shows the mounting error", fmt("pitch=%.2f", pitchDeg));
    take();
    send("C");
    const std::string reply = take();
    run(0.5f);
    expect(has(reply, "ACK,C,") && fabs(pitchDeg) < 0.5f, "C zeroes the mounting offset",
           reply + fmt(" pitch=%.2f", pitchDeg));
    send("K,120,250,5");
    expect(take() == "ACK,K,120,250,5\n", "gains accepted");
    const int commits = EEPROM.commits;
    send("K,120,250,5");
    expect(take() == "ACK,K,120,250,5\n" && EEPROM.commits == commits, "unchanged gains don't rewrite flash");
    settings.kp = settings.pitchOffset = 0;
    loadSettings();
    expect(fabs(settings.pitchOffset - 5) < 0.5f && fabs(settings.kp - 1.2f) < 1e-4f,
           "offset and gains persist in EEPROM", fmt("offset=%.2f kp=%.2f", settings.pitchOffset, settings.kp));
}

static void scenarioNoImu()
{
    g_imu.present = false;
    boot();
    expect(has(take(), "READY,NOIMU"), "boot reports missing IMU");
    send("S");
    expect(take() == "ACK,S,NOIMU\n", "stands without balance when IMU is missing");
    send("W,50,0");
    run(1.0f);
    expect(stride[0] > 0.3f && mode == MODE_BALANCE, "open-loop walking still works");
    send("O");
    take();
    send("C");
    expect(take() == "NACK,C,NOIMU\n", "calibration needs the IMU");
    g_imu.present = true; // IMU plugged in after boot
    send("S");
    expect(take() == "ACK,S\n" && imuOk && !imuLost, "S picks up an IMU connected after boot");
}

static void scenarioImuFail()
{
    boot();
    send("S");
    send("W,60,0");
    run(0.5f);
    take();
    g_imu.present = false; // I2C cable falls out
    for (int i = 0; i < 20; i++)
    {
        send("W,60,0");
        run(0.05f);
    }
    const std::string out = take();
    expect(has(out, "EVT,IMU_FAIL"), "IMU loss is reported");
    expect(has(out, "NACK,W,NOIMU"), "walk heartbeats are refused after IMU loss");
    expect(!imuOk && speedTarget == 0 && fabs(stride[0]) < 0.05f, "walking stops when the IMU is lost",
           fmt("stride=%.2f", stride[0]));

    send("O");
    send("S");
    expect(has(take(), "NACK,S,NOIMU"), "won't stand again while the IMU is still missing");
    g_imu.present = true; // cable reseated
    send("S");
    run(0.3f);
    const std::string again = take();
    expect(has(again, "ACK,S\n") && imuOk && mode == MODE_BALANCE, "S re-initialises the IMU and stands", again);
    send("W,40,0");
    expect(has(take(), "ACK,W,40,0"), "walking allowed again after recovery");
}

static void scenarioTelemetry()
{
    setTilt(2, 40);
    boot();
    send("S");
    send("T,1");
    take();
    run(1.0f);
    const std::string t = take();
    int lines = 0;
    for (size_t p = t.find("T,"); p != std::string::npos; p = t.find("T,", p + 1))
        lines++;
    expect(lines >= 18 && lines <= 21, "telemetry streams at 20 Hz", fmt("%.0f lines/s", lines));
    expect(has(t, ",B\n"), "telemetry reports balance mode");
}

struct Scenario
{
    const char *name;
    void (*fn)();
};
static const Scenario SCENARIOS[] = {
    {"protocol", scenarioProtocol},     {"imu_sign", scenarioImuSign},   {"slope", scenarioSlopeRejection},
    {"walking", scenarioWalking},       {"watchdog", scenarioWatchdog},  {"fall", scenarioFall},
    {"wrong_sign", scenarioWrongSignIsSafe}, {"calibration", scenarioCalibration}, {"no_imu", scenarioNoImu},
    {"imu_fail", scenarioImuFail}, {"telemetry", scenarioTelemetry},
};

// "serve" mode for tools/sim_nano.py: line-oriented control over stdin/stdout.
//   @boot            power-on (runs setup)
//   @t <ms>          advance simulated time
//   @tilt <deg> <deg/s>  move the ground/robot tilt (e.g. 70 175 = knocked over)
//   @state           print the simulated body state
//   anything else    a line received on the Nano's serial port
// After each input, everything the firmware printed is written out, followed by "@@".
static void serve()
{
    char line[256];
    while (std::fgets(line, sizeof(line), stdin))
    {
        std::string cmd(line);
        while (!cmd.empty() && (cmd.back() == '\n' || cmd.back() == '\r'))
            cmd.pop_back();
        if (cmd == "@boot")
            boot();
        else if (cmd.compare(0, 3, "@t ") == 0)
            run(std::atoi(cmd.c_str() + 3) / 1000.0f);
        else if (cmd.compare(0, 6, "@tilt ") == 0)
        {
            float target = 0, rate = 40;
            std::sscanf(cmd.c_str() + 6, "%f %f", &target, &rate);
            setTilt(target, rate);
        }
        else if (cmd == "@state")
            std::printf("#sim tilt=%.1f pitch=%.2f est=%.2f mode=%c hipL=%.1f kneeL=%.1f hipR=%.1f kneeR=%.1f\n",
                        P.groundTilt, P.pitch, pitchDeg, static_cast<char>(mode), P.joint[L_HIP], P.joint[L_KNEE],
                        P.joint[R_HIP], P.joint[R_KNEE]);
        else
        {
            for (char c : cmd)
                Serial.in.push(c);
            Serial.in.push('\n');
        }
        std::fputs(take().c_str(), stdout);
        std::fputs("@@\n", stdout);
        std::fflush(stdout);
    }
}

int main(int argc, char **argv)
{
    const std::string which = argc > 1 ? argv[1] : "all";
    if (which == "serve")
    {
        serve();
        return 0;
    }
    if (which == "list")
    {
        for (const Scenario &s : SCENARIOS)
            std::printf("%s\n", s.name);
        return 0;
    }
    if (which == "all") // each scenario needs fresh firmware globals: run each in its own process
    {
        int failed = 0;
        for (const Scenario &s : SCENARIOS)
        {
            std::printf("== %s\n", s.name);
            std::fflush(stdout);
            failed += std::system((std::string("\"") + argv[0] + "\" " + s.name).c_str()) != 0;
        }
        std::printf("%d scenario(s) failed\n", failed);
        return failed ? 1 : 0;
    }
    for (const Scenario &s : SCENARIOS)
    {
        if (which == s.name)
        {
            s.fn();
            std::printf("%d failure(s)\n", failures);
            return failures ? 1 : 0;
        }
    }
    std::printf("unknown scenario %s\n", which.c_str());
    return 2;
}
