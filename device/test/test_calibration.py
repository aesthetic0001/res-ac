import shutil
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from test_agent import agent
from test_ir import Bridge, SESSION, command


class CalibrationTests(unittest.TestCase):
    def test_estimate_core_starts_locked_wraps_speed_and_loses_state(self):
        root = Path(__file__).resolve().parents[2]
        source = r'''
        #include <cassert>
        #include "FanState.h"
        int main() {
          FanState f;
          assert(!f.calibrated()); assert(!f.afterSend(0xDE80, 0, 0));
          f.calibrateOff(); assert(!f.power() && f.speed()==0);
          f.afterSend(0xDE80, 0, 0); assert(f.power() && f.speed()==1);
          for(int i=0;i<10;++i) f.afterSend(0xDE80, 8, 0);
          assert(f.speed()==1);
          f.afterSend(0xDE80, 16, 0); assert(f.speed()==5);
          f.afterSend(0xDE80, 0, 0); assert(!f.power());
          f.afterSend(0xDE80, 0, 0); assert(f.speed()==5);
          for(int i=0;i<10;++i) f.afterSend(0xDE80, 16, 0);
          assert(f.speed()==5);
          for(int i=0;i<4;++i) f.afterSend(0xDE80, 16, 0);
          assert(f.speed()==1);
          for(int from=1;from<=5;++from) for(int to=1;to<=5;++to) {
            int route=FanState::shortestRoute(from,to);
            assert(route>=-2 && route<=2);
            assert(((from-1+route+5)%5)+1==to);
            int forward=(to-from+5)%5,backward=(from-to+5)%5;
            assert((route<0?-route:route)==(forward<backward?forward:backward));
          }
          assert(FanState::shortestRoute(3,4)==1);
          assert(FanState::shortestRoute(1,5)==-1);
          assert(FanState::shortestRoute(5,1)==1);
          assert(f.afterSend(0xDE80, 21, 0));
          assert(!f.afterSend(0xDE80, 18, 0)); assert(!f.calibrated());
          f.calibrateOff(); assert(!f.afterSend(0xDE80, 9, 0));
          f.calibrateOff(); assert(!f.afterSend(0xDE80, 8, 1));
          f.calibrateOff(); assert(!f.afterSend(123, 0, 0));
          f.calibrateOff(); f.reset(); assert(!f.calibrated());
        }'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fan.cpp'
            path.write_text(source)
            binary = str(Path(directory) / 'fan-test')
            subprocess.run([shutil.which('g++') or 'g++', '-std=c++11', '-I'+str(root/'include'), str(path), '-o', binary], check=True)
            subprocess.run([binary], check=True)

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
            self.assertIn('session changed', processor.reply['result']['error'])

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
