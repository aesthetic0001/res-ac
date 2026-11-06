#include <Arduino.h>
#include <Bridge.h>
#include <Mailbox.h>
#include <DHT.h>
#include <stdlib.h>
#include <string.h>
#include <FanState.h>

// Seeeduino Cloud v1.0: Linux GPIO21 -> TXS0102 -> PB2/MOSI (PCINT2).
// This is NOT the D7 handshake connection found on the original Arduino Yun.
#ifndef RESAC_BATCHED_COMMANDS
#define RESAC_BATCHED_COMMANDS 2
#endif

#ifndef RESAC_COMMAND_GPIO
#define RESAC_COMMAND_GPIO 1
#endif
#if RESAC_COMMAND_GPIO
#include <avr/interrupt.h>
#include <util/atomic.h>
volatile bool commandWake = false;
ISR(PCINT0_vect) {
  if (!(PINB & _BV(PB2))) commandWake = true;
}
#endif

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
#if RESAC_COMMAND_GPIO
bool commandLowHandled = false;
#else
const unsigned long COMMAND_CHECK_MS = 20;
unsigned long lastCommandCheckMs = 0;
#endif
const unsigned long HOUSEKEEPING_MS = 250;
unsigned long intervalMs = DEFAULT_INTERVAL_MS;
unsigned long lastSampleMs = 0;
unsigned long sequence = 0;
unsigned long sensorAttempts = 0;
unsigned long sensorErrors = 0;
unsigned long lastHousekeepingMs = 0;
char lastCommandId[37] = {0};
char lastCommandReply[176] = {0};
FanState fan;
const char *fanControlSource = "reset";
char fanSession[33] = {0};
unsigned long fanRevision = 0;
unsigned long lastFanHeartbeatMs = 0;
char fanHeartbeat[16] = {0};
char controlSession[33] = {0};
bool fanControlValid = false;

bool validEpoch(const char *epoch);
bool parseNumber(const char *, unsigned long, unsigned long &, uint8_t base = 10);
void serviceCommandNotification();
bool commandExecuting = false;
bool commandNotificationsReady = false;
bool fanStatePending = false;

void publishValue(const char *key, const char *value) {
  // Retain a snapshot for reconnects, then notify connected Linux clients.
  Bridge.put(key, value);
  char event[224];
  const int length = snprintf_P(event, sizeof(event), PSTR("v1,%s,%s"), key, value);
  if (length > 0 && static_cast<size_t>(length) < sizeof(event))
    Mailbox.writeMessage(reinterpret_cast<const uint8_t *>(event), length);
}

void publishFanState() {
#if RESAC_BATCHED_COMMANDS >= 2
  if (commandExecuting) { fanStatePending = true; return; }
#endif
  fanStatePending = false;
  char frame[96];
  snprintf_P(frame, sizeof(frame), PSTR("v2,%s,%u,%u,%u,%lu,%s"), fanSession[0] ? fanSession : "none",
           fan.calibrated(), fan.power(), fan.speed(), fanRevision, fanControlSource);
  publishValue("resac_fan_state", frame);
}

void invalidateFan() {
  fanControlValid = false;
  fan.reset();
  fanControlSource = "reset";
  fanSession[0] = '\0';
  ++fanRevision;
  publishValue("resac_fan_session", "");
  publishFanState();
  Serial.println(F("Calibration required: set fan speed to 1, turn it off; confirm with calibrate off"));
}

