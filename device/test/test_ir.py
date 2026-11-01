import datetime
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from test_agent import agent
SESSION = 'a' * 32


class Bridge:
    def __init__(self):
        self.values = {'resac_command_ready': 'v3', 'resac_fan_state': 'v1,%s,1,1,1,1' % SESSION}
        self.sent = []

    def get(self, key):
        return self.values.get(key)

    def put(self, key, value):
        self.values[key] = value
        if key == 'resac_command' and value:
            self.sent.append(value)


def command(button='speed_up', **updates):
    value = dict(id=str(uuid.uuid4()), type='fan.command', payload=dict(button=button, sessionId=SESSION),
                 expiresAt='2099-01-01T00:00:00.000Z')
    value.update(updates)
    return value


class IRTests(unittest.TestCase):
    def test_button_profile_and_nec_numeric_contract(self):
        expected = dict(power=0, speed_up=8, speed_down=16, breeze=9, horizontal=21,
                        vertical=22, timer_1h=18, timer_2h=14, timer_4h=10)
        for name, code in expected.items():
            self.assertEqual(agent.ir_payload(command(name)), (0xde80, code, 0))
        raw = command(type='ir.send', payload=dict(protocol='NEC', address=56960, command=8))
        self.assertEqual(agent.ir_payload(raw), (0xde80, 8, 0))
        for key, values in dict(address=[-1, 65536, True, '0xDE80'],
                                command=[-1, 256, 0xf708, 8.0, None],
                                repeats=[-1, 4, True]).items():
            for value in values:
                invalid = dict(raw, payload=dict(raw['payload'], **{key: value}))
                with self.assertRaises(ValueError):
                    agent.ir_payload(invalid)
        for payload in [dict(button='mute'), dict(button='power', repeats=1), {}, []]:
            with self.assertRaises(ValueError):
                agent.ir_payload(command(payload=payload))

    def test_utc_expiry_without_full_python_stdlib(self):
        for stamp in ['1970-01-01T00:00:00Z', '2000-02-29T12:34:56.123Z',
                      '2026-10-31T14:00:00.000Z', '2100-03-01T00:00:00Z']:
            expected = datetime.datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
            self.assertEqual(agent.command_expiry(dict(expiresAt=stamp)), int(expected))
        for stamp in ['', '2026-02-29T00:00:00Z', '2026-01-01T24:00:00Z', '2026-01-01']:
            with self.assertRaises(ValueError):
                agent.command_expiry(dict(expiresAt=stamp))

    def test_ack_matches_id_address_command_and_repeats_then_saves_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'result.json')
            bridge = Bridge()
            processor = agent.CommandProcessor({}, bridge, path)
            request = command()
            processor._accept(request)
            processor._arduino()
            self.assertEqual(bridge.sent, ['v3,%s,nec,%s,56960,8,0' % (request['id'], SESSION)])
            self.assertEqual(json.loads(Path(path).read_text())['status'], 'failed')
            self.assertIsNone(processor.reply)
            for reply in ['v1,%s,pong,42' % request['id'],
                          'v3,%s,ir_sent,%s,56960,8,0,42' % (uuid.uuid4(), SESSION),
                          'v3,%s,ir_sent,%s,56960,16,0,42' % (request['id'], SESSION)]:
                bridge.values['resac_command_result'] = reply
                processor.bridge_due = 0
                processor._arduino()
                self.assertIsNone(processor.reply)
            self.assertEqual(len(bridge.sent), 1)
            bridge.values['resac_command_result'] = 'v3,%s,ir_sent,%s,56960,8,0,42' % (request['id'], SESSION)
            processor.bridge_due = 0
            processor._arduino()
            self.assertEqual(processor.reply['status'], 'succeeded')
            self.assertFalse(processor.reply['result']['fanStateConfirmed'])
            processor._save()
            restarted = agent.CommandProcessor({}, bridge, path)
            restarted._accept(request)
            self.assertEqual(restarted.reply, processor.reply)
            self.assertEqual(len(bridge.sent), 1)

    def test_interrupted_transmission_is_not_replayed_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'result.json')
            bridge = Bridge()
            request = command('power')
            processor = agent.CommandProcessor({}, bridge, path)
            processor._accept(request)
            processor._arduino()
            restarted = agent.CommandProcessor({}, bridge, path)
            restarted._accept(request)
            self.assertEqual(restarted.reply['status'], 'failed')
            self.assertIn('uncertain', restarted.reply['result']['error'])
            self.assertEqual(len(bridge.sent), 1)
            # An AVR reset clears the mailbox; the running agent also won't replay it.
            bridge.values['resac_command'] = ''
            processor.bridge_due = 0
            processor._arduino()
            self.assertEqual(len(bridge.sent), 1)

    def test_expired_old_firmware_and_unwritable_journal_do_not_send(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = Bridge()
            processor = agent.CommandProcessor({}, bridge, str(Path(directory) / 'result.json'))
            processor._accept(command(expiresAt='2020-01-01T00:00:00Z'))
            self.assertEqual(processor.reply['status'], 'failed')
            bridge.values['resac_command_ready'] = 'v1'
            processor._accept(command())
            self.assertIn('upgrade', processor.reply['result']['error'])
            bridge.values['resac_command_ready'] = 'v3'
            processor.reply = None
            processor._accept(command())
            with patch.object(processor, '_write_state', side_effect=OSError('disk full')):
                processor._arduino()
            self.assertFalse(bridge.sent)
            processor.bridge_due = 0
            with patch.object(agent.time, 'time', return_value=4102444801):
                processor._arduino()
            self.assertIn('expired', processor.reply['result']['error'])

    def test_timeout_does_not_claim_fan_state_or_retransmit(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge = Bridge()
            processor = agent.CommandProcessor({}, bridge, str(Path(directory) / 'result.json'))
            processor._accept(command())
            processor._arduino()
            with patch.object(agent, 'monotonic', return_value=processor.deadline + 1):
                processor._arduino()
            self.assertEqual(processor.reply['status'], 'failed')
            self.assertIn('uncertain', processor.reply['result']['error'])
            self.assertEqual(len(bridge.sent), 1)

    def test_unreadable_or_corrupt_journal_does_not_allow_new_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'result.json')
            Path(path).write_text('broken JSON')
            with self.assertRaises(ValueError):
                agent.CommandProcessor({}, Bridge(), path)
            with patch('builtins.open', side_effect=PermissionError(13, 'denied')):
                with self.assertRaises(ValueError):
                    agent.CommandProcessor({}, Bridge(), path)


if __name__ == '__main__':
    unittest.main()
