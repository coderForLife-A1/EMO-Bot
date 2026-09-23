// Minimal Arduino core stand-in so the sketch's logic can be compiled and tested on a PC.
#pragma once
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <math.h>
#include <queue>
#include <string>

#ifndef PI
#define PI 3.14159265358979f
#endif
#define TWO_PI 6.28318530717958f
#define RAD_TO_DEG 57.2957795130823f

struct __FlashStringHelper;
#define F(s) (reinterpret_cast<const __FlashStringHelper *>(s))

// Simulated clock, advanced by the harness (and by delay()).
extern uint32_t g_micros;
inline uint32_t micros() { return g_micros; }
inline uint32_t millis() { return g_micros / 1000; }
void delay(unsigned long ms); // defined in the harness: advances time

inline long map(long x, long inMin, long inMax, long outMin, long outMax)
{
    return (x - inMin) * (outMax - outMin) / (inMax - inMin) + outMin;
}

#define SERIAL_8N1 0x800001c // ESP32 value; unused on the host

struct FakeSerial
{
    std::queue<char> in;
    std::string out;
    void begin(long) {}
    void begin(long, uint32_t, int8_t, int8_t) {} // ESP32: config, RX pin, TX pin
    int available() { return static_cast<int>(in.size()); }
    int read()
    {
        char c = in.front();
        in.pop();
        return static_cast<unsigned char>(c);
    }
    void print(const __FlashStringHelper *s) { out += reinterpret_cast<const char *>(s); }
    void print(const char *s) { out += s; }
    void print(char c) { out += c; }
    void print(long v) { out += std::to_string(v); }
    void print(int v) { out += std::to_string(v); }
    void print(uint8_t v) { out += std::to_string(static_cast<int>(v)); }
    template <class T>
    void println(T v)
    {
        print(v);
        out += "\n";
    }
};
extern FakeSerial Serial;
#define Serial2 Serial // ESP32 second UART (LINK_UART2 1): the same fake port on the host