void updateFan() {
#if RESAC_BATCHED_COMMANDS
  // One atomic, agent-owned context replaces independent safety/session reads.
  char context[56] = {0};
  const unsigned int length = Bridge.get("resac_fan_control", context, sizeof(context) - 1);
  char *save = nullptr;
  char *version = strtok_r(context, ",", &save);
  char *session = strtok_r(nullptr, ",", &save);
  char *heartbeat = strtok_r(nullptr, ",", &save);
  unsigned long number;
  if (length >= sizeof(context) - 1 || !version || strcmp(version, "v1") != 0 ||
      !session || !validEpoch(session) || !parseNumber(heartbeat, 0xffffffffUL, number) ||
      !number || strlen(heartbeat) > 10 ||
      (strlen(heartbeat) == 10 && strcmp(heartbeat, "4294967295") > 0) ||
      strspn(heartbeat, "0123456789") != strlen(heartbeat) || strtok_r(nullptr, ",", &save)) {
    fanControlValid = false;
    if (fan.calibrated()) invalidateFan();
    return;
  }
  const bool changed = strcmp(session, controlSession) != 0;
  if (changed) {
    // A new agent epoch invalidates calibration and all commands from the old one.
    fan.reset(); fanControlSource = "reset"; ++fanRevision;
    strcpy(controlSession, session); strcpy(fanSession, session); fanHeartbeat[0] = '\0';
  }
  if (strcmp(heartbeat, fanHeartbeat) != 0) {
    strcpy(fanHeartbeat, heartbeat); lastFanHeartbeatMs = millis();
  }
  fanControlValid = fanSession[0] && static_cast<unsigned long>(millis() - lastFanHeartbeatMs) <= 10000;
  if (!fanControlValid && fan.calibrated()) { invalidateFan(); return; }
  if (changed) { publishValue("resac_fan_session", fanSession); publishFanState(); }
#else
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
#endif
}

bool parseNumber(const char *text, unsigned long limit, unsigned long &value, uint8_t base) {
  if (!text || !*text || *text == '-' || *text == '+' || *text == ' ') return false;
  char *end = nullptr;
  value = strtoul(text, &end, base);
  return *end == '\0' && value <= limit;
}

