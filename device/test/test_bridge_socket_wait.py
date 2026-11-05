import importlib.util
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


wait = module('bridge_socket_wait', ROOT / 'device/bridge_socket_wait.py')
prepare = module('prepare_bridge_socket_wait', ROOT / 'scripts/prepare_bridge_socket_wait.py')


class BridgeSocketWaitTests(unittest.TestCase):
    def test_buffered_python_stdin_does_not_hide_remaining_uart_bytes(self):
        uart_read, uart_write = os.pipe()
        stream = os.fdopen(uart_read, 'rb')  # Match the live launcher's buffering.
        self.addCleanup(stream.close)
        self.addCleanup(os.close, uart_write)
        server = SimpleNamespace(sockets=[], clients=[], clients_sendbuffer={}, available=lambda: 0)
        processor = SimpleNamespace(run=lambda: None)
        os.write(uart_write, b'AB')
        with patch.dict(sys.modules, {'mailbox': SimpleNamespace(json_server=server)}):
            self.assertEqual(wait.read_byte(stream, processor), b'A')
            self.assertEqual(wait.read_byte(stream, processor), b'B')

    def test_patch_is_narrow_idempotent_and_rejects_unknown_source(self):
        source = ('prefix\n' + prepare.ORIGINAL + '\nsuffix\n').encode()
        result = prepare.prepare(source)
        self.assertEqual(result, ('prefix\n' + prepare.REPLACEMENT + '\nsuffix\n').encode())
        self.assertEqual(prepare.prepare(result), result)
        for source in (b'different implementation', (prepare.ORIGINAL * 2).encode()):
            with self.assertRaises(ValueError):
                prepare.prepare(source)

    def test_tcp_read_write_and_decoded_queue_wake_while_uart_is_idle(self):
        uart_read, uart_write = os.pipe()
        stream = os.fdopen(uart_read, 'rb', 0)
        client, peer = socket.socketpair()
        self.addCleanup(stream.close)
        self.addCleanup(os.close, uart_write)
        self.addCleanup(client.close)
        self.addCleanup(peer.close)
        queued, processed = [], []
        server = SimpleNamespace(sockets=[client], clients=[client],
                                 clients_sendbuffer={client: b''}, available=lambda: len(queued))

        def run():
            readable, writable, _ = wait.select.select([client], [client] if server.clients_sendbuffer[client] else [], [], 0)
            if readable:
                queued.extend(client.recv(1024).split(b','))
            if writable:
                client.sendall(server.clients_sendbuffer[client])
                server.clients_sendbuffer[client] = b''
                os.write(uart_write, b'X')
            if queued:
                processed.append(queued.pop(0))
                if len(processed) == 3:
                    server.clients_sendbuffer[client] = b'put confirmed'

        peer.sendall(b'heartbeat,session,command')
        with patch.dict(sys.modules, {'mailbox': SimpleNamespace(json_server=server)}):
            # A long UART timeout in this test demonstrates that TCP readiness,
            # not expiration of that timeout, drives all work and the response.
            with patch.object(wait, 'monotonic', return_value=100):
                self.assertEqual(wait.read_byte(stream, SimpleNamespace(run=run)), b'X')
        self.assertEqual(processed, [b'heartbeat', b'session', b'command'])
        self.assertEqual(peer.recv(100), b'put confirmed')

    def test_tcp_activity_does_not_abort_a_uart_byte_wait(self):
        uart_read, uart_write = os.pipe()
        stream = os.fdopen(uart_read, 'rb', 0)
        client, peer = socket.socketpair()
        self.addCleanup(stream.close)
        self.addCleanup(os.close, uart_write)
        self.addCleanup(client.close)
        self.addCleanup(peer.close)
        server = SimpleNamespace(sockets=[client], clients=[], clients_sendbuffer={}, available=lambda: 0)
        calls = []

        def run():
            calls.append(client.recv(1024))
            os.write(uart_write, b'B')

        peer.sendall(b'command')
        with patch.dict(sys.modules, {'mailbox': SimpleNamespace(json_server=server)}):
            self.assertEqual(wait.read_byte(stream, SimpleNamespace(run=run)), b'B')
        self.assertEqual(calls, [b'command'])

    def test_idle_retains_original_uart_timeout_and_prioritizes_ready_uart(self):
        uart_read, uart_write = os.pipe()
        stream = os.fdopen(uart_read, 'rb', 0)
        self.addCleanup(stream.close)
        self.addCleanup(os.close, uart_write)
        server = SimpleNamespace(sockets=[], clients=[], clients_sendbuffer={}, available=lambda: 0)
        processor = SimpleNamespace(run=lambda: self.fail('No runner work should be scheduled'))
        with patch.dict(sys.modules, {'mailbox': SimpleNamespace(json_server=server)}):
            with patch.object(wait.select, 'select', return_value=([], [], [])) as select:
                self.assertIsNone(wait.read_byte(stream, processor))
                self.assertGreater(select.call_args.args[3], 0)
                self.assertLessEqual(select.call_args.args[3], .050001)
            os.write(uart_write, b'A')
            self.assertEqual(wait.read_byte(stream, processor), b'A')

    def test_decoded_tcp_backlog_does_not_starve_ready_uart(self):
        uart_read, uart_write = os.pipe()
        stream = os.fdopen(uart_read, 'rb', 0)
        self.addCleanup(stream.close)
        self.addCleanup(os.close, uart_write)
        server = SimpleNamespace(available=lambda: 100)
        processor = SimpleNamespace(run=lambda: self.fail('UART must take priority over queued TCP work'))
        os.write(uart_write, b'A')
        with patch.dict(sys.modules, {'mailbox': SimpleNamespace(json_server=server)}):
            self.assertEqual(wait.read_byte(stream, processor), b'A')
