import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'device' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


timing = module('timing_trace')
with patch.dict(sys.modules, {'timing_trace': timing}):
    bridge_timing = module('bridge_timing')

IDENT = '12345678-1234-4234-8234-123456789abc'


class TimingTraceTests(unittest.TestCase):
    def test_ring_is_bounded_and_has_no_io_until_frozen_export(self):
        trace = timing.TimingTrace(16, timer=lambda: 12.25)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'capture.json'
            for _ in range(20):
                trace.record('agent.gpio_assert', command=IDENT)
            self.assertEqual(len(trace.events), 16)
            self.assertEqual(trace.dropped, 4)
            self.assertFalse(output.exists())
            with self.assertRaises(ValueError):
                trace.write(str(output))
            trace.request_dump()
            trace.record('agent.gpio_assert')
            trace.write(str(output))
            payload = json.loads(output.read_text())
            self.assertEqual(payload['dropped'], 4)
            self.assertEqual(payload['events'][0][0], 12250000)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            trace.request_dump()  # A second signal must not try to overwrite the capture.
            self.assertFalse(trace.dump_requested)
            with self.assertRaises(ValueError):
                trace.write(str(output))

    def test_trace_accepts_only_fixed_events_numeric_metadata_and_uuid(self):
        trace = timing.TimingTrace()
        for kwargs in [dict(event='TOKEN=secret'), dict(event='uart.frame_end', value='body'),
                       dict(event='agent.gpio_assert', command='session-secret')]:
            with self.assertRaises(ValueError):
                trace.record(**kwargs)
        with self.assertRaises(ValueError):
            trace.validate_output('/etc/res-ac/timing.json')
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'existing'
            target.symlink_to('/etc/passwd')
            with self.assertRaises(ValueError):
                trace.validate_output(str(target))

    def test_legacy_json_writer_exports_the_same_bounded_schema(self):
        trace=timing.TimingTrace(timer=lambda:100.0)
        trace.record('uart.frame_end')
        trace.request_dump()
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'legacy.json'
            with patch.dict(sys.modules, {'json':SimpleNamespace(write=json.dumps)}):
                trace.write(str(target))
            self.assertEqual(json.loads(target.read_text())['events'][0][1], 'uart.frame_end')

    def test_correlates_both_completion_versions_without_saving_bodies(self):
        for frame in ['v1,%s,ping' % IDENT, 'v5,1,2,3,v1,%s,pong,123' % IDENT,
                      'v1,resac_command_result,v6,1,2,3,4,1,0,c,v3,%s,calibrated,%s' % (IDENT, 'a'*32)]:
            self.assertEqual(timing.command_id(frame), IDENT)
            self.assertEqual(timing.command_id(frame.encode()), IDENT)
        for bad in [None, {}, 'v6,invalid', 'v1,resac_sample,private']:
            self.assertIsNone(timing.command_id(bad))

    def test_diagnostic_imports_preserve_the_vendor_json_module_selection(self):
        source = "import sys;sys.path.insert(0,%r);import run_bridge_timing,timing_trace,bridge_timing;assert 'json' not in sys.modules" % str(ROOT/'device')
        subprocess.run([sys.executable, '-S', '-c', source], check=True)

    def test_launcher_uses_vendor_json_and_never_writes_trace_to_uart(self):
        with tempfile.TemporaryDirectory() as directory:
            vendor=Path(directory)/'bridge';vendor.mkdir()
            (vendor/'json.py').write_text("def write(value): return '{\"vendorJson\":true}'\n")
            (vendor/'packet.py').write_text("""class CRC:
 def check(self, value): return value
class PacketReader:
 def __init__(self,processor): self.processor=processor
 def process(self): print('FRAME'); return True
 def t_read(self): return None
def send(index,msg): pass
""")
            (vendor/'mailbox.py').write_text("""import json
class Server:
 clients=[]
 clients_sendbuffer={}
 def run(self): pass
 def write(self,value): pass
class Mailbox:
 def data_store_put(self,key,value): pass
json_server=Server()
mailbox=Mailbox()
""")
            (vendor/'bridge.py').write_text("""import os,signal,packet
class Processor:
 def run(self): pass
 def process(self,data): return ''
reader=packet.PacketReader(Processor())
os.kill(os.getpid(),signal.SIGUSR1)
reader.process()
""")
            output=Path(directory)/'capture.json'
            result=subprocess.run([sys.executable,'-S',str(ROOT/'device/run_bridge_timing.py'),
                '--bridge-dir',str(vendor),'--output',str(output)],capture_output=True,check=True)
            self.assertEqual(result.stdout,b'FRAME\n')
            self.assertEqual(json.loads(output.read_text()),{'vendorJson':True})

    def test_uart_control_markers_arm_and_freeze_both_capture_endpoints(self):
        trace=timing.TimingTrace()
        trace.record('uart.frame_end')
        trace.request_dump()
        self.assertTrue(trace.arm())
        self.assertFalse(trace.dump_requested)
        self.assertFalse(trace.events)
        self.assertTrue(trace.active)

    def test_clock_failure_freezes_diagnostics_without_raising_into_control(self):
        def failed():
            raise OSError('clock unavailable')
        trace = timing.TimingTrace(timer=failed)
        trace.record('agent.gpio_assert')
        self.assertFalse(trace.active)
        self.assertTrue(trace.dump_requested)
        self.assertEqual(trace.clock_errors, 1)