bool sendFanIR(uint16_t address, uint8_t command, uint8_t repeats, const char *source = "manual") {
  if (!fan.calibrated()) {
    Serial.println(F("IR blocked: calibration required; set fan speed to 1, then turn it off"));
    return false;
  }
  if (address == 0xDE80 && (command == 0x16 || command == 0x14) && !fan.power()) {
    Serial.println(F("IR blocked: turn the fan on before changing speed"));
    return false;
  }
  // Modern IRremote sends address/command LSB first and adds the complement byte.
  // 0xDE80 + 0x16 is speed up; 0x14 is speed down.
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
  const int8_t route = FanState::speedSteps(fan.speed(), target);
  const uint8_t count = route < 0 ? -route : route;
  for (uint8_t i = 0; i < count; ++i) {
    if (!sendFanIR(0xDE80, route < 0 ? 0x14 : 0x16, 0, source)) return false;
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
  const unsigned long fetchStarted = millis();
  char frame[128] = {0};
  Bridge.get("resac_command", frame, sizeof(frame) - 1);
  const unsigned long fetchDuration = millis() - fetchStarted;
  // One atomic request; the batched completion requires the matching agent.
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
    // Fast command checks must still see pending invalidation and fresh agent
    // heartbeats before authorizing an action, independently of housekeeping.
    const unsigned long safetyStarted = millis();
    if (control || levelControl) updateFan();
    const unsigned long safetyDuration = millis() - safetyStarted;
    const unsigned long executionStarted = millis();
    if (control || levelControl) {
      if (
#if RESAC_BATCHED_COMMANDS
          !fanControlValid ||
#endif
          !fanSession[0] || strcmp(session, fanSession) != 0 ||
          !fanHeartbeat[0] || static_cast<unsigned long>(millis() - lastFanHeartbeatMs) > 10000 ||
          (!calibrate && !fan.calibrated())) {
        snprintf_P(lastCommandReply, sizeof(lastCommandReply), PSTR("v3,%s,rejected,%s"), id, session);
      } else if (calibrate) {
        fan.calibrateOff(); fanControlSource = "calibration"; ++fanRevision; publishFanState();
        snprintf_P(lastCommandReply, sizeof(lastCommandReply), PSTR("v3,%s,calibrated,%s"), id, session);
        Serial.println(F("Fan calibrated: estimated off; saved speed 1"));
      } else if (levelControl) {
        uint8_t presses;
        if (setFanLevel(command, automatic, presses))
          snprintf_P(lastCommandReply, sizeof(lastCommandReply), PSTR("v4,%s,level_set,%s,%lu,%u,%lu"), id, session, command, presses, millis());
        else snprintf_P(lastCommandReply, sizeof(lastCommandReply), PSTR("v3,%s,rejected,%s"), id, session);
      } else {
        if (sendFanIR(address, command, repeats))
          snprintf_P(lastCommandReply, sizeof(lastCommandReply), PSTR("v3,%s,ir_sent,%s,%lu,%lu,%lu,%lu"),
                   id, session, address, command, repeats, millis());
        else snprintf_P(lastCommandReply, sizeof(lastCommandReply), PSTR("v3,%s,rejected,%s"), id, session);
      }
    } else {
      snprintf_P(lastCommandReply, sizeof(lastCommandReply), PSTR("v1,%s,pong,%lu"), id, millis());
      Serial.println(F("Ping handled by Arduino; replying pong"));
    }
    strcpy(lastCommandId, id);
#if RESAC_BATCHED_COMMANDS
    // Retain and push one versioned completion, including the legacy reply and
    // bounded phase durations. No separate diagnostic round trips delay success.
    char prefix[64];
#if RESAC_BATCHED_COMMANDS >= 2
    const char source = strcmp(fanControlSource, "automatic") == 0 ? 'a' :
                        strcmp(fanControlSource, "manual") == 0 ? 'm' :
                        strcmp(fanControlSource, "calibration") == 0 ? 'c' : 'r';
    const int size = snprintf_P(prefix, sizeof(prefix), PSTR("v6,%lu,%lu,%lu,%lu,%u,%u,%c,"), fetchDuration,
                              safetyDuration, static_cast<unsigned long>(millis() - executionStarted),
                              fanRevision, (fan.calibrated() ? 1 : 0) | (fan.power() ? 2 : 0), fan.speed(), source);
#else
    const int size = snprintf_P(prefix, sizeof(prefix), PSTR("v5,%lu,%lu,%lu,"), fetchDuration,
                              safetyDuration, static_cast<unsigned long>(millis() - executionStarted));
#endif
    const size_t replyLength = strlen(lastCommandReply);
    if (size <= 0 || static_cast<size_t>(size) >= sizeof(prefix) ||
        static_cast<size_t>(size) + replyLength >= sizeof(lastCommandReply)) return;
    memmove(lastCommandReply + size, lastCommandReply, replyLength + 1);
    memcpy(lastCommandReply, prefix, size);
#else
    char diagnostic[72];
    snprintf_P(diagnostic, sizeof(diagnostic), PSTR("v1,%s,%lu,%s"), id,
             static_cast<unsigned long>(millis() - executionStarted),
             strstr(lastCommandReply, ",rejected,") ? "failed" : "ok");
    publishValue("resac_command_diagnostic", diagnostic);
#endif
    publishValue("resac_command_result", lastCommandReply);
  }
  // Repeated deliveries keep the original retained response without executing
  // again or broadcasting the same acknowledgment on every mailbox check.
}

void processSerial() {
  // Local bench test: ir 0xDE80 0x16 followed by Enter. Never send on boot.
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
        Serial.println(F("Fan calibrated: estimated off; saved speed 1"));
      } else Serial.println(F("Set fan speed to 1, turn it off, then: calibrate off (agent must be connected)"));
    } else if (!overflow && kind && strcmp(kind, "ir") == 0 &&
        parseNumber(strtok_r(nullptr, " ", &save), 65535, address, 0) &&
        parseNumber(strtok_r(nullptr, " ", &save), 255, command, 0) &&
        !strtok_r(nullptr, " ", &save)) sendFanIR(address, command, 0);
    else Serial.println(F("Usage: calibrate off | ir 0xDE80 0x16"));
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

void publishSensorDiagnostic(const char *epoch, unsigned long duration, bool ok) {
  if (!validEpoch(epoch)) return;
  char frame[112];
  snprintf_P(frame, sizeof(frame), PSTR("v1,%s,%lu,%lu,%lu,%s,%lu,%lu"), epoch, sequence,
           sensorAttempts, duration, ok ? "ok" : "failed", millis(), sensorErrors);
  publishValue("resac_sensor_diagnostic", frame);
}

#if RESAC_BATCHED_COMMANDS
struct SensorPublication {
  char epoch[33];
  unsigned long duration, sampledAt;
  long temperatureMilli, humidityMilli;
  uint8_t stage;
  bool valid;
} sensorPublication = {};

