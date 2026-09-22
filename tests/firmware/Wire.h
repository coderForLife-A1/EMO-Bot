// I2C stand-in: routes MPU6050 register reads to a simulated IMU provided by the harness.
#pragma once
#include <cstdint>

// Filled by the harness each tick: raw accel X/Z and gyro Y as the MPU6050 would report them.
struct SimImu
{
    bool present = true;
    int16_t ax = 0, az = 16384, gy = 0;
};
extern SimImu g_imu;

struct FakeWire
{
    uint8_t addr = 0;
    uint8_t buf[14];
    uint8_t len = 0, pos = 0;
    void begin() {}
    void begin(int, int) {} // ESP32: SDA, SCL pins
    void setClock(uint32_t) {}
    void setWireTimeout(uint32_t, bool) {} // AVR
    void setTimeOut(uint16_t) {}           // ESP32
    void beginTransmission(uint8_t a) { addr = a; }
    void write(uint8_t) {}
    uint8_t endTransmission(bool = true) { return (addr == 0x68 && !g_imu.present) ? 2 : 0; }
    uint8_t requestFrom(uint8_t a, uint8_t n)
    {
        if (a != 0x68 || !g_imu.present || n != 14)
            return 0;
        const int16_t v[7] = {g_imu.ax, 0, g_imu.az, 0, 0, g_imu.gy, 0};
        for (int i = 0; i < 7; i++)
        {
            buf[2 * i] = static_cast<uint8_t>((v[i] >> 8) & 0xFF);
            buf[2 * i + 1] = static_cast<uint8_t>(v[i] & 0xFF);
        }
        len = 14;
        pos = 0;
        return 14;
    }
    int read() { return pos < len ? buf[pos++] : 0; }
};
extern FakeWire Wire;
