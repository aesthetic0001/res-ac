import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from test_agent import agent

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('command_wire', ROOT / 'device/command_wire.py')
wire = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wire)
CASES = json.loads((ROOT / 'test/fixtures/command-wire.json').read_text())


class Request:
    def __init__(self, config, payload=None, route=None, **kwargs):
        self.route = route
        self.response = (200, b'{}')
        self.completed = True
    def finish(self): return self.response
    def close(self): pass


class CommandWireTests(unittest.TestCase):
    def test_shared_node_vectors_preserve_command_fields_and_types(self):
        for item in CASES:
            self.assertEqual(wire.decode(bytes.fromhex(item['hex'])), item['value'])
        self.assertIsInstance(wire.decode(b'RAC1i\x00\x00\xff\xff'), int)
        self.assertIs(wire.decode(b'RAC1t'), True)

    def test_every_truncation_invalid_type_duplicate_key_and_extra_data_rejected(self):
        frame = bytes.fromhex(CASES[0]['hex'])
        for size in range(len(frame)):
            with self.assertRaises(ValueError): wire.decode(frame[:size])
        invalid = [frame + b'n', b'RAC2n', b'RAC1x', b'RAC1s\x00\x01\xff',
                   b'RAC1o\x00\x01nn', b'RAC1o\x00\x02s\x00\x01kns\x00\x01kn',
                   b'RAC1d' + struct.pack('!d', float('nan')),
                   b'RAC1d' + struct.pack('!d', float('inf')),
                   b'RAC1a\xff\xff', b'RAC1o\xff\xff',
                   b'RAC1' + b'a\x00\x01' * 17 + b'n', b'RAC1s\x7f\xff' + b'x' * 32767]
        for value in invalid:
            with self.assertRaises(ValueError): wire.decode(value)

    def test_binary_command_uses_existing_validation_and_json_fallback(self):
        # Both representations pass through _accept, so an invalid UUID still fails.
        for native, body in [(True, bytes.fromhex(CASES[0]['hex'])),
                             (True, json.dumps(CASES[0]['value']).encode()),
                             (False, json.dumps(CASES[0]['value']).encode())]:
            with tempfile.TemporaryDirectory() as directory, patch.dict('sys.modules', command_wire=wire):
                processor = agent.CommandProcessor(dict(native_bridge=native), bridge=object(),
                    state_path=str(Path(directory) / 'journal'))
                request = Request({}); request.response = (200, body)
                processor.request, processor.request_kind = request, 'poll'
                with patch.object(processor, '_arduino') as dispatch:
                    processor.tick()
                self.assertEqual(processor.current['id'], CASES[0]['value']['commands'][0]['id'])
                self.assertIn('command.timing.decode_ms', processor.phase_timings)
                dispatch.assert_called_once()
        bad = bytes.fromhex(CASES[0]['hex']).replace(b'11111111-1111-4111-8111-111111111111', b'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx')
        with tempfile.TemporaryDirectory() as directory, patch.dict('sys.modules', command_wire=wire):
            processor = agent.CommandProcessor(dict(native_bridge=True), bridge=object(), state_path=str(Path(directory) / 'journal'))
            request = Request({}); request.response = (200, bad)
            processor.request, processor.request_kind = request, 'poll'
            processor.tick(allow_execution=False)
            self.assertIsNone(processor.current)

    def test_only_native_mode_requests_binary_and_keeps_telemetry_kind(self):
        class Bridge:
            def get(self, key): return 'v4'
        for native in (True, False):
            with tempfile.TemporaryDirectory() as directory, patch.object(agent, 'CurlUpload', Request):
                processor = agent.CommandProcessor(dict(native_bridge=native), bridge=Bridge(), state_path=str(Path(directory) / 'journal'))
                processor.tick()
                self.assertEqual(processor.request.route, 'commands?format=binary-v1' if native else 'commands')
        class Transport:
            def submit(self, *args, **kwargs): return agent.HttpResult(1)
        upload = agent.CurlUpload(dict(_http=Transport()), route='commands?format=binary-v1', method='GET')
        self.assertEqual(upload.kind, 'commands')