void publishSample() {
  // Each stage returns to the main loop. Commands run between background
  // exchanges without nested command execution or retaining large stack frames.
  SensorPublication &sample = sensorPublication;
  switch (sample.stage) {
    case 0: {
      memset(sample.epoch, 0, sizeof(sample.epoch));
      Bridge.get("resac_epoch", sample.epoch, sizeof(sample.epoch) - 1);
      const unsigned long started = millis();
      ++sensorAttempts;
      const float humidity = dht.readHumidity(), temperature = dht.readTemperature();
      sample.duration = millis() - started; sample.sampledAt = millis();
      sample.valid = !isnan(humidity) && !isnan(temperature) && humidity >= 0 && humidity <= 100 &&
                     temperature >= -80 && temperature <= 150;
      if (!sample.valid) ++sensorErrors;
      else {
        sample.temperatureMilli = lround(temperature * 1000.0f);
        sample.humidityMilli = lround(humidity * 1000.0f);
        if (validEpoch(sample.epoch)) ++sequence;
        Serial.print(F("Temperature: ")); Serial.print(temperature, 1);
        Serial.print(F(" C; humidity: ")); Serial.print(humidity, 1); Serial.println(F(" %"));
      }
      sample.stage = 1;
      break;
    }
    case 1:
      publishSensorDiagnostic(sample.epoch, sample.duration, sample.valid);
      sample.stage = 2;
      break;
    case 2:
      publishValue("resac_sensor_status", !sample.valid ? "read_error" :
                   validEpoch(sample.epoch) ? "ok" : "waiting_for_agent");
      sample.stage = sample.valid && validEpoch(sample.epoch) ? 3 : 0;
      break;
    case 3: {
      char frame[112];
      snprintf_P(frame, sizeof(frame), PSTR("v1,%s,%lu,%ld,%ld,%lu"), sample.epoch, sequence,
               sample.temperatureMilli, sample.humidityMilli, sample.sampledAt);
      publishValue("resac_sample", frame);
      sample.stage = 4;
      break;
    }
    default: {
      char status[48] = {0};
      Bridge.get(sample.stage == 4 ? "resac_upload_status" : "resac_command_status", status, sizeof(status) - 1);
      if (status[0]) { Serial.print(sample.stage == 4 ? F("Uploader: ") : F("Commands: ")); Serial.println(status); }
      sample.stage = sample.stage == 4 ? 5 : 0;
      break;
    }
  }
}
#else
void publishSample() {
  char epoch[33] = {0};
  Bridge.get("resac_epoch", epoch, sizeof(epoch) - 1);
  const unsigned long started = millis();
  ++sensorAttempts;
  const float humidity = dht.readHumidity();
  const float temperature = dht.readTemperature();
  const unsigned long duration = static_cast<unsigned long>(millis() - started);
  if (isnan(humidity) || isnan(temperature) || humidity < 0 || humidity > 100 ||
      temperature < -80 || temperature > 150) {
    ++sensorErrors;
    publishSensorDiagnostic(epoch, duration, false);
    publishValue("resac_sensor_status", "read_error");
    Serial.println(F("DHT11 read failed; no sample published"));
    return;
  }
  Serial.print(F("Temperature: ")); Serial.print(temperature, 1);
  Serial.print(F(" C; humidity: ")); Serial.print(humidity, 1); Serial.println(F(" %"));
  if (!validEpoch(epoch)) {
    publishValue("resac_sensor_status", "waiting_for_agent");
    return;
  }
  char frame[112];
  // A single datastore value prevents mixing temperature/humidity from different samples.
  // Integer thousandths avoid AVR printf float support and large JSON dependencies.
  const long temperatureMilli = lround(temperature * 1000.0f);
  const long humidityMilli = lround(humidity * 1000.0f);
  ++sequence;
  snprintf_P(frame, sizeof(frame), PSTR("v1,%s,%lu,%ld,%ld,%lu"), epoch, sequence,
           temperatureMilli, humidityMilli, millis());
  publishSensorDiagnostic(epoch, duration, true);
  publishValue("resac_sensor_status", "ok");
  publishValue("resac_sample", frame);
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
#endif
} // namespace

