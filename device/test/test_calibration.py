import shutil
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from test_agent import agent
from test_ir import Bridge, SESSION, command


class CalibrationTests(unittest.TestCase):
    def run_native(self, source):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fan.cpp'
            path.write_text(source)
            binary = str(Path(directory) / 'fan-test')
            subprocess.run([shutil.which('g++') or 'g++', '-std=c++11', '-I'+str(root/'include'), str(path), '-o', binary], check=True)
            subprocess.run([binary], check=True)

    def test_estimate_clamps_speed_ignores_off_presses_and_remembers_speed(self):
        self.run_native(r'''
        #include <cassert>
        #include <initializer_list>
        #include "FanState.h"
        int main() {
          FanState f;
          assert(!f.calibrated()); assert(!f.afterSend(0xDE80, 0, 0));
          f.calibrateOff(); assert(!f.power() && f.speed()==0);
          f.afterSend(0xDE80, 0x16, 0);
          f.afterSend(0xDE80, 0x14, 0);
          f.afterSend(0xDE80, 0, 0); assert(f.power() && f.speed()==1);
          f.afterSend(0xDE80, 0x14, 0); assert(f.speed()==1);
          for(int i=0;i<10;++i) f.afterSend(0xDE80, 0x16, 0);
          assert(f.speed()==5);
          f.afterSend(0xDE80, 0, 0); assert(!f.power() && f.speed()==0);
          for(int i=0;i<10;++i) f.afterSend(0xDE80, 0x14, 0);
          f.afterSend(0xDE80, 0, 0); assert(f.speed()==5);
          for(int i=0;i<10;++i) f.afterSend(0xDE80, 0x14, 0);
          assert(f.speed()==1);
          for(int from=1;from<=5;++from) for(int to=1;to<=5;++to) {
            int steps=FanState::speedSteps(from,to);
            assert(steps==to-from);
            f.calibrateOff(); f.afterSend(0xDE80, 0, 0);
            for(int i=1;i<from;++i) f.afterSend(0xDE80, 0x16, 0);
            for(int i=0;i<(steps<0?-steps:steps);++i)
              f.afterSend(0xDE80, steps<0?0x14:0x16, 0);
            assert(f.speed()==to);
            f.afterSend(0xDE80, 0, 0); f.afterSend(0xDE80, 0, 0);
            assert(f.speed()==to);
          }
          assert(FanState::speedSteps(1,5)==4);
          assert(FanState::speedSteps(5,1)==-4);
          assert(f.afterSend(0xDE80, 0x15, 0));
          for(int code : {0x08, 0x10, 0x12, 0x09}) {
            f.calibrateOff(); assert(!f.afterSend(0xDE80, code, 0)); assert(!f.calibrated());
          }
          f.calibrateOff(); assert(!f.afterSend(0xDE80, 0x16, 1));
          f.calibrateOff(); assert(!f.afterSend(123, 0, 0));
          f.calibrateOff(); f.reset(); assert(!f.calibrated());
          f.calibrateOff(); f.afterSend(0xDE80, 0, 0); assert(f.speed()==1);
        }''')

    def test_firmware_targets_power_on_before_stepping_and_never_wrap(self):
        root = Path(__file__).resolve().parents[2]
        firmware = (root / 'src/main.cpp').read_text()
        control = firmware[firmware.index('bool sendFanIR('):firmware.index('bool validCommandId(')]
        self.run_native(r'''
        #include <cassert>
        #include <vector>
        #include "FanState.h"
        #define F(s) s
        #define HEX 16
        FanState fan;
        const char *fanControlSource = "reset";
        unsigned long fanRevision = 0;
        std::vector<int> sent;
        struct {
          template<typename... Args> void print(Args...) {}
          template<typename... Args> void println(Args...) {}
        } Serial;
        struct {
          void sendNEC(uint16_t address, uint8_t command, uint8_t repeats) { sent.push_back(command); }
        } IrSender;
        void publishFanState() {}
        void invalidateFan() { fan.reset(); }
        void delay(unsigned long) {}
        ''' + control + r'''
        int main() {
          uint8_t presses;
          assert(!setFanLevel(1, false, presses)); assert(sent.empty());
          fan.calibrateOff();
          assert(!sendFanIR(0xDE80, 0x16, 0)); assert(sent.empty());
          assert(!sendFanIR(0xDE80, 0x14, 0)); assert(sent.empty());
          assert(setFanLevel(5, true, presses)); assert(presses==5);
          assert((sent==std::vector<int>{0,0x16,0x16,0x16,0x16}));
          assert(fan.speed()==5);
          for(int from=1;from<=5;++from) for(int to=1;to<=5;++to) {
            assert(setFanLevel(from, false, presses));
            sent.clear();
            assert(setFanLevel(to, false, presses));
            assert(presses==(from<to?to-from:from-to)); assert(fan.speed()==to);
            for(int code : sent) assert(code==(from<to?0x16:0x14));
            assert(setFanLevel(0, false, presses)); assert(presses==1 && !fan.power());
            sent.clear();
            assert(setFanLevel(0, false, presses)); assert(presses==0 && sent.empty());
            assert(setFanLevel(to, false, presses)); assert(presses==1 && fan.speed()==to);
            assert((sent==std::vector<int>{0}));
          }
          sent.clear(); assert(!setFanLevel(6, false, presses)); assert(sent.empty());
        }''')

    def test_uncalibrated_and_old_session_commands_do_not_reach_mailbox(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = Bridge()
            processor = agent.CommandProcessor({}, bridge, str(Path(directory)/'state.json'))
            bridge.values['resac_fan_state'] = 'v1,%s,0,0,0,1' % SESSION
            processor._accept(command())
            self.assertIn('Calibration required', processor.reply['result']['error'])
            processor._accept(command(type='fan.calibrate', payload=dict(confirmOff=True, sessionId='b'*32)))
            self.assertIn('session changed', processor.reply['result']['error'])
            processor._accept(command(type='fan.calibrate', payload=dict(confirmOff=False, sessionId=SESSION)))
            self.assertEqual(processor.reply['status'], 'failed')
            self.assertFalse(bridge.sent)

    def test_calibration_needs_matching_avr_ack_and_session(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = Bridge()
            bridge.values['resac_fan_state'] = 'v1,%s,0,0,0,1' % SESSION
            processor = agent.CommandProcessor({}, bridge, str(Path(directory)/'state.json'))
            request = command(type='fan.calibrate', payload=dict(confirmOff=True, sessionId=SESSION))
            processor._accept(request)
            processor._arduino()
            self.assertEqual(bridge.sent, ['v3,%s,calibrate,%s' % (request['id'], SESSION)])
            self.assertIsNone(processor.reply)
            bridge.values['resac_command_result'] = 'v3,%s,calibrated,%s' % (request['id'], 'b'*32)
            processor.bridge_due = 0
            processor._arduino()
            self.assertIsNone(processor.reply)
            bridge.values['resac_command_result'] = 'v3,%s,calibrated,%s' % (request['id'], SESSION)
            processor.bridge_due = 0
            processor._arduino()
            self.assertEqual(processor.reply['result']['reply'], 'calibrated')

    def test_timeout_invalidates_estimate_and_old_boot_rejection_is_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = Bridge()
            processor = agent.CommandProcessor({}, bridge, str(Path(directory)/'state.json'))
            request = command()
            processor._accept(request); processor._arduino()
            with patch.object(agent, 'monotonic', return_value=processor.deadline+1):
                processor._arduino()
            self.assertEqual(bridge.values['resac_fan_invalidate'], '1')
            processor.reply = None
            request = command()
            processor._accept(request); processor._arduino()
            bridge.values['resac_command_result'] = 'v3,%s,rejected,%s' % (request['id'], SESSION)
            processor.bridge_due = 0
            processor._arduino()
            self.assertEqual(processor.reply['status'], 'failed')
            self.assertIn('session', processor.reply['result']['error'])

    def test_reporter_initializes_watchdog_and_uploads_live_state(self):
        bridge = Bridge()
        bridge.values['resac_fan_session'] = SESSION
        calls = []
        class Request:
            def __init__(self, config, payload, route):
                calls.append((payload, route)); self.process = self
            def poll(self): return None
            def close(self): pass
        with patch.object(agent, 'CurlUpload', Request):
            reporter = agent.FanReporter({}, bridge)
            reporter.tick()
            self.assertEqual(bridge.values['resac_fan_invalidate'], '1')
            self.assertEqual(bridge.values['resac_fan_heartbeat'], '1')
            self.assertEqual(calls[0][1], 'fan-state')
            self.assertEqual(calls[0][0]['power'], 'on')
            reporter.next_due = 0
            reporter.tick()
            self.assertEqual(len(calls), 1)  # In-flight POST never blocks the heartbeat.
            self.assertEqual(bridge.values['resac_fan_heartbeat'], '2')
        self.assertEqual(agent.parse_fan_state('v1,none,0,0,0,0')['power'], 'unknown')
        for frame in ['v1,none,1,1,1,0', 'v1,%s,0,1,1,0' % SESSION, 'v1,%s,1,0,5,0' % SESSION]:
            with self.assertRaises(ValueError): agent.parse_fan_state(frame)
