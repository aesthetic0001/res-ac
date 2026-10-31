#include <Arduino.h>
#include <Bridge.h>
#include <DHT.h>
#include <stdlib.h>
#include <string.h>
#include <FanState.h>

// Software carrier leaves the Bridge UART, millis() and DHT11 pin available.
#define IR_SEND_PIN 9
#define IR_SEND_DUTY_CYCLE_PERCENT 50
#define DISABLE_CODE_FOR_RECEIVER
#define NO_LED_FEEDBACK_CODE
#include <IRremote.hpp>

#define DHTPIN 3
#define DHTTYPE DHT11

namespace {
DHT dht(DHTPIN, DHTTYPE);
const unsigned long DEFAULT_INTERVAL_MS = 10000;
unsigned long intervalMs = DEFAULT_INTERVAL_MS;
unsigned long lastSampleMs = 0;
unsigned long sequence = 0;
unsigned long lastCommandCheckMs = 0;
char lastCommandId[37] = {0};
char lastCommandReply[128] = {0};
FanState fan;
const char *fanControlSource = "reset";
char fanSession[33] = {0};
unsigned long fanRevision = 0;
unsigned long lastFanHeartbeatMs = 0;
unsigned long lastFanPublishMs = 0;
char fanHeartbeat[16] = {0};

bool validEpoch(const char *epoch);

void publishFanState() {
  char frame[96];
  snprintf(frame, sizeof(frame), "v2,%s,%u,%u,%u,%lu,%s", fanSession[0] ? fanSession : "none",
           fan.calibrated(), fan.power(), fan.speed(), fanRevision, fanControlSource);
  Bridge.put("resac_fan_state", frame);
  lastFanPublishMs = millis();
}

void invalidateFan() {
  fan.reset();
  fanControlSource = "reset";
  fanSession[0] = '\0';
  ++fanRevision;
  Bridge.put("resac_fan_session", "");
  publishFanState();
  Serial.println(F("Calibration required: restart fan to factory position: off; confirm with calibrate off"));
}

void updateFan() {
  char invalidate[4] = {0};
  Bridge.get("resac_fan_invalidate", invalidate, sizeof(invalidate) - 1);
  if (strcmp(invalidate, "1") == 0) {
    Bridge.put("resac_fan_invalidate", "");
    invalidateFan();
  }
  char heartbeat[16] = {0};
  Bridge.get("resac_fan_heartbeat", heartbeat, sizeof(heartbeat) - 1);
  if (heartbeat[0] && strcmp(heartbeat, fanHeartbeat) != 0) {
    strcpy(fanHeartbeat, heartbeat);
    lastFanHeartbeatMs = millis();
  }
  if (fan.calibrated() && static_cast<unsigned long>(millis() - lastFanHeartbeatMs) > 10000) invalidateFan();
  if (!fanSession[0]) {
    char session[33] = {0};
    Bridge.get("resac_fan_session", session, sizeof(session) - 1);
    if (validEpoch(session)) { strcpy(fanSession, session); ++fanRevision; publishFanState(); }
  }
  if (static_cast<unsigned long>(millis() - lastFanPublishMs) >= 1000) publishFanState();
}

bool parseNumber(const char *text, unsigned long limit, unsigned long &value, uint8_t base = 10) {
  if (!text || !*text || *text == '-' || *text == '+' || *text == ' ') return false;
  char *end = nullptr;
  value = strtoul(text, &end, base);
  return *end == '\0' && value <= limit;
}

bool sendFanIR(uint16_t address, uint8_t command, uint8_t repeats, const char *source = "manual") {
  if (!fan.calibrated()) {
    Serial.println(F("IR blocked: calibration required; restart fan to factory position: off"));
    return false;
  }
  // Modern IRremote sends address/command LSB first and adds the complement byte.
  // 0xDE80 + 0x08 is speed up; do not pass ESPHome's combined 0xF708 command.
  IrSender.sendNEC(address, command, repeats);
  fanControlSource = source;
  Serial.print(F("IR transmitted: NEC address=0x")); Serial.print(address, HEX);
  Serial.print(F(" command=0x")); Serial.println(command, HEX);
  if (!fan.afterSend(address, command, repeats)) invalidateFan();
  else { ++fanRevision; publishFanState(); }
  return true;
}

bool setFanLevel(uint8_t target, bool automatic, uint8_t &presses) {
  presses = 0;
  if (!fan.calibrated() || target > 5) return false;
  const char *source = automatic ? "automatic" : "manual";
  if (target == 0) {
    if (fan.power()) { if (!sendFanIR(0xDE80, 0x00, 0, source)) return false; ++presses; }
    return true;
  }
  if (!fan.power()) {
    if (!sendFanIR(0xDE80, 0x00, 0, source)) return false;
    ++presses;
    delay(150);
  }
  const int8_t route = FanState::shortestRoute(fan.speed(), target);
  const uint8_t count = route < 0 ? -route : route;
  for (uint8_t i = 0; i < count; ++i) {
    if (!sendFanIR(0xDE80, route < 0 ? 0x10 : 0x08, 0, source)) return false;
    ++presses;
    if (i + 1 < count) delay(150);
  }
  return true;
}

bool validCommandId(const char *id) {
  if (strlen(id) != 36) return false;
  for (uint8_t i = 0; i < 36; ++i) {
    if (i == 8 || i == 13 || i == 18 || i == 23) {
      if (id[i] != '-') return false;
    } else if (!((id[i] >= '0' && id[i] <= '9') ||
                 (id[i] >= 'a' && id[i] <= 'f'))) return false;
  }
  return true;
}

void processCommand() {
  char frame[128] = {0};
  Bridge.get("resac_command", frame, sizeof(frame) - 1);
  // One atomic request; v1 ping remains compatible with the original agent.
  const bool control = strncmp(frame, "v3,", 3) == 0;
  const bool levelControl = strncmp(frame, "v4,", 3) == 0;
  if (!control && !levelControl && strncmp(frame, "v1,", 3) != 0) return;
  char *id = frame + 3;
  char *separator = strchr(id, ',');
  if (!separator) return;
  *separator = '\0';
  if (!validCommandId(id)) return;
  unsigned long address = 0, command = 0, repeats = 0;
  bool calibrate = false;
  bool automatic = false;
  char *session = nullptr;
  if (control || levelControl) {
    char *save = nullptr;
    char *kind = strtok_r(separator + 1, ",", &save);
    if (!kind) return;
    calibrate = strcmp(kind, "calibrate") == 0;
    automatic = strcmp(kind, "auto_level") == 0;
    if (levelControl ? (!automatic && strcmp(kind, "level") != 0) : (!calibrate && strcmp(kind, "nec") != 0)) return;
    session = strtok_r(nullptr, ",", &save);
    if (!session || !validEpoch(session)) return;
    if (levelControl) {
      if (!parseNumber(strtok_r(nullptr, ",", &save), 5, command)) return;
    } else if (!calibrate && (!parseNumber(strtok_r(nullptr, ",", &save), 65535, address) ||
        !parseNumber(strtok_r(nullptr, ",", &save), 255, command) ||
        !parseNumber(strtok_r(nullptr, ",", &save), 3, repeats))) return;
    if (strtok_r(nullptr, ",", &save)) return;
  } else if (strcmp(separator + 1, "ping") != 0) return;
  if (strcmp(id, lastCommandId) != 0) {
    if (control || levelControl) {
      if (!fanSession[0] || strcmp(session, fanSession) != 0 ||
          !fanHeartbeat[0] || static_cast<unsigned long>(millis() - lastFanHeartbeatMs) > 10000 ||
          (!calibrate && !fan.calibrated())) {
        snprintf(lastCommandReply, sizeof(lastCommandReply), "v3,%s,rejected,%s", id, session);
      } else if (calibrate) {
        fan.calibrateOff(); fanControlSource = "calibration"; ++fanRevision; publishFanState();
        snprintf(lastCommandReply, sizeof(lastCommandReply), "v3,%s,calibrated,%s", id, session);
        Serial.println(F("Fan calibrated: estimated off; default speed 1"));
      } else if (levelControl) {
        uint8_t presses;
        if (setFanLevel(command, automatic, presses))
          snprintf(lastCommandReply, sizeof(lastCommandReply), "v4,%s,level_set,%s,%lu,%u,%lu", id, session, command, presses, millis());
        else snprintf(lastCommandReply, sizeof(lastCommandReply), "v3,%s,rejected,%s", id, session);
      } else {
        sendFanIR(address, command, repeats);
        snprintf(lastCommandReply, sizeof(lastCommandReply), "v3,%s,ir_sent,%s,%lu,%lu,%lu,%lu",
                 id, session, address, command, repeats, millis());
      }
    } else {
      snprintf(lastCommandReply, sizeof(lastCommandReply), "v1,%s,pong,%lu", id, millis());
      Serial.println(F("Ping handled by Arduino; replying pong"));
    }
    strcpy(lastCommandId, id);
  }
  // A repeated delivery returns the original response without executing again.
  Bridge.put("resac_command_result", lastCommandReply);
}

void processSerial() {
  // Local bench test: ir 0xDE80 0x08 followed by Enter. Never send on boot.
  static char line[32];
  static uint8_t used = 0;
  static bool overflow = false;
  while (Serial.available()) {
    const char ch = Serial.read();
    if (ch == '\r') continue;
    if (ch != '\n') {
      if (used < sizeof(line) - 1) line[used++] = ch;
      else overflow = true;
      continue;
    }
    line[used] = '\0';
    char *save = nullptr;
    char *kind = strtok_r(line, " ", &save);
    unsigned long address, command;
    if (!overflow && kind && strcmp(kind, "calibrate") == 0) {
      char *position = strtok_r(nullptr, " ", &save);
      if (position && strcmp(position, "off") == 0 && !strtok_r(nullptr, " ", &save) && fanSession[0] &&
          fanHeartbeat[0] && static_cast<unsigned long>(millis() - lastFanHeartbeatMs) <= 10000) {
        fan.calibrateOff(); fanControlSource = "calibration"; ++fanRevision; publishFanState();
        Serial.println(F("Fan calibrated: estimated off; default speed 1"));
      } else Serial.println(F("Confirm fan restarted and left off with: calibrate off (agent must be connected)"));
    } else if (!overflow && kind && strcmp(kind, "ir") == 0 &&
        parseNumber(strtok_r(nullptr, " ", &save), 65535, address, 0) &&
        parseNumber(strtok_r(nullptr, " ", &save), 255, command, 0) &&
        !strtok_r(nullptr, " ", &save)) sendFanIR(address, command, 0);
    else Serial.println(F("Usage: calibrate off | ir 0xDE80 0x08"));
    used = 0;
    overflow = false;
  }
}

bool validEpoch(const char *epoch) {
  if (strlen(epoch) != 32) return false;
  for (uint8_t i = 0; i < 32; ++i) {
    if (!((epoch[i] >= '0' && epoch[i] <= '9') ||
          (epoch[i] >= 'a' && epoch[i] <= 'f'))) return false;
  }
  return true;
}

void updateInterval() {
  char buffer[16] = {0};
  Bridge.get("resac_interval_ms", buffer, sizeof(buffer) - 1);
  if (!buffer[0]) return;
  char *end = nullptr;
  const unsigned long configured = strtoul(buffer, &end, 10);
  if (*end == '\0' && configured >= 2000 && configured <= 3600000) {
    intervalMs = configured;
  }
}

void publishSample() {
  const float humidity = dht.readHumidity();
  const float temperature = dht.readTemperature();
  if (isnan(humidity) || isnan(temperature) || humidity < 0 || humidity > 100 ||
      temperature < -80 || temperature > 150) {
    Bridge.put("resac_sensor_status", "read_error");
    Serial.println(F("DHT11 read failed; no sample published"));
    return;
  }
  Serial.print(F("Temperature: ")); Serial.print(temperature, 1);
  Serial.print(F(" C; humidity: ")); Serial.print(humidity, 1); Serial.println(F(" %"));
  char epoch[33] = {0};
  Bridge.get("resac_epoch", epoch, sizeof(epoch) - 1);
  if (!validEpoch(epoch)) {
    Bridge.put("resac_sensor_status", "waiting_for_agent");
    return;
  }
  char frame[112];
  // A single datastore value prevents mixing temperature/humidity from different samples.
  // Integer thousandths avoid AVR printf float support and large JSON dependencies.
  const long temperatureMilli = lround(temperature * 1000.0f);
  const long humidityMilli = lround(humidity * 1000.0f);
  ++sequence;
  snprintf(frame, sizeof(frame), "v1,%s,%lu,%ld,%ld,%lu", epoch, sequence,
           temperatureMilli, humidityMilli, millis());
  Bridge.put("resac_sample", frame);
  Bridge.put("resac_sensor_status", "ok");
  char uploadStatus[48] = {0};
  Bridge.get("resac_upload_status", uploadStatus, sizeof(uploadStatus) - 1);
  if (uploadStatus[0]) {
    Serial.print(F("Uploader: "));
    Serial.println(uploadStatus);
  }
  char commandStatus[48] = {0};
  Bridge.get("resac_command_status", commandStatus, sizeof(commandStatus) - 1);
  if (commandStatus[0]) {
    Serial.print(F("Commands: "));
    Serial.println(commandStatus);
  }
}
} // namespace