void setup() {
  commandNotificationsReady = false;
  controlSession[0] = 0;
  Serial.begin(115200);
  dht.begin();
  IrSender.begin();
  Serial.println(F("Starting sensor Bridge; Linux boot may take a moment..."));
  Bridge.begin();
  Mailbox.begin();
  publishValue("resac_events", "");
  publishValue("resac_command_notify", "");
  publishValue("resac_sensor_status", "starting");
  // AVR sequence numbers restart at zero; request a fresh reading-ID epoch.
  publishValue("resac_epoch", "");
  // Discard a pre-reset mailbox: a stale toggle must not run during boot.
  Bridge.put("resac_command", "");
  publishValue("resac_sample", "");
  publishValue("resac_command_result", "");
  publishValue("resac_command_diagnostic", "");
  publishValue("resac_sensor_diagnostic", "");
  Bridge.put("resac_fan_invalidate", "");
  invalidateFan();
#if RESAC_COMMAND_GPIO
  // An AVR pull-up keeps this active-low input inactive if Linux releases it
  // or disables the translator. The board translates 3.3 V to 5 V; never drive
  // this shared line from the AVR.
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    DDRB &= ~_BV(DDB2);
    PORTB |= _BV(PB2);
    commandWake = false;
    commandLowHandled = (PINB & _BV(PB2)) == 0;
    PCIFR = _BV(PCIF0);
    PCMSK0 |= _BV(PCINT2);
    PCICR |= _BV(PCIE0);
  }
  publishValue("resac_command_notify", "gpio21-pb2-v1");
#endif
#if RESAC_BATCHED_COMMANDS >= 2
  publishValue("resac_command_protocol", "batched-v2");
#elif RESAC_BATCHED_COMMANDS
  publishValue("resac_command_protocol", "batched-v1");
#else
  publishValue("resac_command_protocol", "");
#endif
  publishValue("resac_command_ready", "v4");
  publishValue("resac_events", "v1");
  Serial.println(F("Bridge ready; DHT11 on digital pin 3"));
  Serial.println(F("IR on digital pin 9; calibration required before sending"));
  lastSampleMs = millis() - DEFAULT_INTERVAL_MS;
  commandNotificationsReady = true;
}

namespace {
void serviceCommandNotification() {
  if (commandExecuting || !commandNotificationsReady) return;
#if RESAC_COMMAND_GPIO
  bool wake;
  bool pending;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    wake = commandWake;
    commandWake = false;
    pending = (PINB & _BV(PB2)) == 0;
  }
  // A held level also catches assertion during DHT's interrupt-masked section.
  // Reading the local input does not generate a Bridge transaction.
  if (!pending) commandLowHandled = false;
  if (pending && (wake || !commandLowHandled)) {
    commandLowHandled = pending;
    commandExecuting = true;
    processCommand();
    commandExecuting = false;
  }
#else
  const unsigned long commandNow = millis();
  if (static_cast<unsigned long>(commandNow - lastCommandCheckMs) >= COMMAND_CHECK_MS) {
    lastCommandCheckMs = commandNow;
    commandExecuting = true;
    processCommand();
    commandExecuting = false;
  }
#endif
}
} // namespace

void loop() {
  processSerial();
  serviceCommandNotification();
  if (fanStatePending) { publishFanState(); serviceCommandNotification(); }
  // Recheck time after command execution, which can include physical IR dwell.
  const unsigned long now = millis();
  if (static_cast<unsigned long>(now - lastHousekeepingMs) >= HOUSEKEEPING_MS) {
    lastHousekeepingMs = now;
    updateInterval();
    serviceCommandNotification();
    updateFan();
    serviceCommandNotification();
  }
#if RESAC_BATCHED_COMMANDS
  if (sensorPublication.stage || static_cast<unsigned long>(millis() - lastSampleMs) >= intervalMs) {
    if (!sensorPublication.stage) lastSampleMs = millis();
    publishSample();
  }
#else
  if (static_cast<unsigned long>(now - lastSampleMs) >= intervalMs) {
    lastSampleMs = now;
    publishSample();
  }
#endif
}
