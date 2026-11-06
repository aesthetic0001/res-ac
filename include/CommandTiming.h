#pragma once

// Diagnostic builds only: no RAM allocation, timer, or serial output by default.
#ifndef RESAC_COMMAND_TIMING
#define RESAC_COMMAND_TIMING 0
#endif

#if RESAC_COMMAND_TIMING
#include <Arduino.h>

struct CommandTiming {
  enum Event : uint8_t { IRQ = 1, ENTER, UART_BEGIN, UART_END, ACTION_BEGIN, ACTION_END, COMPLETE };
  enum Tag : uint8_t { OTHER = 0, COMMAND, SAFETY, RESULT, DIAGNOSTIC, MAILBOX, ATOMIC_PUBLICATION };
  struct Row { uint32_t us; uint8_t event, tag; } __attribute__((packed));
  static const uint8_t capacity = 16;
  Row rows[capacity];
  volatile bool armed = false, irqValid = false;
  volatile uint32_t irqUs = 0;
  bool active = false;
  uint8_t count = 0;
  uint16_t dropped = 0;
  uint32_t idHash = 0;

  void arm() {
    armed = false;
    active = irqValid = false;
    count = 0; dropped = 0; idHash = 0;
    armed = true;
  }
  void irq() {
    if (armed && !irqValid) { irqUs = micros(); irqValid = true; }
  }
  void record(Event event, Tag tag = OTHER) {
    if (!active) return;
    if (count == capacity) { if (dropped != 0xffff) ++dropped; return; }
    rows[count++] = {static_cast<uint32_t>(micros()), event, tag};
  }
  void begin() {
    if (!armed) return;
    armed = false; active = true;
    if (irqValid) rows[count++] = {irqUs, IRQ, OTHER};
    record(ENTER);
  }
  void bind(const char *id) {
    if (!active) return;
    idHash = 2166136261UL;
    for (; *id; ++id) { idHash ^= static_cast<uint8_t>(*id); idHash *= 16777619UL; }
  }
  void finish() { record(COMPLETE); active = false; }
  void stop() { armed = false; active = false; }
  static Tag tag(const char *key) {
    if (strcmp(key, "resac_command") == 0) return COMMAND;
    if (strcmp(key, "resac_fan_control") == 0) return SAFETY;
    if (strcmp(key, "resac_command_result") == 0) return RESULT;
    if (strcmp(key, "resac_command_diagnostic") == 0) return DIAGNOSTIC;
    return OTHER;
  }
};
#endif
