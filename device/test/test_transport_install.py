import gzip
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def elf(interpreter=False):
    data = bytearray(84)
    data[:6] = b'\x7fELF\x01\x02'
    data[16:20] = b'\x00\x02\x00\x08'
    data[28:32] = (52).to_bytes(4, 'big')
    data[42:44] = (32).to_bytes(2, 'big')
    data[44:46] = (1).to_bytes(2, 'big')
    data[52:56] = (3 if interpreter else 1).to_bytes(4, 'big')
    return bytes(data)


class TransportInstallTests(unittest.TestCase):
    def setUp(self):
        self.events, self.uploads = [], {}
        self.probe_ok = True
        outer = self

        class Console:
            def __init__(self, port):
                outer.events.append(('open', port))

            def upload(self, data, destination, mode=0o600, progress=None):
                outer.events.append(('upload', destination))
                outer.uploads[destination] = data

            def run(self, command, **kwargs):
                outer.events.append(('run', command))
                if command == '/tmp/res-ac-http.pending --version':
                    return b'res-ac-http protocol=1 libcurl=8.23.0'
                if '--probe' in command:
                    config = json.loads(outer.uploads['/etc/res-ac/agent.pending.json'])
                    outer.assertEqual(config['http_helper'], '/tmp/res-ac-http.pending')
                    return b'PASS: verified HTTPS' if outer.probe_ok else b'FAIL'
                return b''

            def close(self):
                outer.events.append(('close', 'console'))

        root = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location('installer', root / 'scripts/install_device.py')
        self.installer = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'serial_console': SimpleNamespace(Console=Console)}):
            spec.loader.exec_module(self.installer)

    def invoke(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / 'helper'
            helper.write_bytes(elf())
            config = Path(directory) / 'device.json'
            config.write_text(json.dumps(dict(server_url='https://example.invalid', device_id='board',
                                              device_token='PRIVATE', ca_file='/etc/res-ac/ca-bundle.pem',
                                              curl_binary='/tmp/old-curl')))
            config.chmod(0o600)
            args = ['install_device.py', '--config', str(config), '--http-helper', str(helper), '--no-start']
            with patch.object(sys, 'argv', args), patch.object(self.installer, 'urlopen',
                    side_effect=lambda *args, **kwargs: io.BytesIO(b'-----BEGIN CERTIFICATE-----\nPUBLIC\n')):
                self.installer.main()

    def test_preflight_runs_staged_pair_before_replacing_existing_agent(self):
        self.invoke()
        probe = next(i for i, event in enumerate(self.events) if event[0] == 'run' and '--probe' in event[1])
        stop = next(i for i, event in enumerate(self.events) if event[0] == 'run' and 'res-ac-agent stop' in event[1])
        archive = self.events.index(('upload', '/usr/lib/res-ac/curl.gz'))
        agent = self.events.index(('upload', '/usr/lib/res-ac/res_ac_agent.py'))
        self.assertLess(probe, stop)
        self.assertLess(stop, archive)
        self.assertLess(probe, agent)
        self.assertEqual(gzip.decompress(self.uploads['/usr/lib/res-ac/curl.gz']), elf())
        active_config = json.loads(self.uploads['/etc/res-ac/agent.pending.json'])
        self.assertEqual(active_config['http_helper'], '/tmp/res-ac-http')
        self.assertNotIn('curl_binary', active_config)
        self.assertFalse(any(event[0] == 'run' and event[1] == '/etc/init.d/res-ac-agent start'
                             for event in self.events))

    def test_failed_preflight_keeps_existing_agent_and_transport(self):
        self.probe_ok = False
        with self.assertRaises(RuntimeError):
            self.invoke()
        self.assertNotIn('/usr/lib/res-ac/curl.gz', self.uploads)
        self.assertNotIn('/usr/lib/res-ac/res_ac_agent.py', self.uploads)
        self.assertFalse(any(event[0] == 'run' and 'res-ac-agent stop' in event[1] for event in self.events))
        self.assertEqual(self.events[-1], ('close', 'console'))

    def test_rejects_wrong_architecture_and_dynamic_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'helper'
            for content in [b'not ELF', elf(True), elf()[:60], elf().replace(b'\x7fELF\x01\x02', b'\x7fELF\x02\x01')]:
                path.write_bytes(content)
                with self.assertRaises(ValueError):
                    self.installer.http_helper(path)


if __name__ == '__main__':
    unittest.main()
