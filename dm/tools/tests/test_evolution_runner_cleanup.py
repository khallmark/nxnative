"""Runner shutdown keeps the primary failure while releasing owned resources."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class RunnerCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.output=self.root/'output'
        spec=importlib.util.spec_from_file_location('cleanup_test_runner',Path(__file__).resolve().parents[3]/'scripts'/'evolve_replay.py')
        assert spec is not None and spec.loader is not None
        self.runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.runner)
        self.session=self.root/'session.json';self.objective=self.root/'objective.json'
        config=dict(recovery={'death_steps':[{}]},ui={
            'pause':[dict(read='hud',equals=0)],'gameplay':[dict(read='hud',equals=1)]})
        self.session.write_text(json.dumps(config))
        self.objective.write_text(json.dumps(dict(start=[0,0,0],target=[1,0,0],horizon_frames=60,segments=1,start_health=12)))
        self.events=[];self.logs=[];self.cleanup_failures={};self.primary=None
        self.bridge=SimpleNamespace(config=config,last={'health':12},running=False,
            rpc=lambda verb:dict(active=False),sample=lambda **kwargs:dict(health=12,reads={'hud':[1]}),
            close=self.close_bridge)
        self.backend=SimpleNamespace(uncertain=False,close=lambda:self.close_resource('backend.close'))
        self.evolution=SimpleNamespace(state={'generation':0},pending=lambda:None,advance=self.advance,
            db=SimpleNamespace(close=lambda:self.close_resource('database.close')))
        self.stderr=io.StringIO()

    def close_resource(self,name):
        self.events.append(name)
        if name in self.cleanup_failures:raise self.cleanup_failures[name]

    def close_bridge(self):
        self.events.append(('bridge.running',self.bridge.running))
        self.close_resource('bridge.close')

    def advance(self):
        self.evolution.state['generation']=1
        return {'generation':1}

    def restore_failure(self):
        if self.primary is not None:raise self.primary
        return {},0.

    def episodes(self,bridge,spec,log,backend):
        self.logs.append(log)
        return SimpleNamespace(checkpoint=SimpleNamespace(verify_files=lambda:None),restore=self.restore_failure)

    def run_main(self,cleanup_log_fails=False,interruption_log_fails=False):
        original_open=Path.open;owner=self
        class TrajectoryLog:
            def __init__(self,stream):self.stream=stream
            def __getattr__(self,name):return getattr(self.stream,name)
            def close(self):
                self.stream.close();owner.close_resource('trajectory.close')
        def opened(path,*args,**kwargs):
            if cleanup_log_fails and path.name=='cleanup-errors.jsonl':raise OSError('cleanup log unavailable')
            if interruption_log_fails and path.name=='interruptions.jsonl':raise OSError('interruption log unavailable')
            stream=original_open(path,*args,**kwargs)
            return TrajectoryLog(stream) if path.name=='trajectory.jsonl' else stream
        argv=['evolve_replay.py','--session',str(self.session),'--objective',str(self.objective),
              '--output',str(self.output),'--generations','1']
        with (patch.object(self.runner,'prepare_fast_reset',return_value=SimpleNamespace(fingerprint=lambda:b'test')),
             patch.object(self.runner,'ReplayBridge',return_value=self.bridge),
             patch.object(self.runner,'FridaReset',return_value=self.backend),
             patch.object(self.runner,'Evolution',return_value=self.evolution),
             patch.object(self.runner,'NativeEpisodes',side_effect=self.episodes),
             patch.object(self.runner.signal,'signal'),patch.object(sys,'argv',argv),
             patch.object(Path,'open',opened),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(self.stderr)):
            self.runner.main()

    def cleanup_records(self):
        return [json.loads(line) for line in (self.output/'cleanup-errors.jsonl').read_text().splitlines()]

    def test_primary_failure_and_traceback_survive_all_cleanup_failures(self):
        primary=self.runner.TrialStopped('original checkpoint failure');self.primary=primary
        self.backend.uncertain=True
        self.cleanup_failures={name:OSError(name+' failed') for name in
            ('backend.close','bridge.close','trajectory.close','database.close')}
        try:self.run_main()
        except self.runner.TrialStopped as error:
            self.assertIs(error,primary)
            frames=traceback.extract_tb(error.__traceback__)
            self.assertEqual(frames[-1].name,'restore_failure')
            self.assertNotIn('close_resource',[frame.name for frame in frames])
        else:self.fail('primary exception did not propagate')
        self.assertEqual(self.events,['backend.close',('bridge.running',False),'bridge.close','trajectory.close','database.close'])
        self.assertTrue(self.logs[0].closed)
        interruption=json.loads((self.output/'interruptions.jsonl').read_text())
        self.assertEqual(interruption['error'],'original checkpoint failure')
        self.assertIn('restore_failure',interruption['traceback'])
        self.assertEqual([record['operation'] for record in self.cleanup_records()],list(self.cleanup_failures))

    def test_first_cleanup_failure_propagates_without_a_primary(self):
        failure=OSError('instrumentation detach failed')
        self.cleanup_failures={'backend.close':failure,'database.close':OSError('later database failure')}
        try:self.run_main()
        except OSError as error:self.assertIs(error,failure)
        else:self.fail('cleanup exception did not propagate')
        self.assertIn(('bridge.running',False),self.events)
        self.assertTrue(self.logs[0].closed);self.assertIn('database.close',self.events)
        self.assertEqual([record['operation'] for record in self.cleanup_records()],['backend.close','database.close'])
        self.assertFalse((self.output/'interruptions.jsonl').exists())

    def test_log_close_failure_does_not_skip_database_close(self):
        failure=OSError('trajectory close failed')
        self.cleanup_failures={'trajectory.close':failure}
        with self.assertRaisesRegex(OSError,'trajectory close failed'):self.run_main()
        self.assertEqual(self.events[-2:],['trajectory.close','database.close'])
        self.assertTrue(self.logs[0].closed)
        self.assertEqual(self.cleanup_records()[0]['operation'],'trajectory.close')

    def test_keyboard_interrupt_remains_primary_when_backend_close_fails(self):
        primary=KeyboardInterrupt();self.primary=primary
        self.cleanup_failures={'backend.close':OSError('detach failed')}
        try:self.run_main()
        except KeyboardInterrupt as error:self.assertIs(error,primary)
        else:self.fail('interrupt did not propagate')
        self.assertTrue(self.logs[0].closed);self.assertIn(('bridge.running',False),self.events)
        self.assertEqual(self.cleanup_records()[0]['error'],'detach failed')

    def test_cleanup_diagnostic_write_failure_does_not_mask_primary(self):
        primary=self.runner.TrialStopped('first failure');self.primary=primary
        self.cleanup_failures={'backend.close':OSError('second failure')}
        try:self.run_main(cleanup_log_fails=True)
        except self.runner.TrialStopped as error:self.assertIs(error,primary)
        else:self.fail('primary exception did not propagate')
        self.assertIn('cleanup log unavailable',self.stderr.getvalue())
        self.assertTrue(self.logs[0].closed);self.assertIn('database.close',self.events)

    def test_interruption_log_failure_does_not_replace_original_error(self):
        primary=self.runner.TrialStopped('original restore failure');self.primary=primary
        try:self.run_main(interruption_log_fails=True)
        except self.runner.TrialStopped as error:self.assertIs(error,primary)
        else:self.fail('primary exception did not propagate')
        self.assertEqual(self.cleanup_records()[0]['operation'],'interruptions.write')
        self.assertEqual(self.cleanup_records()[0]['error'],'interruption log unavailable')
        self.assertTrue(self.logs[0].closed);self.assertIn('database.close',self.events)

    def test_successful_cleanup_stays_silent_and_closes_logs(self):
        self.run_main()
        self.assertTrue(self.logs[0].closed)
        self.assertEqual(self.stderr.getvalue(),'')
        self.assertFalse((self.output/'cleanup-errors.jsonl').exists())
        self.assertEqual(self.events,['backend.close',('bridge.running',True),'bridge.close','trajectory.close','database.close'])


if __name__=='__main__':unittest.main()