class BridgeTimingTests(unittest.TestCase):
    def setUp(self):
        self.trace = timing.TimingTrace(timer=lambda: 100.0)
        self.wire, self.handled = [], []
        wire, handled = self.wire, self.handled
        self.client = object()
        class Server:
            clients = [self.client]
            clients_sendbuffer = {self.client: b''}
            quota = 100000
            def write(server, value):
                encoded = json.dumps(value).encode()
                for client in server.clients:
                    server.clients_sendbuffer[client] += encoded
            def run(server):
                for client in server.clients:
                    data = server.clients_sendbuffer[client]
                    wire.append(data[:server.quota])
                    server.clients_sendbuffer[client] = data[server.quota:]
        self.server = Server()
        self.mailbox = SimpleNamespace(data_store_put=lambda key, value: handled.append((key, value)))
        server = self.server
        class Processor:
            def register(self, *unused):
                pass
            def run(self):
                server.run()
            def process(self, data):
                handled.append(data)
                return 'ACK'
        class CRC:
            def check(self, expected):
                return expected
        class Reader:
            def __init__(self, processor):
                self.processor = processor
                self.bytes = iter(['\xff', 'Mpayload'])
            def t_read(self):
                return next(self.bytes)
            def process(self):
                self.processor.run()
                self.t_read()
                data = self.t_read()
                if not CRC().check(True):
                    return None
                packet.send(7, self.processor.process(data))
                return True
        packet = SimpleNamespace(PacketReader=Reader, CRC=CRC, send=lambda index, data: wire.append((index, data)))
        self.packet, self.processor = packet, Processor()
        self.hooks = bridge_timing.BridgeTiming(self.trace).install(packet, server, self.mailbox)
        self.addCleanup(self.hooks.uninstall)

    def events(self, name):
        return [row for row in self.trace.events if row[1] == name]

    def test_packet_wrappers_preserve_results_and_response_bytes(self):
        reader = self.packet.PacketReader(self.processor)
        self.assertIs(reader.process(), True)
        self.assertEqual(self.handled, ['Mpayload'])
        self.assertEqual(self.wire[-1], (7, 'ACK'))
        names = [r[1] for r in self.trace.events]
        for name in ['uart.wait_begin', 'uart.frame_start', 'uart.frame_parsed', 'uart.handler_end',
                     'uart.ack_begin', 'uart.ack_end', 'uart.frame_end']:
            self.assertIn(name, names)
        self.assertLess(names.index('uart.frame_start'), names.index('uart.frame_parsed'))
        self.assertLess(names.index('uart.ack_end'), names.index('uart.frame_end'))
        self.hooks.uninstall()
        self.assertIs(self.packet.PacketReader(self.processor).process(), True)

    def test_socket_completion_means_all_bytes_accepted_and_handles_partial_writes(self):
        self.mailbox.data_store_put('resac_command', 'v1,%s,ping' % IDENT)
        self.server.write(dict(response='put', key='resac_command', value='v1,%s,ping' % IDENT))
        self.server.write(dict(request='raw', data='v1,resac_command_result,v1,%s,pong,123' % IDENT))
        expected = self.server.clients_sendbuffer[self.client]
        self.server.quota = 3
        self.server.run()
        self.assertFalse(self.events('bridge.socket_flushed'))
        self.server.quota = 100000
        self.server.run()
        self.assertEqual(b''.join(self.wire), expected)
        self.assertEqual([r[3] for r in self.events('bridge.socket_flushed')], [1, 2])
        self.assertEqual([r[5] for r in self.events('bridge.socket_flushed')], [IDENT, IDENT])
        capture = json.dumps(list(self.trace.events))
        self.assertNotIn('pong', capture)
        self.assertNotIn('resac_command', capture)

    def test_close_is_not_a_successful_flush_and_metadata_stays_bounded(self):
        for _ in range(40):
            self.server.write(dict(request='raw', data='v1,resac_command_result,v1,%s,pong,1' % IDENT))
        self.assertEqual(len(self.hooks.pending[self.client]), 32)
        self.assertGreater(self.trace.dropped, 0)
        self.server.clients.clear()
        self.server.clients_sendbuffer.clear()
        self.server.run()
        self.assertTrue(self.events('bridge.client_closed'))
        self.assertFalse(self.events('bridge.socket_flushed'))
        self.assertFalse(self.hooks.pending)

    def test_datastore_tags_accept_binary_separator_after_latin1_decode(self):
        self.packet.PacketReader(self.processor)
        self.assertEqual(self.processor.process(b'Dresac_command_result\xfev1,reply'), 'ACK')
        self.assertEqual(self.events('uart.frame_parsed')[-1][3], 35)

    def test_explicit_uart_markers_reset_and_freeze_bridge_capture(self):
        self.trace.record('uart.frame_end')
        self.mailbox.data_store_put('resac_timing_arm','1')
        self.assertTrue(self.trace.active)
        self.assertFalse(self.trace.events)
        self.mailbox.data_store_put('resac_timing_dump','1')
        self.assertFalse(self.trace.active)
        self.assertTrue(self.trace.dump_requested)

    def test_bad_crc_is_reported_without_changing_original_result(self):
        self.assertFalse(self.packet.CRC().check(False))
        self.assertEqual(len(self.events('uart.crc_failed')), 1)
        self.trace.request_dump()
        before = len(self.trace.events)
        self.server.write(dict(request='raw', data='v1,resac_command_result,v1,%s,pong,1' % IDENT))
        self.assertEqual(len(self.trace.events), before)


