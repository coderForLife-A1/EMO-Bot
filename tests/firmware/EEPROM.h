// EEPROM stand-in (1 KB in RAM) so the firmware's saved settings can be tested on a PC.
#pragma once
#include <cstdint>
#include <cstring>

struct FakeEEPROM
{
    uint8_t mem[1024];
    FakeEEPROM() { memset(mem, 0xFF, sizeof(mem)); } // erased AVR EEPROM reads 0xFF
    bool begin(size_t size) { return size <= sizeof(mem); } // ESP32 flash emulation
    int commits = 0;
    bool commit() { return ++commits > 0; }                 // ESP32: persisted on commit
    template <class T>
    T &get(int addr, T &obj)
    {
        memcpy(&obj, mem + addr, sizeof(T));
        return obj;
    }
    template <class T>
    const T &put(int addr, const T &obj)
    {
        memcpy(mem + addr, &obj, sizeof(T));
        return obj;
    }
};
extern FakeEEPROM EEPROM;
