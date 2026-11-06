import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_agent import agent

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('cooperative_json',ROOT/'device/cooperative_json.py')
cooperative=importlib.util.module_from_spec(spec);spec.loader.exec_module(cooperative)


class CooperativeJsonTests(unittest.TestCase):
    def test_large_nested_json_yields_between_bounded_steps_and_preserves_values(self):
        payload={'events':[{'name':'command.execute','attributes':{'text':'quote " \\ \u00e9','number':1.25,'ok':True},'n':i} for i in range(32)],'nothing':None}
        encoding=cooperative.Encoding(payload,agent.encode_json)
        steps=0
        while True:
            before=encoding.size;result=encoding.step(128);steps+=1
            self.assertLessEqual(encoding.size-before,128)
            if result is not None:break
        self.assertGreater(steps,20)
        self.assertEqual(json.loads(result),payload)
        self.assertEqual(encoding.step(),result)

    def test_invalid_and_oversized_payloads_never_produce_a_body(self):
        for payload in [{'bad':float('nan')},{'bad':float('inf')},{1:'bad key'},{'bad':set()}]:
            encoding=cooperative.Encoding(payload,agent.encode_json)
            with self.assertRaises(ValueError):
                while encoding.step() is None:pass
        cycle=[];cycle.append(cycle)
        with self.assertRaises(ValueError):cooperative.Encoding(cycle,agent.encode_json).step()
        encoding=cooperative.Encoding({'large':'x'*2000},agent.encode_json,limit=1000)
        with self.assertRaises(ValueError):
            while encoding.step() is None:pass
        self.assertIsNone(encoding.result)

    def test_native_telemetry_is_cooperative_and_retries_same_batch_after_backoff(self):
        clock=[100.0];requests=[]
        class Request:
            def __init__(self,config,payload,**kwargs):
                self.payload=payload;self.encoded=kwargs['encoded_body'];self.status=500
                self.process=SimpleNamespace(poll=lambda:0);requests.append(self)
            def finish(self):return self.status,b'{}'
            def close(self):pass
        with patch.dict(sys.modules,{'cooperative_json':cooperative}), \
                patch.object(agent,'monotonic',lambda:clock[0]),patch.object(agent,'CurlUpload',Request):
            telemetry=agent.Telemetry({'native_bridge':True});pending=agent.Buffer(1)
            for i in range(32):telemetry.event('command.execute',attributes={'command.id':str(i),'text':'x'*300})
            telemetry.tick(pending)
            self.assertFalse(requests);self.assertIsNotNone(telemetry.encoding)
            steps=0
            while not requests:
                telemetry.tick(pending);steps+=1
                self.assertLess(steps,200)
                # Other event-loop work can run between serialization steps.
                clock[0]+=.001
            self.assertGreater(steps,20)
            first=requests[0]
            self.assertEqual(json.loads(first.encoded),first.payload)
            telemetry.tick(pending)
            self.assertIsNone(telemetry.encoding)
            retry=telemetry.next_due
            for i in range(64):telemetry.event('bridge.dispatch')
            telemetry.tick(pending);self.assertEqual(len(requests),1)
            self.assertEqual(telemetry.next_due,retry)
            clock[0]=retry
            while len(requests)<2:telemetry.tick(pending);clock[0]+=.001
            self.assertEqual(requests[1].payload['batchId'],first.payload['batchId'])
            self.assertGreater(requests[1].payload['events'][0]['ageMs'],first.payload['events'][0]['ageMs'])

    def test_fan_snapshot_change_detection_does_not_serialize_json_on_idle_ticks(self):
        from test_bridge_events import ready_bridge,Request
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            bridge=ready_bridge();bridge._queue=lambda message:None
            clock=[100.0]
            with patch.object(agent,'CurlUpload',Request),patch.object(agent,'monotonic',lambda:clock[0]):
                auto=agent.AutoController({},path=str(Path(directory)/'curve.json'))
                reporter=agent.FanReporter({'_bridge':bridge},automation=auto)
                with patch.object(agent,'encode_json',side_effect=AssertionError('Idle JSON serialization')):
                    reporter.tick()
                    for _ in range(30):reporter.tick()
                saved=auto.snapshot();auto.settings['thresholdsC'][0]=18
                self.assertNotEqual(saved['thresholdsC'],auto.snapshot()['thresholdsC'])
