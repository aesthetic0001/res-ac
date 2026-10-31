import importlib.util
import socket
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('repair', Path(__file__).resolve().parents[1] / 'dhcp_route_repair.py')
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


class RouteRepairTests(unittest.TestCase):
    def test_default_matches_device_gateway_and_metric_in_native_byte_order(self):
        gateway = '192.0.2.1'
        encoded = struct.unpack('=I', socket.inet_aton(gateway))[0]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'route'
            path.write_text('Iface Destination Gateway Flags RefCnt Use Metric Mask\n'
                            'test0 00000000 %08X 0003 0 0 20 00000000\n' % encoded)
            self.assertTrue(repair.default_exists('test0', gateway, 20, str(path)))
            self.assertFalse(repair.default_exists('test1', gateway, 20, str(path)))
            self.assertFalse(repair.default_exists('test0', '192.0.2.2', 20, str(path)))
            self.assertFalse(repair.default_exists('test0', gateway, 0, str(path)))

    def test_netlink_add_preserves_netifd_protocol_and_never_replaces_routes(self):
        message = repair.route_message(7, '192.0.2.1', 20)
        length, kind, flags, seq, pid = struct.unpack('=IHHII', message[:16])
        self.assertEqual((length, kind, flags, seq, pid), (len(message), 24, 0x605, 1, 0))
        self.assertEqual(struct.unpack('=BBBBBBBBI', message[16:28]), (socket.AF_INET, 0, 0, 0, 254, 4, 0, 1, 0))
        attributes = {}
        for offset in range(28, len(message), 8):
            size, kind = struct.unpack('=HH', message[offset:offset + 4])
            self.assertEqual(size, 8)
            attributes[kind] = message[offset + 4:offset + 8]
        self.assertEqual(struct.unpack('=I', attributes[4])[0], 7)
        self.assertEqual(socket.inet_ntoa(attributes[5]), '192.0.2.1')
        self.assertEqual(struct.unpack('=I', attributes[6])[0], 20)

    def test_only_missing_current_lease_route_is_added(self):
        env = dict(INTERFACE='test', interface='test0', router='192.0.2.1')
        def setting(section, option, default):
            return '20' if option == 'metric' else default
        with patch.object(repair, 'setting', setting), patch.object(repair, 'default_exists', side_effect=[False, True]), \
                patch.object(repair, 'add_default') as add:
            self.assertEqual(repair.repair('renew', env), 1)
            add.assert_called_once_with('test0', '192.0.2.1', 20)
        with patch.object(repair, 'setting', setting), patch.object(repair, 'default_exists', return_value=True), \
                patch.object(repair, 'add_default') as add:
            self.assertEqual(repair.repair('bound', env), 0)
            add.assert_not_called()

    def test_deconfig_disabled_defaults_and_missing_gateway_do_not_add_routes(self):
        env = dict(INTERFACE='test', interface='test0', router='192.0.2.1')
        with patch.object(repair, 'setting', return_value='0'), patch.object(repair, 'add_default') as add:
            for event in ['deconfig', 'leasefail', 'renew']:
                self.assertEqual(repair.repair(event, env), 0)
            add.assert_not_called()
        env['router'] = ''
        with patch.object(repair, 'setting', side_effect=['1', '20']), patch.object(repair, 'add_default') as add:
            self.assertEqual(repair.repair('renew', env), 0)
            add.assert_not_called()


if __name__ == '__main__':
    unittest.main()
