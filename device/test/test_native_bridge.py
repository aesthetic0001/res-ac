"""Exercise the real C service, UART CRC/retries, binary clients, and agent."""
import fcntl
import termios
import importlib.util
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('native_bridge_protocol',ROOT/'device/native_bridge_protocol.py')
protocol=importlib.util.module_from_spec(spec);spec.loader.exec_module(protocol)


def crc(data):
    result=0xffff
    for byte in data:
        result ^= byte
        for _ in range(8): result=(result>>1)^0x8408 if result&1 else result>>1
    return result


def packet(index,payload):
    data=b'\xff'+bytes([index])+struct.pack('!H',len(payload))+payload
    return data+struct.pack('!H',crc(data))


def frame(payload):return struct.pack('!I',len(payload))+payload


class NativeBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory=tempfile.TemporaryDirectory()
        cls.binary=Path(os.environ.get('RESAC_BRIDGE_HELPER',Path(cls.directory.name)/'bridge'))
        if not os.environ.get('RESAC_BRIDGE_HELPER'):
            subprocess.run(['cc','-std=c99','-O2','-Wall','-Wextra','-Werror',
                            str(ROOT/'device/native_bridge.c'),'-o',str(cls.binary)],check=True)

    @classmethod
    def tearDownClass(cls):cls.directory.cleanup()

    def setUp(self):
        self.process=subprocess.Popen([str(self.binary),'--port','0'],stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        self.addCleanup(self.stop)
        ready=self.process.stderr.readline().decode().split()
        self.assertEqual(ready[0],'READY');self.port=int(ready[1]);self.index=7
        self.send(b'XX100',b'\0')
        self.modules=patch.dict(sys.modules,{'native_bridge_protocol':protocol});self.modules.start()
        self.addCleanup(self.modules.stop)

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=3)
        for stream in [self.process.stdin,self.process.stdout,self.process.stderr]:stream.close()

    def read(self,n):
        data=b'';deadline=time.monotonic()+2
        while len(data)<n:
            self.assertTrue(select.select([self.process.stdout],[],[],max(0,deadline-time.monotonic()))[0],'UART response timed out')
            part=os.read(self.process.stdout.fileno(),n-len(data));self.assertTrue(part);data+=part
        return data

    def ack(self,index,expected):
        header=self.read(4);self.assertEqual(header[:2],b'\xff'+bytes([index]))
        length=struct.unpack('!H',header[2:])[0];body=self.read(length+2)
        self.assertEqual(struct.unpack('!H',body[-2:])[0],crc(header+body[:-2]));self.assertEqual(body[:-2],expected)

    def send(self,data,expected,index=None):
        if index is None:index=self.index;self.index=(self.index+1)&255
        self.process.stdin.write(packet(index,data));self.process.stdin.flush();self.ack(index,expected)

    def connect(self):
        client=socket.create_connection(('127.0.0.1',self.port));client.settimeout(2)
        self.addCleanup(client.close);return client

    def receive(self,client):
        def exact(n):
            data=b''
            while len(data)<n:
                block=client.recv(n-len(data));self.assertTrue(block);data+=block
            return data
        length=struct.unpack('!I',exact(4))[0]
        return protocol.decode(exact(length))

    def snapshot(self,client):
        client.sendall(protocol.encode(dict(command='get')))
        return self.receive(client)['value']

    def test_offline_preflight_checks_clock_crc_and_framing(self):
        result=subprocess.run([str(self.binary),'--self-test'],capture_output=True,check=True)
        self.assertIn(b'PASS: native Bridge clock, framing, and CRC',result.stdout)

    def test_atomic_publication_retains_pushes_and_deduplicates_exact_uart_index(self):
        client=self.connect()
        index=self.index
        self.send(b'Bresac_sample\xfev1,epoch,1,22000,50000,1',b'\1')
        self.assertEqual(self.receive(client),dict(request='raw',data='v1,resac_sample,v1,epoch,1,22000,50000,1'))
        self.send(b'Bresac_sample\xfedifferent',b'\1',index=index)
        self.assertFalse(select.select([client],[],[],.03)[0])
        self.assertEqual(self.snapshot(client)['resac_sample'],'v1,epoch,1,22000,50000,1')

    def test_legacy_get_put_and_mailbox_frames_share_retained_state(self):
        client=self.connect()
        self.send(b'Dresac_sensor_status\xfeok',b'\1')
        self.send(b'dresac_sensor_status',b'ok')
        self.assertFalse(select.select([client],[],[],0)[0])
        self.send(b'Mv1,resac_sensor_status,ok',b'')
        self.assertEqual(self.receive(client)['data'],'v1,resac_sensor_status,ok')
        self.assertEqual(self.snapshot(client)['resac_sensor_status'],'ok')

    def test_bad_crc_and_wrong_index_do_not_modify_or_publish(self):
        client=self.connect();good=packet(self.index,b'Bresac_sample\xfebad')
        self.process.stdin.write(good[:-1]+bytes([good[-1]^1]));self.process.stdin.flush()
        self.assertFalse(select.select([self.process.stdout,client],[],[],.03)[0])
        self.process.stdin.write(packet((self.index+9)&255,b'Bresac_sample\xfebad'));self.process.stdin.flush()
        self.assertFalse(select.select([self.process.stdout,client],[],[],.03)[0])
        self.send(b'dresac_sample',b'')
        self.assertNotIn('resac_sample',self.snapshot(client))

    def test_fragmented_tcp_does_not_block_uart_and_confirm_follows_storage(self):
        client=self.connect();encoded=protocol.encode(dict(command='put',key='resac_command',value='v1,id,ping'))
        client.sendall(encoded[:7]);self.send(b'dresac_command',b'')
        client.sendall(encoded[7:]);self.assertEqual(self.receive(client),dict(response='put',key='resac_command',value='v1,id,ping'))
        self.send(b'dresac_command',b'v1,id,ping')

    def test_partial_uart_frame_expires_without_periodic_idle_reads(self):
        self.process.stdin.write(packet(self.index,b'dmissing')[:5]);self.process.stdin.flush();time.sleep(.07)
        self.send(b'dresac_native_bridge',b'v1')

    def test_protocol_limits_close_only_bad_client(self):
        bad=self.connect();good=self.connect()
        bad.sendall(struct.pack('!I',0xffffffff));self.assertEqual(bad.recv(1),b'')
        self.assertEqual(self.snapshot(good)['resac_native_bridge'],'v1')
        good.sendall(frame(b'p'+protocol.entry('resac_native_bridge','wrong')))
        self.assertEqual(good.recv(1),b'')
        self.send(b'dresac_native_bridge',b'v1')

    def test_decoded_socket_backlog_drains_without_another_read_edge(self):
        client=self.connect()
        client.sendall(b''.join(protocol.encode(dict(command='put',key='resac_counter',value=str(i))) for i in range(50)))
        self.assertEqual([self.receive(client)['value'] for _ in range(50)],[str(i) for i in range(50)])
        self.send(b'dresac_counter',b'49')

    def test_shutdown_restores_shared_tty_flags_and_termios(self):
        master,slave=os.openpty()
        original_flags=fcntl.fcntl(slave,fcntl.F_GETFL)
        original_termios=termios.tcgetattr(slave)
        child=subprocess.Popen([str(self.binary),'--port','0'],stdin=slave,stdout=slave,stderr=subprocess.PIPE)
        try:
            self.assertEqual(child.stderr.readline().split()[0],b'READY')
            child.terminate();self.assertEqual(child.wait(timeout=2),0)
            self.assertEqual(fcntl.fcntl(slave,fcntl.F_GETFL),original_flags)
            self.assertEqual(termios.tcgetattr(slave),original_termios)
        finally:
            if child.poll() is None:child.kill();child.wait(timeout=2)
            child.stderr.close();os.close(master);os.close(slave)

    def test_packet_index_wrap_and_clean_shutdown_ack(self):
        self.send(b'XX100',b'\0',index=255);self.index=0
        self.send(b'dresac_native_bridge',b'v1')
        self.send(b'XXXXX',b'');self.assertEqual(self.process.wait(timeout=2),0)

    def test_native_agent_uses_snapshot_push_and_exact_gpio_confirmation(self):
        from test_agent import agent
        from types import SimpleNamespace
        self.send(b'Bresac_events\xfev1',b'\1')
        self.send(b'Bresac_command_notify\xfegpio21-pb2-v1',b'\1')
        levels=[]
        gpio=SimpleNamespace(open=lambda:None,set=levels.append,close=lambda:None)
        bridge=agent.BridgeEvents(port=self.port,native=True,command_gpio=gpio);self.addCleanup(bridge.close)
        def pump(predicate):
            until=time.monotonic()+2
            while not predicate():
                self.assertLess(time.monotonic(),until);bridge.poll(.01)
        pump(lambda:bridge.ready)
        frame_value='v1,12345678-1234-4234-8234-123456789abc,ping'
        bridge.dispatch_command(frame_value,agent.monotonic()+2)
        self.assertNotIn(True,levels)
        pump(lambda:bridge.notification['signaled']);self.assertTrue(levels[-1])
        self.send(b'dresac_command',frame_value.encode())
        self.send(b'Bresac_command_result\xfev6,1,0,0,1,0,0,r,v1,12345678-1234-4234-8234-123456789abc,pong,1',b'\1')
        pump(lambda:bridge.get('resac_command_result') is not None)
        bridge.cancel_command();self.assertFalse(levels[-1])

    def test_agent_rejects_bad_native_lengths_and_snapshot_capability(self):
        from test_agent import agent
        bridge=agent.BridgeEvents(native=True)
        with self.assertRaises(ValueError):bridge._receive(struct.pack('!I',0xffffffff))
        bridge.incoming=bytearray();bridge.snapshot_pending=True
        with self.assertRaises(ValueError):bridge._receive(frame(b'S'+protocol.entry('resac_events','v1')))
        for payload in [b'V',b'V\x01\x00\x05aX',b'?'+protocol.entry('key','value')]:
            with self.assertRaises(ValueError):protocol.decode(payload)
