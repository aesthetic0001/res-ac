import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
with patch.object(sys, 'path', [str(root)] + sys.path):
    spec = importlib.util.spec_from_file_location('watchdog', root / 'network_watchdog.py')
    watchdog = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(watchdog)


class RecoveryTests(unittest.TestCase):
    def test_waits_for_continuous_outage_and_ignores_backend_failures(self):
        recovery = watchdog.Recovery()
        resets = []
        for now in [0, 30, 60, 90]:
            recovery.tick('down', 'radio0', now, resets.append)
        self.assertEqual(resets, [])
        recovery.tick('up', 'radio0', 100, resets.append)
        recovery.tick('down', 'radio0', 110, resets.append)
        recovery.tick('down', 'radio0', 229, resets.append)
        self.assertEqual(resets, [])
        recovery.tick('down', 'radio0', 230, resets.append)
        self.assertEqual(resets, ['radio0'])
        for now in range(240, 3000, 30):
            recovery.tick('up', 'radio0', now, resets.append)
        self.assertEqual(resets, ['radio0'])

    def test_backoff_is_bounded_and_brief_reconnections_do_not_reset_it(self):
        recovery = watchdog.Recovery()
        resets = []
        for now in range(0, 6001, 30):
            recovery.tick('down', 'radio0', now, lambda radio: resets.append(now))
        self.assertEqual(resets, [120, 420, 1020, 2220, 4020, 5820])
        recovery.tick('up', 'radio0', 6001, lambda radio: None)
        recovery.tick('down', 'radio0', 6031, lambda radio: None)
        self.assertEqual(recovery.cooldown, 1800)
        recovery.tick('up', 'radio0', 6100, lambda radio: None)
        recovery.tick('up', 'radio0', 6400, lambda radio: None)
        self.assertEqual(recovery.cooldown, 300)

    def test_unknown_disabled_and_radio_changes_restart_grace_period(self):
        for state in ['unknown', 'disabled']:
            recovery = watchdog.Recovery()
            resets = []
            recovery.tick('down', 'radio0', 0, resets.append)
            recovery.tick(state, None, 120, resets.append)
            recovery.tick('down', 'radio0', 150, resets.append)
            recovery.tick('down', 'radio1', 270, resets.append)
            self.assertEqual(resets, [])
            recovery.tick('down', 'radio1', 390, resets.append)
            self.assertEqual(resets, ['radio1'])

    def test_failed_restart_still_has_cooldown(self):
        recovery = watchdog.Recovery()
        recovery.tick('down', 'radio0', 0, lambda radio: None)
        def fail(radio):
            raise IOError('failure')
        with self.assertRaises(IOError):
            recovery.tick('down', 'radio0', 120, fail)
        resets = []
        recovery.tick('down', 'radio0', 300, resets.append)
        self.assertEqual(resets, [])
        recovery.tick('down', 'radio0', 420, resets.append)
        self.assertEqual(resets, ['radio0'])


class TransportRecoveryTests(unittest.TestCase):
    def test_five_minutes_of_failures_renews_dhcp_then_escalates_to_radio(self):
        recovery = watchdog.TransportRecovery()
        renew, radio = [], []
        health = dict(failed_since=0)
        for now in [0, 30, 299]:
            self.assertFalse(recovery.tick('up', 'radio0', now, health, lambda: renew.append(now), radio.append))
        self.assertTrue(recovery.tick('up', 'radio0', 300, health, lambda: renew.append(300), radio.append))
        self.assertEqual(renew, [300])
        self.assertFalse(recovery.tick('up', 'radio0', 599, health, lambda: None, radio.append))
        self.assertTrue(recovery.tick('up', 'radio0', 600, health, lambda: None, radio.append))
        self.assertEqual(radio, ['radio0'])
        self.assertEqual(recovery.next_attempt, 1200)

    def test_manual_disable_unknown_or_stale_agent_state_cannot_reset_network(self):
        recovery = watchdog.TransportRecovery()
        for state, health in [('disabled', dict(failed_since=0)), ('unknown', dict(failed_since=0)),
                             ('down', dict(failed_since=0)), ('up', None), ('up', dict(failed_since=None))]:
            self.assertFalse(recovery.tick(state, 'radio0', 1000, health,
                                          lambda: self.fail('unexpected DHCP restart'),
                                          lambda radio: self.fail('unexpected radio restart')))

    def test_recovery_backoff_is_reserved_even_if_reconfiguration_fails(self):
        recovery = watchdog.TransportRecovery()
        def fail():
            raise IOError('failure')
        with self.assertRaises(IOError):
            recovery.tick('up', 'radio0', 300, dict(failed_since=0), fail, lambda radio: None)
        self.assertEqual(recovery.next_attempt, 600)
        self.assertFalse(recovery.tick('up', 'radio0', 301, dict(failed_since=0), fail, lambda radio: None))

    def test_five_minutes_of_success_resets_escalation_but_a_brief_recovery_does_not(self):
        recovery = watchdog.TransportRecovery()
        recovery.tick('up', 'radio0', 300, dict(failed_since=0), lambda: None, lambda radio: None)
        recovery.tick('up', 'radio0', 301, dict(failed_since=None), lambda: None, lambda radio: None)
        recovery.tick('up', 'radio0', 400, dict(failed_since=399), lambda: None, lambda radio: None)
        self.assertEqual(recovery.stage, 1)
        recovery.tick('up', 'radio0', 500, dict(failed_since=None), lambda: None, lambda radio: None)
        recovery.tick('up', 'radio0', 800, dict(failed_since=None), lambda: None, lambda radio: None)
        self.assertEqual((recovery.stage, recovery.cooldown), (0, 300))

    def test_uplink_always_comes_back_up_and_rechecks_manual_policy(self):
        with patch.object(watchdog, 'snapshot', return_value=('disabled', None)), patch.object(watchdog, 'command') as command:
            watchdog.renew_uplink()
            watchdog.transport_restart_radio('radio0')
            command.assert_not_called()
        with patch.object(watchdog, 'snapshot', return_value=('up', 'radio0')), patch.object(watchdog, 'report'), \
                patch.object(watchdog, 'command', side_effect=[IOError('failed'), '']) as command:
            with self.assertRaises(IOError):
                watchdog.renew_uplink()
            self.assertEqual(command.call_args_list[-1].args[0], ['ifup', 'wwan'])