class AgentTimingTests(unittest.TestCase):
    def test_trace_separates_socket_receipt_from_completion_consumption(self):
        from test_agent import agent
        from test_bridge_events import ready_bridge, deliver, event
        from unittest.mock import patch
        bridge = ready_bridge(resac_command_protocol='batched-v1', resac_command_notify=agent.COMMAND_GPIO_PROTOCOL)
        clock = [100.0]
        trace = timing.TimingTrace(timer=lambda: clock[0])
        bridge.timing = trace
        bridge.command_gpio = SimpleNamespace(open=lambda: None, set=lambda pending: None, close=lambda: None)
        with tempfile.TemporaryDirectory() as directory, patch.object(agent, 'monotonic', lambda: clock[0]), \
                patch.dict(sys.modules, {'timing_trace': timing}):
            processor = agent.CommandProcessor(dict(_bridge=bridge, _timing_trace=trace), state_path=str(Path(directory)/'journal'))
            processor._accept(dict(id=IDENT, type='device.ping', payload={}))
            processor._arduino()
            frame = bridge.notification['frame']
            deliver(bridge, dict(response='put', key='resac_command', value=frame))
            clock[0] += .1
            payload = json.dumps(event('resac_command_result','v5,40,0,0,v1,%s,pong,1' % IDENT)).encode()
            connection = SimpleNamespace(recv=lambda size:payload, close=lambda:None)
            bridge.connection = connection
            with patch.object(agent.select, 'select', return_value=([connection], [], [])):
                bridge.poll()
            clock[0] += .25
            processor._arduino()
            self.assertEqual(processor.reply['status'], 'succeeded')
        rows = {row[1]:row for row in trace.events}
        self.assertLessEqual(rows['agent.recv_begin'][0], rows['agent.json_begin'][0])
        self.assertEqual(rows['agent.completion_received'][5], IDENT)
        self.assertEqual(rows['agent.completion_consumed'][0]-rows['agent.completion_received'][0],250000)
        self.assertLessEqual(rows['agent.gpio_begin'][0], rows['agent.gpio_assert'][0])
