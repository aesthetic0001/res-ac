import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('wifi_roaming', Path(__file__).resolve().parents[1] / 'wifi_roaming.py')
wifi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wifi)

OLD = '02:00:00:00:00:01'
NEW = '02:00:00:00:00:02'
OTHER = '02:00:00:00:00:03'


def station(failed, bssid=OLD):
    return 'Station %s (on wlan0)\n\ttx packets: 100\n\ttx retries: 300\n\ttx failed: %d\n' % (bssid, failed)


def scan(bssid=NEW, signal=-65, ssid='eduroam', flags='[WPA2-EAP-CCMP][ESS]'):
    return '%s\t2412\t%d\t%s\t%s' % (bssid, signal, flags, ssid)


class LinkHealthTests(unittest.TestCase):
    def check(self, received, before=10, after=13, bssid=OLD):
        command = Mock(side_effect=[station(before),
            '4 packets transmitted, %d packets received, 50%% packet loss' % received,
            station(after, bssid)])
        result = wifi.link_health('wlan0', ['192.0.2.1'], command)
        return result, command

    def test_local_loss_and_independent_transmit_failures_are_required(self):
        self.assertEqual(self.check(2)[0]['state'], 'poor')
        self.assertEqual(self.check(0, after=10)[0]['state'], 'unknown')
        self.assertEqual(self.check(2, after=11)[0]['state'], 'unknown')

    def test_gateway_success_is_healthy_despite_unrelated_application_outage(self):
        for received in [3, 4]:
            self.assertEqual(self.check(received)[0]['state'], 'up')

    def test_counter_reset_or_automatic_roam_invalidates_probe(self):
        self.assertEqual(self.check(0, after=1)[0]['state'], 'unknown')
        self.assertEqual(self.check(0, bssid=NEW)[0]['state'], 'unknown')

    def test_missing_driver_counters_do_not_probe_or_recover(self):
        command = Mock(return_value='')
        self.assertEqual(wifi.link_health('wlan0', ['192.0.2.1'], command), {'state': 'unknown'})
        self.assertEqual(command.call_count, 1)

    def test_malformed_ping_and_multiple_stations_are_unknown(self):
        command = Mock(side_effect=[station(0), 'ping: operation not permitted', station(4)])
        self.assertEqual(wifi.link_health('wlan0', ['192.0.2.1'], command)['state'], 'unknown')
        self.assertIsNone(wifi.station(station(0) + station(2, NEW)))

    def test_probe_is_bounded_and_uses_current_lease_gateway(self):
        result, command = self.check(0)
        command.assert_any_call(['ping', '-c', '4', '-W', '1', '192.0.2.1'], timeout=8, allow_failure=True)
        self.assertEqual(result['failed'], 3)

    def test_invalid_input_cannot_become_a_probe(self):
        command = Mock()
        for device, gateways in [('../wlan0', ['192.0.2.1']), ('wlan0', []), ('wlan0', ['bad gateway'])]:
            self.assertEqual(wifi.link_health(device, gateways, command)['state'], 'unknown')
        command.assert_not_called()


class CandidateTests(unittest.TestCase):
    def test_only_eligible_same_profile_enterprise_aps_are_ranked(self):
        rows = [scan(OLD, -30), scan(NEW, -65), scan(OTHER, -60),
                scan('02:00:00:00:00:04', -50, ssid='different'),
                scan('02:00:00:00:00:05', -45, flags='[WPA2-PSK-CCMP][ESS]'),
                scan('02:00:00:00:00:06', -40, flags='[ESS]'),
                scan('02:00:00:00:00:07', -90), 'malformed']
        self.assertEqual(wifi.candidates('\n'.join(rows), 'eduroam', OLD, {}, 10), [OTHER, NEW])

    def test_quarantine_expires_and_duplicate_scan_entries_are_bounded(self):
        text = scan() + '\n' + scan()
        avoided = {NEW: (100, 1)}
        self.assertEqual(wifi.candidates(text, 'eduroam', OLD, avoided, 99), [])
        self.assertEqual(wifi.candidates(text, 'eduroam', OLD, avoided, 100), [NEW])


