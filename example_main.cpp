
// This code is only for reference purposes. The actual code would be uploaded in the raspberry Pi.

#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>

// PCA9685 servo controller on default I2C address 0x40.
Adafruit_PWMServoDriver pwm = Adafruit_PWMServoDriver(0x40);

static const uint32_t SERIAL_BAUD = 115200;
static const uint8_t JOINT_COUNT = 16;
static const uint16_t SERVO_MIN_TICK = 102; // ~= 0.5ms at 50Hz, 12-bit
static const uint16_t SERVO_MAX_TICK = 512; // ~= 2.5ms at 50Hz, 12-bit

// Per-joint software end-stops (degrees).
static const uint8_t JOINT_MIN_DEG[JOINT_COUNT] = {
    10, 10, 10, 10, 10, 10, 10, 10,
    10, 10, 10, 10, 10, 10, 10, 10};
static const uint8_t JOINT_MAX_DEG[JOINT_COUNT] = {
    170, 170, 170, 170, 170, 170, 170, 170,
    170, 170, 170, 170, 170, 170, 170, 170};

// Map logical joint index to PCA9685 channel index.
static const uint8_t JOINT_TO_CHANNEL[JOINT_COUNT] = {
    0, 1, 2, 3, 4, 5, 6, 7,
    8, 9, 10, 11, 12, 13, 14, 15};

// Fast, non-blocking serial line buffer.
static const uint8_t RX_BUF_SIZE = 32;
char rxBuf[RX_BUF_SIZE];
uint8_t rxLen = 0;

uint16_t angleToTick(uint8_t angleDeg)
{
    return map(angleDeg, 0, 180, SERVO_MIN_TICK, SERVO_MAX_TICK);
}

void sendAck(uint8_t joint, int requestedDeg, uint8_t appliedDeg)
{
    Serial.print(F("ACK,"));
    Serial.print(joint);
    Serial.print(',');
    Serial.print(requestedDeg);
    Serial.print(',');
    Serial.println(appliedDeg);
}

void sendNack(const __FlashStringHelper *reason)
{
    Serial.print(F("NACK,"));
    Serial.println(reason);
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

void processCommand(char *line)
{
    // Expected command: J,<joint>,<angle>
    // Example: J,2,90
    char *token0 = strtok(line, ",");
    char *token1 = strtok(nullptr, ",");
    char *token2 = strtok(nullptr, ",");
    char *token3 = strtok(nullptr, ",");

    if (token0 == nullptr || token1 == nullptr || token2 == nullptr || token3 != nullptr)
    {
        sendNack(F("FORMAT"));
        return;
    }

    if (token0[0] != 'J' || token0[1] != '\0')
    {
        sendNack(F("CMD"));
        return;
    }

    long jointLong = -1;
    long angleLong = -1;
    if (!parseIntSafe(token1, &jointLong) || !parseIntSafe(token2, &angleLong))
    {
        sendNack(F("PARSE"));
        return;
    }

    if (jointLong < 0 || jointLong >= JOINT_COUNT)
    {
        sendNack(F("JOINT"));
        return;
    }

    const uint8_t joint = static_cast<uint8_t>(jointLong);
    const int requestedDeg = static_cast<int>(angleLong);

    int clampedDeg = requestedDeg;
    if (clampedDeg < JOINT_MIN_DEG[joint])
    {
        clampedDeg = JOINT_MIN_DEG[joint];
    }
    if (clampedDeg > JOINT_MAX_DEG[joint])
    {
        clampedDeg = JOINT_MAX_DEG[joint];
    }

    const uint8_t appliedDeg = static_cast<uint8_t>(clampedDeg);
    const uint8_t channel = JOINT_TO_CHANNEL[joint];
    const uint16_t tick = angleToTick(appliedDeg);

    pwm.setPWM(channel, 0, tick);
    sendAck(joint, requestedDeg, appliedDeg);
}

void handleSerialInput()
{
    while (Serial.available() > 0)
    {
        const char c = static_cast<char>(Serial.read());

        if (c == '\r')
        {
            continue;
        }

        if (c == '\n')
        {
            rxBuf[rxLen] = '\0';
            if (rxLen > 0)
            {
                processCommand(rxBuf);
            }
            rxLen = 0;
            continue;
        }

        if (rxLen < (RX_BUF_SIZE - 1))
        {
            rxBuf[rxLen++] = c;
        }
        else
        {
            // Overflow protection: reset frame and report error.
            rxLen = 0;
            sendNack(F("OVERFLOW"));
        }
    }
}

void setup()
{
    Serial.begin(SERIAL_BAUD);
    Wire.begin();

    pwm.begin();
    pwm.setPWMFreq(50); // 50Hz servo update rate

    Serial.println(F("READY"));
}

void loop()
{
    // Keep loop tight and non-blocking for responsive command handling.
    handleSerialInput();
}