"""The production runner catches UI handoffs before fitness, reset and teardown."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class RunnerHandoffTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        spec=importlib.util.spec_from_file_location('handoff_test_runner',Path(__file__).resolve().parents[3]/'scripts'/'evolve_replay.py')
        assert spec and spec.loader
        self.runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.runner)
        self.output=self.root/'output';self.output.mkdir()
        self.session=self.root/'session.json';self.objective=self.root/'objective.json'
        config=dict(recovery={'death_steps':[{}]},ui={'gameplay':[dict(read='hud',equals=1)],'pause':[dict(read='hud',equals=0)]})
        self.session.write_text(json.dumps(config))
        self.objective.write_text(json.dumps(dict(start=[0,0,0],target=[1,0,0],horizon_frames=60,start_health=12,segments=1)))
        self.events=[];self.genome={'0000.lx':.3};self.event=self.runner.NativeUIInterruption({'timeline':'exact timeline'})
        self.bridge=SimpleNamespace(config=config,running=False,last={},rpc=lambda *args:dict(active=False),
            sample=lambda **kw:dict(health=12,reads={'hud':[1]}),close=lambda:self.events.append('bridge.close'))
        self.backend=SimpleNamespace(uncertain=False,close=lambda:self.events.append('backend.close'))
        self.evolution=SimpleNamespace(state={'generation':4,'candidates':[self.genome]},
            pending=lambda:(4,0,0,1),record=lambda *args:self.fail('interrupted UI was scored'),
            advance=lambda:self.fail('interrupted UI advanced generation'),db=SimpleNamespace(close=lambda:self.events.append('database.close')))
    def restore(self):self.events.append('restore');return {},.1
    def evaluate(self,genome):
        self.events.append('evaluate');self.assertEqual(genome,self.genome);raise self.event
    def run_main(self,hold):
        def env(*args):return SimpleNamespace(checkpoint=SimpleNamespace(verify_files=lambda:None),restore=self.restore,evaluate=self.evaluate)
        argv=['evolve_replay.py','--session',str(self.session),'--objective',str(self.objective),'--output',str(self.output),'--resident-handoff']
        with (patch.object(self.runner,'prepare_fast_reset',return_value=SimpleNamespace(fingerprint=lambda:b'test')),
              patch.object(self.runner,'ReplayBridge',return_value=self.bridge) as create,
              patch.object(self.runner,'FridaReset',return_value=self.backend),
              patch.object(self.runner,'Evolution',return_value=self.evolution),
              patch.object(self.runner,'NativeEpisodes',side_effect=env),
              patch.object(self.runner,'wait_for_handoff',side_effect=hold),
              patch.object(self.runner.signal,'signal'),patch.object(sys,'argv',argv),
              contextlib.redirect_stdout(io.StringIO())):
            try:self.runner.main()
            finally:self.created=create.call_count
    def test_typed_event_holds_same_owner_before_explicit_shutdown(self):
        def hold(bridge,backend,output,event,**fields):
            self.assertIs(bridge,self.bridge);self.assertIs(backend,self.backend);self.assertIs(event,self.event)
            self.assertEqual(self.events,['restore','evaluate'])
            self.assertEqual(fields['key'],(4,0,0,1));self.assertEqual(fields['genome'],self.genome)
            self.assertTrue(fields['resident']);self.assertFalse(fields['log'].closed)
            self.events.append('explicit shutdown');return 'shutdown'
        self.run_main(hold)
        self.assertEqual(self.events,['restore','evaluate','explicit shutdown','backend.close','bridge.close','database.close'])
    def test_pending_handoff_blocks_start_before_attachment_or_restore(self):
        (self.output/'handoff.json').write_text('{"phase":"shutdown_requested"}')
        with self.assertRaisesRegex(self.runner.TrialStopped,'Unresolved'):self.run_main(lambda *a,**k:self.fail('hold reached'))
        self.assertEqual(self.created,0);self.assertEqual(self.events,[])
    def test_control_marker_blocks_new_output_before_attachment(self):
        config=json.loads(self.session.read_text());control=self.root/'control.txt'
        config['control']=str(control);self.session.write_text(json.dumps(config))
        (self.root/'control.txt.handoff.json').write_text('{"resolved":false}')
        with self.assertRaisesRegex(self.runner.TrialStopped,'Unresolved'):
            self.run_main(lambda *a,**k:self.fail('hold reached'))
        self.assertEqual(self.created,0);self.assertEqual(self.events,[])

    def test_non_ui_cleanup_failure_does_not_enter_handoff(self):
        self.event=self.runner.TrialStopped('lethal cleanup uncertain')
        with self.assertRaisesRegex(self.runner.TrialStopped,'lethal cleanup uncertain'):
            self.run_main(lambda *a,**k:self.fail('failed cleanup entered handoff'))
        self.assertEqual(self.events.count('restore'),1);self.assertIn('backend.close',self.events)
    def test_handoff_runtime_failure_is_primary_through_teardown(self):
        original=OSError('handoff lost input ACK')
        def hold(*args,**kwargs):raise original
        def close():raise OSError('detach failed')
        self.backend.close=close
        with self.assertRaises(OSError) as caught:self.run_main(hold)
        self.assertIs(caught.exception,original)
        self.assertEqual(self.events.count('restore'),1)
        self.assertIn('detach failed',(self.output/'cleanup-errors.jsonl').read_text())


if __name__=='__main__':unittest.main()
