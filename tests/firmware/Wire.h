// I2C stand-in: routes MPU6050 register reads to a simulated IMU and models the PCA9685's ALL_LED
// write (used by allOutputsOff) for the test harness.
#pragma once
#include <cstdint>

// Filled by the harness each tick: raw accel X/Z and gyro Y as the MPU6050 would report them.
struct SimImu
{
    bool present = true;
    int16_t ax = 0, az = 16384, gy = 0;
};
extern SimImu g_imu;
extern bool g_chanOff[16];  // defined by the harness (see Adafruit_PWMServoDriver.h)
extern bool g_pcaAllFails;  // harness: make the ALL_LED write fail (tests the per-channel fallback)
extern int g_pcaAllWrites;  // harness: number of successful ALL_LED full-off writes

struct FakeWire
{
    uint8_t addr = 0;
    uint8_t tx[8];
    uint8_t txLen = 0;
    uint8_t buf[14];
    uint8_t len = 0, pos = 0;
    void begin() {}
    void begin(int, int) {} // ESP32: SDA, SCL pins
    void setClock(uint32_t) {}
    void setWireTimeout(uint32_t, bool) {} // AVR
    void setTimeOut(uint16_t) {}           // ESP32
    void beginTransmission(uint8_t a)
    {
        addr = a;
        txLen = 0;
    }
    void write(uint8_t b)
    {
        if (txLen < sizeof(tx))
            tx[txLen++] = b;
    }
    uint8_t endTransmission(bool = true)
    {
        if (addr == 0x68)
            return g_imu.present ? 0 : 2;
        if (addr == 0x40 && txLen == 5 && tx[0] == 0xFA) // ALL_LED_ON_L .. ALL_LED_OFF_H
        {
            if (g_pcaAllFails)
                return 4;
            if (tx[4] & 0x10) // full-off bit reaches every channel
            {
                for (int ch = 0; ch < 16; ch++)
                    g_chanOff[ch] = true;
                g_pcaAllWrites++;
            }
        }
        return 0;
    }
    uint8_t requestFrom(uint8_t a, uint8_t n)
    {
        if (a != 0x68 || !g_imu.present || n > 14)
            return 0;
        const int16_t v[7] = {g_imu.ax, 0, g_imu.az, 0, 0, g_imu.gy, 0};
        for (int i = 0; i < 7; i++)
        {
            buf[2 * i] = static_cast<uint8_t>((v[i] >> 8) & 0xFF);
            buf[2 * i + 1] = static_cast<uint8_t>(v[i] & 0xFF);
        }
        len = n;
        pos = 0;
        return n;
    }
    int read() { return pos < len ? buf[pos++] : 0; }
};
extern FakeWire Wire;
