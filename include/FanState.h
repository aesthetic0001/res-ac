#pragma once
#include <stdint.h>

// Estimates only: calibration assumes the fan is off with saved speed 1.
// The fan retains its speed across power toggles and power cycles.
class FanState {
 public:
  bool calibrated() const { return calibrated_; }
  bool power() const { return calibrated_ && power_; }
  uint8_t speed() const { return power() ? speed_ : 0; }
  // Positive = speed-up presses, negative = speed-down presses; no wrapping.
  static int8_t speedSteps(uint8_t current, uint8_t target) {
    if (current < 1 || current > 5 || target < 1 || target > 5) return 0;
    return static_cast<int8_t>(target) - static_cast<int8_t>(current);
  }
  void reset() { calibrated_ = false; power_ = false; speed_ = 1; }
  void calibrateOff() { reset(); calibrated_ = true; }
  bool afterSend(uint16_t address, uint8_t command, uint8_t repeats) {
    if (!calibrated_) return false;
    if (address != 0xDE80 || repeats) { reset(); return false; }
    if (command == 0x00) power_ = !power_;
    else if (command == 0x16) { if (power_ && speed_ < 5) ++speed_; }
    else if (command == 0x14) { if (power_ && speed_ > 1) --speed_; }
    else if (command != 0x15) { reset(); return false; }
    return true;
  }
 private:
  bool calibrated_ = false;
  bool power_ = false;
  uint8_t speed_ = 1;
};
