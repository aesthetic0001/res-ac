#pragma once
#include <stdint.h>

// Estimates only: calibration assumes a power-cycled fan, off, default speed 1.
class FanState {
 public:
  bool calibrated() const { return calibrated_; }
  bool power() const { return calibrated_ && power_; }
  uint8_t speed() const { return power() ? speed_ : 0; }
  void reset() { calibrated_ = false; power_ = false; speed_ = 1; }
  void calibrateOff() { reset(); calibrated_ = true; }
  bool afterSend(uint16_t address, uint8_t command, uint8_t repeats) {
    if (!calibrated_) return false;
    if (address != 0xDE80 || repeats) { reset(); return false; }
    if (command == 0x00) power_ = !power_;
    else if (command == 0x08) { if (power_ && speed_ < 5) ++speed_; }
    else if (command == 0x10) { if (power_ && speed_ > 1) --speed_; }
    else if (command != 0x15 && command != 0x16) { reset(); return false; }
    return true;
  }
 private:
  bool calibrated_ = false;
  bool power_ = false;
  uint8_t speed_ = 1;
};