class RoamingTests(unittest.TestCase):
    def setUp(self):
        self.control = Mock()
        self.control.status.return_value = dict(wpa_state='COMPLETED', bssid=OLD,
                                               key_mgmt='WPA2/IEEE 802.1X/EAP', ssid='eduroam')
        self.replies = {'SCAN': 'OK', 'SCAN_RESULTS': scan(), 'ROAM ' + NEW: 'OK'}
        self.control.request.side_effect = lambda request: self.replies[request]
        self.factory, self.allowed = Mock(return_value=self.control), Mock(return_value=True)
        self.restart, self.report = Mock(), Mock()
        self.roaming = wifi.Roaming(Mock(), self.report, self.restart, self.allowed, self.factory)

    def observe(self, state, now, bssid=OLD):
        self.roaming.observe(dict(state=state, bssid=bssid), 'wlan0', 'radio0', now)

    def start_scan(self):
        self.observe('poor', 0)
        self.observe('poor', 120)
        self.control.request.assert_called_with('SCAN')

    def roam(self):
        self.start_scan()
        self.observe('poor', 150)

    def test_requires_sustained_poor_link_and_waits_for_scan(self):
        self.observe('poor', 0)
        self.observe('poor', 119)
        self.factory.assert_not_called()
        self.observe('poor', 120)
        self.observe('poor', 130)
        self.assertEqual(self.control.request.call_args_list[-1].args[0], 'SCAN')
        self.observe('poor', 150)
        self.control.request.assert_called_with('ROAM ' + NEW)
        self.restart.assert_not_called()
        self.assertGreater(self.roaming.avoided[OLD][0], 150)

    def test_brief_failure_and_unknown_measurements_cannot_trigger_roam(self):
        for state in ['up', 'unknown']:
            self.roaming.reset_observation()
            self.observe('poor', 0)
            self.observe(state, 110)
            self.observe('poor', 120)
            self.observe('poor', 230)
        self.factory.assert_not_called()

    def test_link_recovery_cancels_outstanding_scan(self):
        self.start_scan()
        self.observe('up', 150)
        self.assertIsNone(self.roaming.pending)
        self.assertNotIn('ROAM ' + NEW, [c.args[0] for c in self.control.request.call_args_list])

    def test_manual_ifdown_or_explicit_pin_wins_before_and_after_scan(self):
        self.allowed.return_value = False
        self.observe('poor', 0)
        self.observe('poor', 120)
        self.factory.assert_not_called()
        self.allowed.return_value = True
        self.roaming.reset_observation()
        self.start_scan()
        self.allowed.side_effect = [True, False]
        self.observe('poor', 150)
        self.restart.assert_not_called()
        self.assertNotIn('ROAM ' + NEW, [c.args[0] for c in self.control.request.call_args_list])

    def test_profile_or_bssid_change_during_scan_cancels_action(self):
        self.start_scan()
        self.control.status.return_value['ssid'] = 'other'
        self.observe('poor', 150)
        self.assertIsNone(self.roaming.pending)
        self.restart.assert_not_called()

    def test_no_alternative_uses_radio_fallback_and_bounded_backoff(self):
        self.replies['SCAN_RESULTS'] = scan(OLD)
        self.roam()
        self.restart.assert_called_once_with('radio0')
        self.assertEqual(self.roaming.next_attempt, 450)
        self.observe('poor', 180)
        self.observe('poor', 400)
        self.assertEqual(self.restart.call_count, 1)
        for now in [500, 1000, 2000, 3000, 5000]:
            self.roaming.reserve(now)
        self.assertEqual(self.roaming.cooldown, 1800)

    def test_refused_roam_quarantines_target_and_does_not_reset_radio(self):
        self.replies['ROAM ' + NEW] = 'FAIL'
        self.roam()
        self.assertIn(NEW, self.roaming.avoided)
        self.restart.assert_not_called()

    def test_second_failed_ap_tries_a_third_instead_of_returning_to_quarantined_ap(self):
        self.roam()
        self.control.status.return_value['bssid'] = NEW
        self.replies['SCAN_RESULTS'] = scan(OLD, -30) + '\n' + scan(OTHER, -65)
        self.replies['ROAM ' + OTHER] = 'OK'
        self.observe('poor', 210, NEW)
        self.observe('poor', 450, NEW)
        self.observe('poor', 480, NEW)
        self.control.request.assert_called_with('ROAM ' + OTHER)
        self.assertIn(OLD, self.roaming.avoided)
        self.assertIn(NEW, self.roaming.avoided)

    def test_incomplete_authentication_or_non_enterprise_profile_cannot_be_roamed(self):
        for key, value in [('wpa_state', 'ASSOCIATING'), ('key_mgmt', 'WPA2-PSK')]:
            original = self.control.status.return_value[key]
            self.control.status.return_value[key] = value
            self.observe('poor', 0)
            self.observe('poor', 120)
            self.control.request.assert_not_called()
            self.control.status.return_value[key] = original
            self.roaming.reset_observation()

    def test_settling_avoids_probe_and_stable_health_resets_backoff(self):
        self.roam()
        with patch.object(wifi, 'link_health') as health:
            self.roaming.tick('wlan0', ['192.0.2.1'], 'radio0', 180)
            health.assert_not_called()
        self.observe('up', 210, NEW)
        self.assertFalse(self.roaming.verifying)
        self.assertEqual(self.roaming.cooldown, 600)
        self.observe('up', 510, NEW)
        self.assertEqual(self.roaming.cooldown, 300)

    def test_quarantine_and_memory_are_bounded(self):
        for now in [0, 1, 2, 3]:
            self.roaming.avoid(OLD, now)
        self.assertEqual(self.roaming.avoided[OLD][0], 3603)
        for i in range(40):
            self.roaming.avoid('02:00:00:00:01:%02x' % i, 10)
        self.assertLessEqual(len(self.roaming.avoided), 32)

    def test_unavailable_control_is_backed_off_without_crashing(self):
        self.factory.side_effect = IOError('no control socket')
        self.observe('poor', 0)
        self.observe('poor', 120)
        self.assertEqual(self.roaming.next_attempt, 420)
        self.observe('poor', 150)
        self.assertEqual(self.factory.call_count, 1)

    def test_no_persistent_or_security_configuration_commands_are_sent(self):
        self.roam()
        self.assertEqual([c.args[0] for c in self.control.request.call_args_list],
                         ['SCAN', 'SCAN_RESULTS', 'ROAM ' + NEW])
        self.control.close.assert_called()


if __name__ == '__main__':
    unittest.main()