void setup() {
  Serial.begin(115200);
  dht.begin();
  IrSender.begin();
  Serial.println(F("Starting sensor Bridge; Linux boot may take a moment..."));
  Bridge.begin();
  Bridge.put("resac_sensor_status", "starting");
  // AVR sequence numbers restart at zero; request a fresh reading-ID epoch.
  Bridge.put("resac_epoch", "");
  // Discard a pre-reset mailbox: a stale toggle must not run during boot.
  Bridge.put("resac_command", "");
  Bridge.put("resac_command_result", "");
  Bridge.put("resac_fan_invalidate", "");
  invalidateFan();
  Bridge.put("resac_command_ready", "v4");
  Serial.println(F("Bridge ready; DHT11 on digital pin 3"));
  Serial.println(F("IR on digital pin 9; calibration required before sending"));
  lastSampleMs = millis() - DEFAULT_INTERVAL_MS;
}

void loop() {
  processSerial();
  const unsigned long now = millis();
  if (static_cast<unsigned long>(now - lastCommandCheckMs) >= 250) {
    lastCommandCheckMs = now;
    updateInterval();
    updateFan();
    processCommand();
  }
  if (static_cast<unsigned long>(now - lastSampleMs) >= intervalMs) {
    lastSampleMs = now;
    publishSample();
  }
}
