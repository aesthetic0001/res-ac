import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from test_agent import agent
from test_ir import Bridge, SESSION, command


class AutomationTests(unittest.TestCase):
    def test_thresholds_hysteresis_and_validation(self):
        thresholds=[22,24,26,28,30]
        self.assertEqual([agent.curve_target(t,thresholds,1) for t in [21,22,24,26,28,30]],list(range(6)))
        self.assertEqual(agent.curve_target(25.1,thresholds,1,3),3)
        self.assertEqual(agent.curve_target(25,thresholds,1,3),2)
        self.assertEqual(agent.curve_target(24.9,thresholds,1,3),2)
        self.assertEqual(agent.curve_target(20.9,thresholds,1,5),0)
        for payload in [dict(enabled=True,thresholdsC=[22,21,26,28,30]),
                        dict(enabled=True,thresholdsC=[22,24,26,28]),
                        dict(enabled=True,thresholdsC=[22,24,26,28,float('nan')]),
                        dict(enabled=True,thresholdsC=thresholds,hysteresisC=-.1),
                        dict(enabled='yes',thresholdsC=thresholds)]:
            with self.assertRaises(ValueError):agent.validate_curve(payload)

    def test_hysteresis_zero_small_and_uncapped_values_survive_restart(self):
        settings=dict(enabled=True,thresholdsC=[24.5,24.7,24.7,25,25])
        self.assertEqual(agent.validate_curve(settings)['hysteresisC'],1)
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/'curve.json')
            for hysteresis in [0,.1,10,1.7976931348623157e308]:
                auto=agent.AutoController({},Bridge(),path)
                auto.configure(dict(settings,hysteresisC=hysteresis))
                resumed=agent.AutoController({},Bridge(),path)
                self.assertEqual(resumed.settings['hysteresisC'],hysteresis)
                self.assertEqual(resumed.snapshot()['hysteresisC'],hysteresis)
        for hysteresis in [-.1,float('nan'),float('inf'),float('-inf'),True,None,'0.1']:
            with self.assertRaises(ValueError):
                agent.validate_curve(dict(settings,hysteresisC=hysteresis))

    def test_zero_hysteresis_follows_thresholds_in_both_directions(self):
        thresholds=[24.5,24.7,24.7,25,25]
        for temperature in [24,24.49,24.5,24.6,24.7,24.9,25,25.1,26]:
            expected=sum(temperature>=threshold for threshold in thresholds)
            for previous in range(6):
                self.assertEqual(agent.curve_target(temperature,thresholds,0,previous),expected)
        thresholds=[24.5,24.75,24.75,25,25]
        self.assertEqual(agent.curve_target(24.6875,thresholds,.125,3),3)
        self.assertEqual(agent.curve_target(24.625,thresholds,.125,3),1)
        self.assertEqual(agent.curve_target(20,thresholds,10,5),5)
        self.assertEqual(agent.curve_target(15,thresholds,10,5),3)

    def test_equal_thresholds_select_highest_matching_level(self):
        for thresholds,temperature,expected in [([22,24,24,28,30],24,3),
                                                 ([24,24,24,24,24],24,5),
                                                 ([24,24,24,24,24],23.9,0),
                                                 ([22,22,26,28,30],22,2),
                                                 ([22,22.1,22.1,28,30],22.1,3)]:
            settings=agent.validate_curve(dict(enabled=True,thresholdsC=thresholds))
            self.assertEqual(agent.curve_target(temperature,settings['thresholdsC'],1),expected)
        for previous in range(6):
            self.assertEqual(agent.curve_target(24,[24]*5,1,previous),5)
        self.assertEqual(agent.curve_target(23,[24]*5,1,5),0)
        self.assertEqual(agent.curve_target(23.5,[24]*5,1,5),5)

    def test_target_command_validates_level_and_matching_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge=Bridge();bridge.values['resac_command_ready']='v4'
            telemetry=agent.Telemetry({},bridge)
            processor=agent.CommandProcessor({'_telemetry':telemetry},bridge,str(Path(directory)/'result.json'),automatic=True)
            request=command(type='fan.level',payload=dict(level=5,sessionId=SESSION))
            processor._accept(request);processor._arduino()
            self.assertEqual(bridge.sent,['v4,%s,auto_level,%s,5' % (request['id'],SESSION)])
            bridge.values['resac_command_result']='v4,%s,level_set,%s,4,1,123' % (request['id'],SESSION)
            processor.bridge_due=0;processor._arduino();self.assertIsNone(processor.reply)
            bridge.values['resac_command_result']='v4,%s,level_set,%s,5,5,123' % (request['id'],SESSION)
            bridge.values['resac_command_diagnostic']='v1,%s,12,ok' % request['id']
            processor.bridge_due=0;processor._arduino()
            self.assertEqual(processor.reply['result']['presses'],5)
            events=telemetry.events[:]
            self.assertEqual([event['name'] for event in events],['command.local','bridge.dispatch','command.execute','avr.execute'])
            for event in events:
                self.assertEqual(event['attributes']['command.type'],'fan.level')
                self.assertEqual(event['attributes']['command.source'],'automatic')
                self.assertEqual(event['attributes']['command.id'],request['id'])
                self.assertEqual(event['traceparent'],request['traceparent'])
                self.assertEqual(agent.decode_json(event['attributes']['command.payload']),request['payload'])
            for level in [-1,6,True,2.5]:
                processor._accept(command(type='fan.level',payload=dict(level=level,sessionId=SESSION)))
                self.assertEqual(processor.reply['status'],'failed')

    def test_fresh_samples_dwell_arbiter_manual_override_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge=Bridge();bridge.values['resac_command_ready']='v4'
            bridge.values['resac_sensor_status']='ok'
            bridge.values['resac_sample']='v1,%s,1,25000,40000,1' % SESSION
            clock=[100.0]
            path=str(Path(directory)/'curve.json');state_path=str(Path(directory)/'auto.json')
            commands=SimpleNamespace(current=None,reply=None)
            with patch.object(agent,'monotonic',lambda:clock[0]):
                telemetry=agent.Telemetry({},bridge)
                auto=agent.AutoController({'_telemetry':telemetry},bridge,path,state_path)
                settings=dict(enabled=True,thresholdsC=[22,24,26,28,30],hysteresisC=1)
                auto.configure(settings);auto.tick(commands)
                self.assertFalse(auto.busy,'Retained startup sample must not trigger IR')
                clock[0]+=1;bridge.values['resac_sample']='v1,%s,2,25000,40000,2' % SESSION
                commands.current={};auto.tick(commands);self.assertFalse(auto.busy)
                commands.current=None;clock[0]+=1;auto.tick(commands)
                self.assertTrue(auto.busy);self.assertEqual(auto.target,2)
                auto.tick(commands);self.assertEqual(len(bridge.sent),1)
                request_id=auto.runner.current['id']
                bridge.values['resac_command_result']='v4,%s,level_set,%s,2,1,123' % (request_id,SESSION)
                bridge.values['resac_fan_state']='v2,%s,1,1,2,2,automatic' % SESSION
                clock[0]+=1;auto.tick(commands)
                self.assertFalse(auto.busy)
                execution=next(event for event in telemetry.events if event['name']=='command.execute')
                self.assertEqual(execution['attributes']['command.type'],'fan.level')
                self.assertEqual(execution['attributes']['command.source'],'automatic')
                self.assertEqual(execution['attributes']['command.id'],request_id)
                self.assertEqual(agent.decode_json(execution['attributes']['command.payload']),dict(level=2,sessionId=SESSION))
                clock[0]+=1;bridge.values['resac_sample']='v1,%s,3,30000,40000,3' % SESSION
                auto.tick(commands);self.assertEqual(auto.target,5);self.assertEqual(auto.status,'cooldown')
                clock[0]+=29;bridge.values['resac_sample']='v1,%s,4,30000,40000,4' % SESSION
                auto.tick(commands);self.assertTrue(auto.busy)
                auto.runner=None
                clock[0]+=1;bridge.values['resac_fan_state']='v2,%s,1,1,1,3,manual' % SESSION
                auto.tick(commands)
                self.assertFalse(auto.settings['enabled']);self.assertEqual(auto.status,'manual_override')
                restarted=agent.AutoController({},bridge,path,state_path)
                self.assertFalse(restarted.settings['enabled'])
                self.assertEqual(json.loads(Path(path).read_text())['thresholdsC'],settings['thresholdsC'])
                self.assertEqual(Path(path).stat().st_mode & 0o777,0o600)

    def test_lost_calibration_stale_sensor_and_error_do_not_send(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge=Bridge();bridge.values['resac_command_ready']='v4'
            bridge.values['resac_sensor_status']='ok'
            bridge.values['resac_sample']='v1,%s,1,30000,40000,1' % SESSION
            clock=[100.0]
            with patch.object(agent,'monotonic',lambda:clock[0]):
                auto=agent.AutoController({},bridge,str(Path(directory)/'cfg.json'),str(Path(directory)/'state.json'))
                auto.configure(dict(enabled=True,thresholdsC=[22,24,26,28,30]))
                commands=SimpleNamespace(current=None,reply=None)
                auto.tick(commands)
                clock[0]+=1;bridge.values['resac_fan_state']='v1,%s,0,0,0,2' % SESSION
                bridge.values['resac_sample']='v1,%s,2,30000,40000,2' % SESSION
                auto.tick(commands);self.assertEqual(auto.status,'calibration_required');self.assertFalse(bridge.sent)
                clock[0]+=31;bridge.values['resac_fan_state']='v1,%s,1,0,0,3' % SESSION
                auto.tick(commands);self.assertEqual(auto.status,'sensor_unavailable');self.assertFalse(auto.busy)
                clock[0]+=1;bridge.values['resac_sensor_status']='read_error'
                bridge.values['resac_sample']='v1,%s,3,30000,40000,3' % SESSION
                auto.tick(commands);self.assertFalse(auto.busy)

    def test_result_persistence_cannot_clear_an_active_automatic_mailbox(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge=Bridge()
            processor=agent.CommandProcessor({},bridge,str(Path(directory)/'state.json'))
            processor._accept(command(type='unsupported'))
            bridge.values['resac_command']='automatic-job-in-flight'
            processor.tick(allow_execution=False)
            self.assertEqual(bridge.values['resac_command'],'automatic-job-in-flight')
            self.assertFalse(Path(processor.state_path).exists())
