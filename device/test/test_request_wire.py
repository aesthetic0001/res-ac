import json
import math
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

from test_agent import agent
import command_wire
import request_wire as wire
import cooperative_json


class RequestWireTests(unittest.TestCase):
    def test_telemetry_encoding_switches_to_json_if_server_support_disappears(self):
        from types import SimpleNamespace
        requests=[]
        class Upload:
            completed=False
            def __init__(self, config, payload, **options):
                self.payload,self.options=payload,options;requests.append(self)
            def close(self):pass
        transport=SimpleNamespace(binary_enabled=True)
        telemetry=agent.Telemetry({'native_bridge':True,'_http':transport});pending=agent.Buffer(2)
        with patch.object(agent,'CurlUpload',Upload),patch.object(telemetry,'resources',return_value={}):
            for i in range(32):telemetry.event('bridge.dispatch',attributes={'number':i})
            telemetry.tick(pending);self.assertTrue(telemetry.encoding_binary)
            ident=telemetry.batch['batchId'];transport.binary_enabled=False
            for _ in range(100):
                telemetry.tick(pending)
                if requests:break
            self.assertEqual(len(requests),1);self.assertEqual(requests[0].payload['batchId'],ident)
            self.assertFalse(requests[0].options.get('binary_body',False))
            self.assertEqual(json.loads(requests[0].options['encoded_body']),requests[0].payload)

    def test_binary_telemetry_keeps_the_batch_identity_across_415_and_json_retry(self):
        from types import SimpleNamespace
        requests=[];clock=[100.0]
        class Upload:
            completed=True
            def __init__(self, config, payload, **options):
                self.payload,self.options=payload,options;requests.append(self)
            def finish(self):return (415 if len(requests)==1 else 200),b'{}'
            def close(self):pass
        transport=SimpleNamespace(binary_enabled=True)
        telemetry=agent.Telemetry({'native_bridge':True,'_http':transport});pending=agent.Buffer(2)
        with patch.object(agent,'CurlUpload',Upload),patch.object(agent,'monotonic',lambda:clock[0]),patch.object(telemetry,'resources',return_value={}):
            telemetry.event('bridge.dispatch')
            for _ in range(100):
                telemetry.tick(pending)
                if requests:break
            self.assertTrue(requests[0].options['binary_body']);ident=requests[0].payload['batchId']
            transport.binary_enabled=False;telemetry.tick(pending)
            clock[0]=telemetry.next_due
            for _ in range(100):
                telemetry.tick(pending)
                if len(requests)>1:break
            self.assertEqual(len(requests),2);self.assertEqual(requests[1].payload['batchId'],ident)
            self.assertFalse(requests[1].options.get('binary_body',False))
            self.assertEqual(json.loads(requests[1].options['encoded_body']),requests[1].payload)

    def test_values_match_existing_rac1_decoding_in_small_steps(self):
        cases = json.loads((Path(__file__).resolve().parents[2] / 'test/fixtures/command-wire.json').read_text())
        values = [c['value'] for c in cases] + [None, True, False, -2147483648, 2147483648,
                  -0.0, 1.234567890123456, '\ufefftext\0\u00e9\U0001f600', {'': []}]
        for value in values:
            encoding = wire.Encoding(value); result = None; steps = 0
            while result is None:
                before = encoding.size; result = encoding.step(7); steps += 1
                self.assertLessEqual(encoding.size - before, 7)
                self.assertLess(steps, 10000)
            self.assertEqual(command_wire.decode(result), value)
        self.assertEqual(math.copysign(1, command_wire.decode(wire.encode(-0.0))), -1)

    def test_bounds_nonfinite_values_and_cycles_before_transmission(self):
        cycle = []; cycle.append(cycle)
        for value in [float('nan'), float('inf'), 10**1000, '\ud800', set(), cycle,
                      [None] * 8192, {i: None for i in range(2)}, 'x' * 32768]:
            with self.assertRaises((ValueError, UnicodeError)): wire.encode(value)
        encoding = wire.Encoding({'value': 'x' * 1000}, limit=100)
        with self.assertRaises(ValueError):
            while encoding.step() is None: pass

    def test_legacy_byte_strings_validate_utf8_and_do_not_alias_unicode_keys(self):
        with patch.object(wire, 'strings', (str, bytes)):
            self.assertEqual(command_wire.decode(wire.encode({b'text': b'\xc3\xa9'})), {'text': '\u00e9'})
            for value in [b'\xff', b'\xed\xa0\x80', {b'\xc3\xa9': 1, '\u00e9': 2}]:
                with self.assertRaises((ValueError, UnicodeError)): wire.encode(value)

    def test_byte_and_time_work_budgets_yield_and_key_cache_is_bounded(self):
        value = {'events': [{'name': 'command.execute', 'duration': 1.25, 'number': i} for i in range(32)]}
        encoding = wire.Encoding(value); clock = [0.0]
        def read(): clock[0] += .002; return clock[0]
        with patch.object(cooperative_json.time, 'time', read):
            self.assertIsNone(encoding.step())
            self.assertLess(encoding.size, 512)
        result = None
        while result is None: result = encoding.step()
        self.assertEqual(command_wire.decode(result), value)
        encoding = wire.Encoding(dict(('key%d' % i, i) for i in range(100)))
        while encoding.step() is None: pass
        self.assertLessEqual(len(encoding.keys), 64)

