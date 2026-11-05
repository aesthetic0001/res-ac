"""Run the actual sketch against a virtual clock and Bridge/IR test doubles."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class FirmwareSchedulingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        directory = Path(cls.directory.name)
        root = Path(__file__).resolve().parents[2]
        headers = {
            'Arduino.h': r'''
                #pragma once
                #include <cstdint>
                #include <cstdio>
                #include <cstdlib>
                #include <cstring>
                #include <cmath>
                #include <string>
                #include <vector>
                using std::isnan;
                using std::lround;
                #define F(s) s
                #define HEX 16
                extern unsigned long testClock;
                inline unsigned long millis() { return testClock; }
                inline void delay(unsigned long ms) { testClock += ms; }
                struct TestSerial {
                  void begin(int) {}
                  int available() { return 0; }
                  int read() { return -1; }
                  template<typename... T> void print(T...) {}
                  template<typename... T> void println(T...) {}
                };
                extern TestSerial Serial;
            ''',
            'Bridge.h': r'''
                #pragma once
                #include <map>
                #include <string>
                #include <cstring>
                struct TestBridge {
                  std::map<std::string,std::string> values;
                  std::map<std::string,int> reads;
                  void begin() {}
                  void put(const char *key, const char *value) { values[key] = value; }
                  unsigned int get(const char *key, char *buffer, unsigned int length) {
                    ++reads[key];
                    std::strncpy(buffer, values[key].c_str(), length);
                    return values[key].size() < length ? values[key].size() : length;
                  }
                };
                extern TestBridge Bridge;
            ''',
            'Mailbox.h': r'''
                #pragma once
                struct TestMailbox {
                  std::vector<std::string> messages;
                  void begin() {}
                  void writeMessage(const uint8_t *data, unsigned int size) {
                    messages.emplace_back(reinterpret_cast<const char *>(data), size);
                  }
                };
                extern TestMailbox Mailbox;
            ''',
            'DHT.h': r'''
                #pragma once
                #define DHT11 11
                struct DHT {
                  DHT(int, int) {}
                  void begin() {}
                  float readHumidity() { return 50; }
                  float readTemperature() { return 22; }
                };
            ''',
            'IRremote.hpp': r'''
                #pragma once
                struct TestIr {
                  unsigned int sends = 0;
                  void begin() {}
                  void sendNEC(uint16_t, uint8_t, uint8_t) { ++sends; }
                };
                extern TestIr IrSender;
            '''
        }
        for name, source in headers.items():
            (directory / name).write_text(source)
        source = r'''
            #include <cassert>
            #include <limits>
            #include "Arduino.h"
            #include "Bridge.h"
            #include "Mailbox.h"
            #include "IRremote.hpp"
            unsigned long testClock = 0;
            TestSerial Serial;
            TestBridge Bridge;
            TestMailbox Mailbox;
            TestIr IrSender;
        ''' + '#include "' + str(root / 'src/main.cpp') + '"\n' + r'''
            const char *session = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
            const char *id1 = "11111111-1111-4111-8111-111111111111";
            const char *id2 = "22222222-2222-4222-8222-222222222222";
            void reset() {
              testClock = lastSampleMs = lastCommandCheckMs = lastHousekeepingMs = 0;
              intervalMs = DEFAULT_INTERVAL_MS;
              lastCommandId[0] = lastCommandReply[0] = 0;
              fan.calibrateOff(); std::strcpy(fanSession, session);
              std::strcpy(fanHeartbeat, "1"); lastFanHeartbeatMs = 0;
              IrSender.sends = 0; Mailbox.messages.clear();
              Bridge.values.clear(); Bridge.reads.clear();
              Bridge.values["resac_fan_session"] = session;
              Bridge.values["resac_fan_heartbeat"] = "1";
              Bridge.values["resac_interval_ms"] = "10000";
            }
            void power(const char *id = id1, const char *requestedSession = session) {
              Bridge.values["resac_command"] = std::string("v3,") + id + ",nec," + requestedSession + ",56960,0,0";
            }
            int main(int argc, char **argv) {
              assert(argc == 2); reset();
              const std::string mode = argv[1];
              if (mode == "cadence") {
                Bridge.values["resac_command"] = std::string("v1,") + id1 + ",ping";
                testClock = 19; loop(); assert(Bridge.reads["resac_command"] == 0);
                testClock = 20; loop(); assert(Bridge.reads["resac_command"] == 1);
                assert(Bridge.values["resac_command_result"].find(",pong,") != std::string::npos);
                testClock = 40; loop(); assert(Bridge.reads["resac_command"] == 2);
                assert(Mailbox.messages.size() == 2); // Duplicate ID adds no execution/reply.
                Bridge.values["resac_command"] = std::string("v1,") + id2 + ",ping";
                testClock = 60; loop(); assert(Bridge.reads["resac_command"] == 3);
                assert(Bridge.reads["resac_interval_ms"] == 0);
                assert(Bridge.reads["resac_fan_heartbeat"] == 0);
                testClock = 249; loop(); assert(Bridge.reads["resac_interval_ms"] == 0);
                testClock = 250; loop(); assert(Bridge.reads["resac_interval_ms"] == 1);
                assert(Bridge.reads["resac_fan_heartbeat"] == 1);
              } else if (mode == "safety") {
                power(); Bridge.values["resac_fan_invalidate"] = "1";
                testClock = 20; loop();
                assert(IrSender.sends == 0 && !fan.calibrated());
                assert(Bridge.values["resac_command_result"].find(",rejected,") != std::string::npos);
                assert(lastHousekeepingMs == 0); // Rejection happens before the 250 ms tick.
                reset(); power(); testClock = lastSampleMs = 10001; lastHousekeepingMs = 10000;
                loop(); assert(IrSender.sends == 0 && !fan.calibrated());
                reset(); power(); testClock = lastSampleMs = 10001; lastHousekeepingMs = 10000;
                Bridge.values["resac_fan_heartbeat"] = "2";
                loop(); assert(IrSender.sends == 1 && fan.power());
                assert(lastFanHeartbeatMs == 10001);
                assert(Bridge.reads["resac_interval_ms"] == 0);
                const int safetyReads = Bridge.reads["resac_fan_heartbeat"];
                testClock += 20; lastSampleMs = testClock; loop();
                assert(IrSender.sends == 1); // No repeated physical action or safety reads.
                assert(Bridge.reads["resac_fan_heartbeat"] == safetyReads);
                reset(); power(id1, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");
                testClock = 20; loop(); assert(IrSender.sends == 0);
              } else if (mode == "rollover") {
                Bridge.values["resac_command"] = std::string("v1,") + id1 + ",ping";
                lastCommandCheckMs = std::numeric_limits<unsigned long>::max() - 10;
                lastHousekeepingMs = std::numeric_limits<unsigned long>::max() - 100;
                testClock = lastSampleMs = 9; loop();
                assert(Bridge.reads["resac_command"] == 1);
                assert(Bridge.reads["resac_interval_ms"] == 0);
              } else if (mode == "dwell") {
                Bridge.values["resac_command"] = std::string("v4,") + id1 + ",level," + session + ",5";
                testClock = 20; loop();
                assert(IrSender.sends == 5 && fan.speed() == 5);
                assert(testClock == 620); // Original power/step dwell is preserved.
                assert(lastHousekeepingMs == testClock);
                assert(Bridge.reads["resac_interval_ms"] == 1);
              } else { assert(false); }
            }
        '''
        path = directory / 'sketch.cpp'
        path.write_text(source)
        cls.binary = str(directory / 'sketch-test')
        subprocess.run([shutil.which('g++') or 'g++', '-std=c++11', '-I' + str(directory),
                        '-I' + str(root / 'include'), str(path), '-o', cls.binary], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_commands_are_checked_before_housekeeping_is_due(self):
        subprocess.run([self.binary, 'cadence'], check=True)

    def test_new_commands_refresh_invalidation_heartbeat_and_session_guards(self):
        subprocess.run([self.binary, 'safety'], check=True)

    def test_command_schedule_survives_millis_rollover(self):
        subprocess.run([self.binary, 'rollover'], check=True)

    def test_housekeeping_runs_after_physical_ir_dwell(self):
        subprocess.run([self.binary, 'dwell'], check=True)
