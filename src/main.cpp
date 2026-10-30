#include <Arduino.h>
#include <Bridge.h>
#include <DHT.h>
#include <stdlib.h>
#include <string.h>

#define DHTPIN 3
#define DHTTYPE DHT11

namespace {
DHT dht(DHTPIN, DHTTYPE);
const unsigned long DEFAULT_INTERVAL_MS = 10000;
unsigned long intervalMs = DEFAULT_INTERVAL_MS;
unsigned long lastSampleMs = 0;
unsigned long sequence = 0;

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
}
} // namespace

void setup() {
  Serial.begin(115200);
  dht.begin();
  Serial.println(F("Starting sensor Bridge; Linux boot may take a moment..."));
  Bridge.begin();
  Bridge.put("resac_sensor_status", "starting");
  Serial.println(F("Bridge ready; DHT11 on digital pin 3"));
  lastSampleMs = millis() - DEFAULT_INTERVAL_MS;
}

void loop() {
  const unsigned long now = millis();
  if (static_cast<unsigned long>(now - lastSampleMs) >= intervalMs) {
    lastSampleMs = now;
    updateInterval();
    publishSample();
  }
}
