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
            'avr/interrupt.h': r'''
                #pragma once
                #define ISR(name) void name()
            ''',
            'util/atomic.h': r'''
                #pragma once
                #define ATOMIC_RESTORESTATE 0
                #define ATOMIC_BLOCK(unused) for (bool once = true; once; once = false)
            ''',
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
                #define _BV(bit) (1U << (bit))
                enum {PB2=2, DDB2=2, PCIF0=0, PCINT2=2, PCIE0=0};
                extern uint8_t PINB, DDRB, PORTB, PCIFR, PCMSK0, PCICR;
                extern unsigned long testClock;
                extern void (*readHook)(const char *);
                extern void (*mailboxHook)();
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
                    if (readHook) readHook(key);
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
                    if (mailboxHook) mailboxHook();
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
            (directory / name).parent.mkdir(parents=True, exist_ok=True)
            (directory / name).write_text(source)
        source = r'''
            #include <cassert>
            #include <limits>
            #include "Arduino.h"
            #include "Bridge.h"
            #include "Mailbox.h"
            #include "IRremote.hpp"
            uint8_t PINB=0, DDRB=0xff, PORTB=0xff, PCIFR=0, PCMSK0=0, PCICR=0;
            unsigned long testClock = 0;
            void (*readHook)(const char *) = nullptr;
            void (*mailboxHook)() = nullptr;
            TestSerial Serial;
            TestBridge Bridge;
            TestMailbox Mailbox;
            TestIr IrSender;
        ''' + '#include "' + str(root / 'src/main.cpp') + '"\n' + r'''
            const char *session = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
            const char *id1 = "11111111-1111-4111-8111-111111111111";
            const char *id2 = "22222222-2222-4222-8222-222222222222";
            void reset() {
              testClock = lastSampleMs = lastHousekeepingMs = 0;
              commandNotificationsReady = true; commandExecuting = false; readHook = nullptr; mailboxHook = nullptr;
              #if RESAC_BATCHED_COMMANDS
              sensorPublication.stage=0;
              #endif
              controlSession[0] = 0; fanControlValid = false;
              #if RESAC_COMMAND_GPIO
              PINB = _BV(PB2); commandWake = commandLowHandled = false;
              #else
              lastCommandCheckMs = 0;
              #endif
              intervalMs = DEFAULT_INTERVAL_MS;
              lastCommandId[0] = lastCommandReply[0] = 0;
              fan.calibrateOff(); std::strcpy(fanSession, session);
              std::strcpy(fanHeartbeat, "1"); lastFanHeartbeatMs = 0;
              IrSender.sends = 0; Mailbox.messages.clear();
              Bridge.values.clear(); Bridge.reads.clear();
              Bridge.values["resac_fan_session"] = session;
              Bridge.values["resac_fan_heartbeat"] = "1";
              Bridge.values["resac_interval_ms"] = "10000";
              Bridge.values["resac_epoch"] = session;
            }
            void notify() {
              #if RESAC_COMMAND_GPIO
              PINB &= ~_BV(PB2); PCINT0_vect();
              #endif
            }
            void power(const char *id = id1, const char *requestedSession = session) {
              Bridge.values["resac_command"] = std::string("v3,") + id + ",nec," + requestedSession + ",56960,0,0";
              notify();
            }
            int main(int argc, char **argv) {
              assert(argc == 2); reset();
              const std::string mode = argv[1];
              if (mode == "gpio") {
                #if RESAC_COMMAND_GPIO
                Bridge.values["resac_command"] = std::string("v1,") + id1 + ",ping";
                for (unsigned long t=1; t<250; ++t) { testClock=t; loop(); }
                assert(Bridge.reads["resac_command"] == 0); // No idle Bridge reads.
                notify(); loop();
                assert(Bridge.reads["resac_command"] == 1);
                for (unsigned long t=250; t<500; ++t) { testClock=t; loop(); }
                assert(Bridge.reads["resac_command"] == 1); // Held low reads once.
                PINB = _BV(PB2); PCINT0_vect(); loop();
                assert(Bridge.reads["resac_command"] == 1); // Release is not work.
                Bridge.values["resac_command"] = std::string("v1,") + id2 + ",ping";
                PINB = 0; loop(); // Assertion IRQ masked during a sensor read.
                assert(Bridge.reads["resac_command"] == 2);
                notify(); loop(); assert(Bridge.reads["resac_command"] == 3);
                assert(Mailbox.messages.size() == 4); // Duplicate ID is not executed again.
                PINB = _BV(PB2); loop(); notify(); PINB = _BV(PB2); loop();
                assert(Bridge.reads["resac_command"] == 3); // Canceled before main-loop dispatch.
                #else
                assert(false);
                #endif
              } else if (mode == "boot") {
                #if RESAC_COMMAND_GPIO
                PINB = 0; power(); setup();
                assert(!(DDRB & _BV(DDB2)) && (PORTB & _BV(PB2)));
                assert(PCMSK0 & _BV(PCINT2)); assert(PCICR & _BV(PCIE0));
                assert(Bridge.values["resac_command_notify"] == "gpio21-pb2-v1");
                assert(Bridge.values["resac_command"].empty());
                lastSampleMs = testClock; loop(); assert(Bridge.reads["resac_command"] == 0);
                #endif
              } else if (mode == "cadence") {
                #if !RESAC_COMMAND_GPIO
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
              #endif
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
                #if !RESAC_COMMAND_GPIO
                lastCommandCheckMs = std::numeric_limits<unsigned long>::max() - 10;
                #endif
                notify();
                lastHousekeepingMs = std::numeric_limits<unsigned long>::max() - 100;
                testClock = lastSampleMs = 9; loop();
                assert(Bridge.reads["resac_command"] == 1);
                assert(Bridge.reads["resac_interval_ms"] == 0);
              } else if (mode == "dwell") {
                Bridge.values["resac_command"] = std::string("v4,") + id1 + ",level," + session + ",5";
                notify(); testClock = 20; loop();
                assert(IrSender.sends == 5 && fan.speed() == 5);
                assert(testClock == 620); // Original power/step dwell is preserved.
                assert(lastHousekeepingMs == testClock);
                assert(Bridge.reads["resac_interval_ms"] == 1);

              } else if (mode == "batch") {
                std::strcpy(controlSession, session);
                Bridge.values["resac_fan_control"] = std::string("v1,") + session + ",1";
                power(); testClock = 20; loop();
                assert(IrSender.sends == 1 && fan.power());
                assert(Bridge.reads["resac_fan_control"] == 1);
                assert(Bridge.reads["resac_fan_invalidate"] == 0);
                assert(Bridge.reads["resac_fan_heartbeat"] == 0);
                assert(Bridge.values["resac_command_result"].find("v5,0,0,0,v3,") == 0);
                assert(Bridge.values["resac_command_diagnostic"].empty());
                assert(Mailbox.messages.size() == 2); // State pair + one combined completion.
                reset(); std::strcpy(controlSession, session);
                Bridge.values["resac_fan_control"] = std::string("v1,") + session + ",1";
                Bridge.values["resac_command"] = std::string("v3,") + id1 + ",calibrate," + session;
                readHook=[](const char *) { testClock+=100; };
                notify(); testClock=20; loop();
                assert(Bridge.values["resac_command_result"].find("v5,100,100,0,v3,")==0);
                reset(); std::strcpy(controlSession, session);
                Bridge.values["resac_fan_control"] = std::string("v1,") + session + ",1";
                power(); testClock = lastSampleMs = 10001; lastHousekeepingMs = 10000;
                loop(); assert(IrSender.sends == 0 && !fan.calibrated());
                Bridge.values["resac_command"] = std::string("v3,") + id2 + ",calibrate," + session;
                notify(); loop(); assert(!fan.calibrated()); // Re-reading a stale snapshot cannot refresh it.
                Bridge.values["resac_fan_control"] = std::string("v1,") + session + ",2";
                lastCommandId[0]=0; notify(); loop(); assert(!fan.calibrated()); // Invalidated epoch stays invalid.
                const char *fresh="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
                Bridge.values["resac_fan_control"] = std::string("v1,") + fresh + ",1";
                Bridge.values["resac_command"] = std::string("v3,") + id1 + ",calibrate," + fresh;
                notify(); loop(); assert(fan.calibrated() && strcmp(fanSession,fresh)==0);
                for (const char *bad : {"", "v1,aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa,0",
                     "v1,aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa,1,extra", "v9,aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa,1",
                     "v1,aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa,4294967296"}) {
                  reset(); std::strcpy(controlSession, session);
                  Bridge.values["resac_fan_control"] = bad;
                  Bridge.values["resac_command"] = std::string("v3,") + id1 + ",calibrate," + session;
                  notify(); testClock=20; loop(); assert(!fan.calibrated());
                  assert(Bridge.values["resac_command_result"].find(",rejected,")!=std::string::npos);
                }
                reset(); std::strcpy(controlSession, session);
                Bridge.values["resac_fan_control"] = std::string("v1,") + session + ",1";
                Bridge.values["resac_command"] = std::string("v4,") + id1 + ",level," + session + ",5";
                notify(); testClock=20; loop();
                assert(IrSender.sends==5 && fan.speed()==5 && testClock==620);
              } else if (mode == "priority") {
                fan.reset();
                Bridge.values["resac_command"] = std::string("v1,") + id1 + ",ping";
                readHook=[](const char *key) {
                  if (strcmp(key,"resac_interval_ms")==0) notify();
                  if (strcmp(key,"resac_fan_control")==0) assert(Bridge.reads["resac_command"]==1);
                };
                testClock=lastSampleMs=250; loop();
                assert(Bridge.reads["resac_command"]==1);
                assert(Mailbox.messages.size()==1); // Single combined completion, no recursive handling.
                assert(Bridge.values["resac_command_result"].find("v5,0,0,0,v1,")==0);

              } else if (mode == "sensor-priority") {
                #if RESAC_BATCHED_COMMANDS
                fan.reset();
                Bridge.values["resac_command"] = std::string("v1,") + id1 + ",ping";
                testClock=10000; lastHousekeepingMs=testClock;
                loop(); assert(sensorPublication.stage==1 && Bridge.reads["resac_command"]==0);
                mailboxHook=[]() {
                  if (Mailbox.messages.back().find("v1,resac_sensor_diagnostic,")==0) notify();
                };
                loop(); assert(sensorPublication.stage==2 && Bridge.reads["resac_command"]==0);
                readHook=[](const char *key) {
                  if (strcmp(key,"resac_command")==0) assert(sensorPublication.stage==2);
                };
                loop(); assert(Bridge.reads["resac_command"]==1);
                assert(Bridge.values["resac_sensor_status"]=="ok");
                loop(); assert(Bridge.values["resac_sample"].find(std::string("v1,")+session+",1,22000,50000,")==0);
                loop(); loop(); assert(sensorPublication.stage==0);
                #endif
              } else { assert(false); }
            }
        '''
        path = directory / 'sketch.cpp'
        path.write_text(source)
        cls.binaries = []
        for enabled in (0, 1):
            binary = str(directory / ('sketch-test-' + str(enabled)))
            subprocess.run([shutil.which('g++') or 'g++', '-std=c++11', '-DRESAC_BATCHED_COMMANDS=0', '-DRESAC_COMMAND_GPIO=' + str(enabled),
                            '-I' + str(directory), '-I' + str(root / 'include'), str(path), '-o', binary], check=True)
            cls.binaries.append(binary)
        cls.binary = cls.binaries[0]
        cls.batched_binary = str(directory / 'batched-test')
        subprocess.run([shutil.which('g++') or 'g++', '-std=c++11', '-DRESAC_COMMAND_GPIO=1',
                        '-I' + str(directory), '-I' + str(root / 'include'), str(path), '-o', cls.batched_binary], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_commands_are_checked_before_housekeeping_is_due(self):
        subprocess.run([self.binary, 'cadence'], check=True)

    def test_new_commands_refresh_invalidation_heartbeat_and_session_guards(self):
        for binary in self.binaries:
            subprocess.run([binary, 'safety'], check=True)

    def test_command_schedule_survives_millis_rollover(self):
        for binary in self.binaries:
            subprocess.run([binary, 'rollover'], check=True)

    def test_housekeeping_runs_after_physical_ir_dwell(self):
        for binary in self.binaries:
            subprocess.run([binary, 'dwell'], check=True)


    def test_gpio_reads_commands_only_on_notification_and_handles_masked_interrupts(self):
        subprocess.run([self.binaries[1], 'gpio'], check=True)

    def test_gpio_boot_discards_stale_command_and_held_signal(self):
        subprocess.run([self.binaries[1], 'boot'], check=True)

    def test_batched_completion_and_atomic_safety_remain_fail_closed(self):
        subprocess.run([self.batched_binary, 'batch'], check=True)

    def test_commands_preempt_between_housekeeping_transactions(self):
        subprocess.run([self.batched_binary, 'priority'], check=True)

    def test_batched_boot_still_discards_stale_commands(self):
        subprocess.run([self.batched_binary, 'boot'], check=True)

    def test_sensor_publication_returns_to_command_loop_between_ordered_stages(self):
        subprocess.run([self.batched_binary, 'sensor-priority'], check=True)
