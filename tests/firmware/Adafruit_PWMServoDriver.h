// PCA9685 stand-in: records every servo pulse the firmware sets, for the test harness.
#pragma once
#include <cstdint>
#include <vector>

struct PwmCall
{
    uint8_t channel;
    uint16_t on;
    uint16_t off;
};
extern std::vector<PwmCall> g_pwm;
extern uint16_t g_chan[16];  // last pulse tick per channel
extern bool g_chanOff[16];   // channel switched fully off (servo limp)

struct Adafruit_PWMServoDriver
{
    explicit Adafruit_PWMServoDriver(uint8_t) {}
    void begin() {}
    void setOscillatorFrequency(uint32_t) {}
    void setPWMFreq(float) {}
    void setPWM(uint8_t ch, uint16_t on, uint16_t off)
    {
        g_pwm.push_back({ch, on, off});
        g_chanOff[ch] = off >= 4096;
        if (off < 4096)
            g_chan[ch] = off;
    }
};
