// VL53L0X stand-in (Pololu library API subset used by the ESP32 sketch): a simulated time-of-flight sensor
// ranging continuously at the period the firmware asks for.
#pragma once
#include "Arduino.h"
#include "Wire.h"

// Set by the harness.
struct SimTof
{
    bool present = true;  // false = not answering on I2C
    bool halted = false;  // answers I2C but stopped ranging (e.g. reset itself into standby)
    uint16_t mm = 300;    // what it measures; 8190 = no target in range
};
extern SimTof g_tof;

class VL53L0X
{
  public:
    enum regAddr
    {
        RESULT_INTERRUPT_STATUS = 0x13
    };
    uint8_t last_status = 0; // 0 = last I2C transfer succeeded

    void setBus(FakeWire *) {}
    void setTimeout(uint16_t) {}
    bool init(bool = true)
    {
        started = false;
        g_tof.halted = false;
        return g_tof.present;
    }
    void startContinuous(uint32_t periodMs = 0)
    {
        period = periodMs;
        started = true;
        readyAt = millis() + period;
    }
    uint8_t readReg(uint8_t)
    {
        if (!g_tof.present)
        {
            last_status = 2;
            return 0;
        }
        last_status = 0;
        return (started && !g_tof.halted && millis() >= readyAt) ? 0x04 : 0x00; // 0x04 = new sample ready
    }
    uint16_t readRangeContinuousMillimeters()
    {
        if (!g_tof.present || !started || g_tof.halted)
        {
            last_status = g_tof.present ? 0 : 2;
            timeout = true;
            return 65535;
        }
        last_status = 0;
        readyAt = millis() + period;
        return g_tof.mm;
    }
    bool timeoutOccurred()
    {
        const bool t = timeout;
        timeout = false;
        return t;
    }

  private:
    bool started = false;
    bool timeout = false;
    uint32_t period = 0;
    uint32_t readyAt = 0;
};