class SnapshotTests(unittest.TestCase):
    def test_compacts_empty_containers_for_legacy_json_without_changing_strings(self):
        text = '{"empty": [\n\t ], "object": {\n\t }, "label": "with spaces and \\\"quotes\\\""}'
        def legacy(value):
            self.assertNotIn('[\n', value)
            self.assertNotIn('{\n', value)
            return json.loads(value)
        with patch.object(watchdog, 'decode_json', side_effect=legacy):
            self.assertEqual(watchdog.network_status(text), json.loads(text))

    def setUp(self):
        self.settings = {'wireless.eduroam.network': 'wwan', 'wireless.eduroam.mode': 'sta',
                         'wireless.eduroam.device': 'radio0', 'network.wwan.proto': 'dhcp',
                         'network.wwan.metric': '20'}
        self.status = dict(up=True, autostart=True, l3_device='wlan0',
                           **{'ipv4-address': [{'address': '192.0.2.10', 'mask': 24}],
                              'route': [dict(target='0.0.0.0', mask=0, nexthop='192.0.2.1')]})
        self.patches = [patch.object(watchdog, 'setting', side_effect=lambda key, default=None: self.settings.get(key, default)),
                        patch.object(watchdog, 'command', return_value='unused'),
                        patch.object(watchdog, 'decode_json', side_effect=lambda text: self.status)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_healthy_local_network_does_not_probe_backend(self):
        with patch.object(watchdog, 'default_exists', return_value=True), patch.object(watchdog, 'repair') as repair:
            self.assertEqual(watchdog.snapshot(), ('up', 'radio0'))
            repair.assert_not_called()
        watchdog.command.assert_called_once_with(['ifstatus', 'wwan'], allow_failure=True)

    def test_roaming_uses_current_interface_and_gateway(self):
        details = {}
        with patch.object(watchdog, 'default_exists', return_value=True):
            self.assertEqual(watchdog.snapshot(details), ('up', 'radio0'))
        self.assertEqual(details, dict(device='wlan0', gateways=['192.0.2.1']))

    def test_roaming_respects_ap_pin_and_current_uplink_policy(self):
        self.settings['wireless.eduroam.bssid'] = '02:00:00:00:00:01'
        with patch.object(watchdog, 'snapshot') as snapshot:
            self.assertFalse(watchdog.roaming_allowed('radio0', 'wlan0'))
            snapshot.assert_not_called()
        del self.settings['wireless.eduroam.bssid']
        with patch.object(watchdog, 'default_exists', return_value=True):
            self.assertTrue(watchdog.roaming_allowed('radio0', 'wlan0'))
            self.assertFalse(watchdog.roaming_allowed('radio1', 'wlan0'))
            self.assertFalse(watchdog.roaming_allowed('radio0', 'wlan1'))
            self.status['autostart'] = False
            self.assertFalse(watchdog.roaming_allowed('radio0', 'wlan0'))

    def test_missing_route_is_repaired_using_current_lease_before_radio_reset(self):
        with patch.object(watchdog, 'default_exists', side_effect=[False, True]), patch.object(watchdog, 'repair') as repair:
            self.assertEqual(watchdog.snapshot(), ('up', 'radio0'))
            repair.assert_called_once_with('renew', dict(INTERFACE='wwan', interface='wlan0', router='192.0.2.1'))

    def test_no_dhcp_or_gateway_is_down(self):
        self.status['up'] = False
        self.assertEqual(watchdog.snapshot(), ('down', 'radio0'))
        self.status['up'] = True
        self.status['route'] = []
        self.assertEqual(watchdog.snapshot(), ('down', 'radio0'))

    def test_disabled_networks_and_manual_ifdown_are_respected(self):
        for key, value in [('wireless.eduroam.disabled', '1'), ('wireless.radio0.disabled', '1'),
                           ('network.wwan.auto', '0'), ('network.wwan.defaultroute', '0')]:
            self.settings[key] = value
            self.assertEqual(watchdog.snapshot(), ('disabled', None))
            del self.settings[key]
        self.status['autostart'] = False
        self.assertEqual(watchdog.snapshot(), ('disabled', None))

    def test_invalid_status_or_radio_never_restarts_network(self):
        self.status = {}
        self.assertEqual(watchdog.snapshot(), ('unknown', None))
        self.settings['wireless.eduroam.device'] = '../radio0'
        self.assertEqual(watchdog.snapshot(), ('unknown', None))

    def test_restart_always_attempts_up_even_when_down_fails(self):
        with patch.object(watchdog, 'report'), patch.object(watchdog, 'command', side_effect=[IOError('timeout'), '']) as command:
            with self.assertRaises(IOError):
                watchdog.restart_radio('radio0')
            self.assertEqual(command.call_args_list[-1].args[0], ['wifi', 'up', 'radio0'])


if __name__ == '__main__':
    unittest.main()
