import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_agent import agent
from test_bridge_events import ready_bridge, SESSION

PREVIOUS = '22222222-2222-4222-8222-222222222222'
NEXT = '11111111-1111-4111-8111-111111111111'
ROOT = Path(__file__).resolve().parents[2]
VECTOR = json.loads((ROOT / 'test/fixtures/command-wire.json').read_text())[-1]


class Request:
    def __init__(self, config=None, payload=None, route=None, **kwargs):
        self.payload, self.route, self.completed = payload, route, False
        self.closed = False
        self.response = (200, b'{}')
    def finish(self): return self.response
    def close(self): self.closed = True


class Bridge:
    def get(self, key): return 'v4'
    def put(self, *args): pass


class HandoffTests(unittest.TestCase):
    def saved(self, directory, bridge=None, native=True):
        processor = agent.CommandProcessor(dict(native_bridge=native), bridge or Bridge(),
            state_path=str(Path(directory) / 'journal'))
        processor.reply = dict(id=PREVIOUS, status='succeeded', result={'reply':'pong'}, traceparent=agent.traceparent())
        processor._save()
        return processor

    def response(self, processor, body, status=200):
        request = Request()
        request.completed, request.response = True, (status, body)
        processor.request, processor.request_kind = request, 'result'
        return request

    def test_binary_handoff_dispatches_next_without_another_http_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            processor = self.saved(directory)
            request = self.response(processor, bytes.fromhex(VECTOR['hex']))
            with patch.object(processor, '_arduino') as dispatch, patch.object(agent, 'CurlUpload', side_effect=AssertionError('Extra HTTP request')):
                processor.tick()
            self.assertTrue(request.closed)
            self.assertEqual(processor.current['id'], NEXT)
            self.assertIsNone(processor.reply)
            dispatch.assert_called_once()
            self.assertIn('command.timing.decode_ms', processor.phase_timings)
            # A crash before dispatch can recover the previous result's same handoff.
            restarted = agent.CommandProcessor(processor.config, Bridge(), state_path=processor.state_path)
            self.assertEqual(restarted.cached['id'], PREVIOUS)

    def test_malformed_handoff_retains_previous_journal_and_never_dispatches(self):
        base = VECTOR['value']
        bodies = [b'RAC1o', b'broken', json.dumps(dict(base, resultId=NEXT)).encode(),
                  json.dumps(dict(base, commands=[base['commands'][0]] * 2)).encode(),
                  json.dumps(dict(base, commands=[{'id':PREVIOUS}])).encode(),
                  json.dumps(dict(base, commands=[{'id':1}])).encode()]
        for body in bodies:
            with tempfile.TemporaryDirectory() as directory:
                processor = self.saved(directory)
                previous = Path(processor.state_path).read_bytes()
                self.response(processor, body)
                with patch.object(processor, '_arduino') as dispatch:
                    processor.tick()
                dispatch.assert_not_called()
                self.assertEqual(processor.reply['id'], PREVIOUS)
                self.assertIsNone(processor.current)
                self.assertEqual(Path(processor.state_path).read_bytes(), previous)

    def test_empty_handoff_and_older_server_ack_fall_back_to_the_existing_poll(self):
        empty = dict(VECTOR['value'], commands=[])
        for body in [json.dumps(empty).encode(), b'{"ok":true,"duplicate":false}']:
            with tempfile.TemporaryDirectory() as directory, patch.object(agent, 'CurlUpload', Request):
                processor = self.saved(directory)
                self.response(processor, body)
                processor.tick()
                self.assertIsNone(processor.reply)
                self.assertIsNone(processor.current)
                self.assertEqual(processor.request.route, 'commands?format=binary-v1')

    def test_native_mode_alone_opts_into_result_handoff(self):
        for native in (False, True):
            with tempfile.TemporaryDirectory() as directory, patch.object(agent, 'CurlUpload', Request):
                processor = self.saved(directory, native=native)
                processor.tick()
                self.assertEqual(processor.request.route, 'commands/%s/result' % PREVIOUS +
                                 ('?format=binary-v1&next=1' if native else ''))

    def test_busy_automation_defers_execution_of_a_received_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            processor = self.saved(directory)
            self.response(processor, bytes.fromhex(VECTOR['hex']))
            with patch.object(processor, '_arduino') as dispatch:
                processor.tick(allow_execution=False)
                dispatch.assert_not_called()
                self.assertEqual(processor.current['id'], NEXT)
                processor.tick()
                dispatch.assert_called_once()

    def test_ir_handoff_journals_before_dispatch_and_restart_reports_uncertainty(self):
        bridge = ready_bridge(resac_command_protocol='batched-v2',resac_command_notify=agent.COMMAND_GPIO_PROTOCOL)
        bridge.command_gpio = SimpleNamespace(open=lambda:None,set=lambda value:None,close=lambda:None)
        bridge.control_session, bridge.control_confirmed = SESSION, True
        next_command = dict(id=NEXT,type='fan.level',payload=dict(level=2,sessionId=SESSION),
                            expiresAt='2099-01-01T00:00:00Z',traceparent=agent.traceparent())
        body = json.dumps(dict(ok=True,resultId=PREVIOUS,commands=[next_command])).encode()
        with tempfile.TemporaryDirectory() as directory:
            processor = self.saved(directory,bridge)
            self.response(processor,body)
            original = bridge.dispatch_command
            def dispatch(frame, deadline, **kwargs):
                saved = json.loads(Path(processor.state_path).read_text())
                self.assertEqual(saved['id'], NEXT)
                self.assertEqual(saved['status'], 'failed')
                self.assertIn('uncertain', saved['result']['error'])
                return original(frame,deadline,**kwargs)
            with patch.object(bridge,'dispatch_command',side_effect=dispatch) as sent:
                processor.tick()
                self.assertEqual(sent.call_count,1)
                self.assertEqual(bridge.notification['frame'].split(',')[1],NEXT)
                restarted=agent.CommandProcessor(processor.config,bridge,state_path=processor.state_path)
                with patch.object(agent,'CurlUpload',Request), patch.object(restarted,'_arduino',side_effect=AssertionError('IR replay')):
                    restarted.tick()
                self.assertEqual(restarted.request.payload['status'],'failed')
                self.assertIn('uncertain',restarted.request.payload['result']['error'])
                self.assertEqual(sent.call_count,1)
