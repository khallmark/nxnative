"""Qualification barriers and resident movement use the same input owner."""
from contextlib import contextmanager
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from typing import Any,Callable
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evolution.core import Evolution
from evolution.episodes import NativeUIInterruption
from evolution.handoff import (_atomic,_move,_move_guard,assert_no_pending_handoff,
                               prepare_objective_handoff,wait_for_handoff)
from evolution.live import TrialStopped
from evolution.timeline import gene_space,seed_genome
from test_evolution_handoff import FakeBridge


def result(epoch=7) -> dict[str,Any]:
    return dict(success=True,reason='objective',score=175.,native_epoch=epoch,objective_frame=25,
        position=(1.,0.,0.),terminal_status=dict(active=False,reason='cancelled',epoch=epoch,
            start_frame=100,end_frame=200,current_frame=132,stop_frame=130,last_health=9))


class QualificationTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.output=Path(tmp.name)
        task=dict(start=[0,0,0],target=[1,0,0],horizon_frames=100,start_health=9,segments=1,stop_on_objective=True)
        self.e=Evolution(self.output/'evolution.sqlite',[task,dict(task,target=[2,0,0])],population=4,repeats=2,
                         bounds=gene_space(1),initial_genome=seed_genome(1),implementation='test')
        self.addCleanup(self.e.db.close);self.genome=self.e.state['candidates'][0]
        self.config=dict(control=str(self.output/'control'),pid=123)
        self.keys=[(0,0,0,0),(0,0,1,0),(0,0,0,1),(0,0,1,1)]
    def prepare(self,key=None,row=None):
        return prepare_objective_handoff(self.e,key or self.keys[-1],row or result(),self.output,
            self.config,genome=self.genome,implementation='test')
    def previous(self):
        for key in self.keys[:-1]:self.e.record(key,result())
    def test_only_exact_candidate_complete_cartesian_repeats_can_qualify(self):
        self.assertIsNone(self.prepare());self.assertFalse((self.output/'handoff.json').exists())
        self.e.record((0,1,1,1),result())
        for key in self.keys[:-2]:self.e.record(key,result())
        self.assertIsNone(self.prepare())
        self.e.record(self.keys[-2],result())
        intent=self.prepare();assert intent is not None
        self.assertEqual([r['key']for r in intent['event']['qualification']['trials']],
                         [list(k)for k in sorted(self.keys)])
        self.assertNotIn(self.keys[-1],self.e.completed)
        self.assertEqual(self.e.state['generation'],0)
        self.assertIsNotNone(self.e.pending())
        with self.assertRaises(TrialStopped):assert_no_pending_handoff(self.output/'different',self.config)
    def test_failed_or_horizon_repeat_does_not_qualify(self):
        for success,reason in [(False,'objective'),(True,'horizon')]:
            with self.subTest(reason=reason):
                self.e.db.execute('DELETE FROM trials');self.e.db.commit()
                for key in self.keys[:-1]:self.e.record(key,result())
                failed=dict(result(),success=success,reason=reason)
                self.e.db.execute('UPDATE trials SET result=? WHERE task=0 AND repeat=0',
                                  (json.dumps(dict(failed,genome=self.genome)),));self.e.db.commit()
                self.assertIsNone(self.prepare());self.assertFalse((self.output/'handoff.json').exists())
    def test_malformed_terminal_or_genome_evidence_never_qualifies(self):
        self.previous()
        for bad in [dict(result(),native_epoch=0),dict(result(),terminal_status={}),
                    dict(result(),terminal_status={**result()['terminal_status'],'active':True}),
                    dict(result(),objective_frame=True)]:
            with self.subTest(bad=bad),self.assertRaises(TrialStopped):self.prepare(row=bad)
        self.e.db.execute('UPDATE trials SET result=? WHERE task=0 AND repeat=0',
            (json.dumps(dict(result(),genome={})),));self.e.db.commit()
        with self.assertRaisesRegex(TrialStopped,'genome'):self.prepare()
    def test_control_barrier_survives_output_publication_failure_before_record(self):
        self.previous();original=_atomic
        def write(path,value):
            if path==self.output/'handoff.json':raise OSError('output unavailable')
            original(path,value)
        with patch('evolution.handoff._atomic',side_effect=write),self.assertRaisesRegex(OSError,'output unavailable'):
            self.prepare()
        self.assertNotIn(self.keys[-1],self.e.completed)
        with self.assertRaises(TrialStopped):assert_no_pending_handoff(self.output/'new',self.config)
    def test_archive_requires_final_record_and_preserves_scored_rows(self):
        self.previous();bridge=FakeBridge();bridge.config.update(self.config)
        self.config=bridge.config;intent=self.prepare();assert intent is not None
        with self.assertRaisesRegex(TrialStopped,'recorded qualification'):
            wait_for_handoff(bridge,None,self.output,key=self.keys[-1],genome=self.genome,
                objective=self.e.tasks[-1],implementation='test',log=io.StringIO(),intent=intent,evolution=self.e)
        # JSON normalization must reconcile tuple-valued observations with SQLite.
        self.e.record(self.keys[-1],result())
        def command(_):
            (self.output/'handoff-command.json').write_text(json.dumps(dict(
                handoff_id=intent['pending']['handoff_id'],sequence=1,action='shutdown')))
        with patch('evolution.handoff.time.sleep',side_effect=command):
            self.assertEqual(wait_for_handoff(bridge,None,self.output,key=self.keys[-1],genome=self.genome,
                objective=self.e.tasks[-1],implementation='test',log=io.StringIO(),intent=intent,evolution=self.e),'shutdown')
        archived=json.loads(Path(intent['pending']['event_path']).read_text())
        self.assertEqual(archived['kind'],'objective');self.assertTrue(archived['scored'])
        self.assertEqual(len(archived['qualification']['trials']),4)
        self.assertEqual(archived['qualification']['signature'],self.e.state['signature'])
        self.assertEqual(archived['evidence']['cancel_status'],result()['terminal_status'])
        self.assertFalse(bridge.running);self.assertEqual(bridge.inputs,[])
    def test_atomic_publication_syncs_file_before_rename_and_directory_after(self):
        import os
        events=[];real_fsync=os.fsync;real_replace=Path.replace
        def sync(fd):events.append('sync');return real_fsync(fd)
        def replace(path,target):events.append('rename');return real_replace(path,target)
        with patch('evolution.handoff.os.fsync',side_effect=sync),patch.object(Path,'replace',replace):
            _atomic(self.output/'value.json',{'a':1})
        self.assertEqual(events,['sync','rename','sync'])


class MovementBridge(FakeBridge):
    def __init__(self):
        super().__init__();self.world_xz={'rotation_address':'0x1000'}
        self.config['reads']['health']={'address':'0x2000'}
        self.config['native_guard_health_address']='0x3000'
        self.frame=100;self.active=False;self.started=None;self.hp=9;self.hud=1;self.position=[1,2,3]
        self.moves=[];self.budgets=[];self.failure=None;self.cancel_failure=None;self.bad_cancel=False
        self.change: Callable[[Any],None] | None=None;self.stall=False
    @contextmanager
    def request_deadline(self,seconds):
        self.budgets.append(seconds);yield
    def row(self) -> dict[str,Any]:
        return dict(epoch=self.epoch,active=self.active,reason='running'if self.active else ('completed'if self.started and self.frame>=self.started+3 else 'cancelled'),
            start_frame=self.started or 100,end_frame=(self.started or 100)+3,current_frame=self.frame,
            stop_frame=0 if self.active else self.frame,last_health=None if self.started and self.frame<self.started else self.hp,
            guard_address='0x3000',guard_threshold=0,left_stick_space='world_xz',world_xz=self.world_xz)
    def rpc(self,verb):return self.row()
    def sample(self,keys=None,hold=None,actors=False):
        if self.active and not self.stall:
            assert self.started is not None
            self.frame+=1
            if self.change:self.change(self)
            if self.frame>=self.started+3:self.active=False
        return dict(position=self.position,health=self.hp,reads={'hud':[self.hud],'self':[2]})
    def start_replay(self,timeline,health_address,episode=True):
        self.moves.append((timeline,health_address,episode));self.epoch+=1;self.started=self.frame+1
        self.active=True;self.owns_replay=True;self.episode_epoch=self.epoch
        if self.failure:raise self.failure
        return self.row()
    def cancel_replay(self):
        self.cancelled+=1
        if self.cancel_failure:raise self.cancel_failure
        self.active=False;self.owns_replay=False;self.episode_epoch=None
        row=self.row()
        if self.bad_cancel:row['epoch']+=1
        return row


class MovementTests(unittest.TestCase):
    def setUp(self):
        self.b=MovementBridge();self.state=self.b.sample();self.status=self.b.row()
        self.command=dict(handoff_id='x',sequence=1,action='move',expected_epoch=7,expected_ui='gameplay',
            expected_reads={'hud':[1]},world_xz=[.6,.8],frames=3,bounds=[[0,4],[0,4],[0,4]],timeout_seconds=.5)
    def run_move(self):
        health=_move_guard(self.command,self.b,self.state,self.status)
        return _move(self.b,None,self.command,self.state,self.status,health)
    def test_native_world_segment_and_authoritative_health_are_bound_once(self):
        out=self.run_move();self.assertEqual(out['reason'],'completed')
        self.assertEqual(self.b.moves,[('f0:L0.600000000/0.800000000~f3\n',0x3000,True)])
        self.assertEqual(self.b.cancelled,1);self.assertFalse(self.b.owns_replay)
        self.assertEqual(self.b.budgets,[.5,2.])
    def test_malformed_or_unbounded_commands_cannot_dispatch(self):
        for changes in [{'keys':'A'},{'frames':31},{'frames':True},{'world_xz':[1,1]},
                        {'world_xz':[float('nan'),0]},{'bounds':[[0,1]]*3},
                        {'expected_ui':'pause'},{'timeout_seconds':10},{'expected_epoch':8},
                        {'expected_reads':{'self':[2]}},{'raw_timeline':'f0:A~f3'}]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                _move_guard({**self.command,**changes},self.b,self.state,self.status)
        self.assertEqual(self.b.moves,[])
    def test_damage_ui_and_bounds_stop_and_remain_inactive(self):
        for attribute,value,reason in [('hp',8,'damage'),('hud',0,'ui'),('position',[8,2,3],'bounds')]:
            with self.subTest(reason=reason):
                self.setUp();self.b.change=lambda b,a=attribute,v=value:setattr(b,a,v)
                self.assertEqual(self.run_move()['reason'],reason);self.assertEqual(self.b.cancelled,1)
                self.assertFalse(self.b.owns_replay)
    def test_stalled_frames_stop_at_wall_budget_and_cancel_before_first_frame(self):
        self.b.stall=True
        with patch('evolution.handoff.time.monotonic',side_effect=[0.,0.,1.]),patch('evolution.handoff.time.sleep'):
            out=self.run_move()
        self.assertEqual(out['reason'],'timeout');self.assertEqual(self.b.cancelled,1)
        self.assertIsNone(out['status']['last_health'])
    def test_missing_start_ack_still_cancels_and_preserves_both_failures(self):
        original=OSError('start ACK missing');self.b.failure=original;self.b.cancel_failure=OSError('cancel ACK missing')
        with self.assertRaises(OSError) as caught:self.run_move()
        self.assertIs(caught.exception,original);self.assertEqual(self.b.cancelled,1)
        self.assertIn('cancel ACK missing',caught.exception.__notes__[0])
    def test_malformed_start_ack_and_wrong_cancel_epoch_fail_closed(self):
        start=self.b.start_replay
        def invalid(*args,**kwargs):start(*args,**kwargs);return {}
        self.b.start_replay=invalid
        with self.assertRaises(TrialStopped):self.run_move()
        self.assertEqual(self.b.cancelled,1)
        self.setUp();self.b.bad_cancel=True
        with self.assertRaises(TrialStopped):self.run_move()
    def test_resident_move_consumes_sequence_before_dispatch_and_rejects_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp);self.b.config['control']=str(output/'control')
            event=NativeUIInterruption(dict(native_epoch=7,cancel_status=self.status))
            commands=iter([{k:v for k,v in self.command.items()if k!='handoff_id'},
                {**{k:v for k,v in self.command.items()if k!='handoff_id'},'duplicate':True},
                dict(sequence=2,action='shutdown')])
            def publish(seconds):
                if seconds<.1:return
                pending=json.loads((output/'handoff.json').read_text())
                command=dict(handoff_id=pending['handoff_id'],**next(commands))
                (output/'handoff-command.json').write_text(json.dumps(command))
            start=self.b.start_replay
            def dispatch(*args,**kwargs):
                pending=json.loads((output/'handoff.json').read_text())
                rows=Path(pending['event_path']).with_name('commands.jsonl').read_text().splitlines()
                self.assertEqual(json.loads(rows[-1])['phase'],'accepted')
                return start(*args,**kwargs)
            self.b.start_replay=dispatch
            with patch('evolution.handoff.time.sleep',side_effect=publish):
                wait_for_handoff(self.b,None,output,event,key=(0,0,0,0),genome={},objective={},
                    implementation='test',log=io.StringIO())
            self.assertEqual(len(self.b.moves),1);self.assertEqual(self.b.cancelled,1)
            pending=json.loads((output/'handoff.json').read_text())
            rows=[json.loads(line)for line in Path(pending['event_path']).with_name('commands.jsonl').read_text().splitlines()]
            self.assertEqual([row['phase']for row in rows],
                ['ready','accepted','moved','rejected','accepted','shutdown_requested'])

    def test_identity_failure_still_cancels_and_never_becomes_a_moved_ack(self):
        def identity(_):raise TrialStopped('player identity changed')
        self.b.change=identity
        with self.assertRaisesRegex(TrialStopped,'identity changed'):self.run_move()
        self.assertEqual(self.b.cancelled,1)


class ProgressionRunnerTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name);self.output=self.root/'output'
        module=importlib.util.spec_from_file_location('progression_runner',Path(__file__).resolve().parents[3]/'scripts'/'evolve_replay.py')
        assert module is not None and module.loader is not None
        self.runner=importlib.util.module_from_spec(module);module.loader.exec_module(self.runner)
        self.config: dict[str,Any]=dict(control=str(self.root/'control'),pid=123,recovery={'death_steps':[{}]},
            ui={'pause':[dict(read='hud',equals=0)],'gameplay':[dict(read='hud',equals=1)]})
        self.spec=dict(start=[0,0,0],target=[1,0,0],horizon_frames=100,start_health=9,segments=1,stop_on_objective=True)
        self.session=self.root/'session.json';self.objective=self.root/'objective.json'
        self.bridge=SimpleNamespace(config=self.config,last=None,running=False,rpc=lambda verb:dict(active=False),
            sample=lambda **kwargs:dict(health=9,reads={'hud':[1]}),close=lambda:self.events.append(('close',self.bridge.running)))
        self.events=[];self.rows=[];self.record_failure=False
    def run_main(self,flags=('--resident-handoff','--progression-handoff')):
        self.session.write_text(json.dumps(self.config));self.objective.write_text(json.dumps(self.spec))
        def create(*args,**kwargs):
            evolution=Evolution(*args,**kwargs);record=evolution.record
            def record_trial(key,result):
                marker=Path(self.config['control']+'.handoff.json')
                self.rows.append((key,marker.exists()))
                if marker.exists() and self.record_failure:raise OSError('DB final record failed')
                record(key,result)
            evolution.record=record_trial
            return evolution
        def restore():self.events.append('restore');return {},0.
        def evaluate(genome):self.events.append('evaluate');return result()
        def hold(bridge,backend,output,**kwargs):
            evolution=kwargs['evolution'];intent=kwargs['intent']
            self.assertIn((0,0,0,1),evolution.completed)
            self.assertEqual(len(intent['event']['qualification']['trials']),2)
            self.assertFalse(bridge.running);self.assertEqual(evolution.state['generation'],0)
            self.events.append('hold');return 'shutdown'
        argv=['evolve_replay.py','--session',str(self.session),'--objective',str(self.objective),
            '--output',str(self.output),'--generations','1','--population','4','--repeats','2',*flags]
        env=SimpleNamespace(checkpoint=SimpleNamespace(verify_files=lambda:None),restore=restore,evaluate=evaluate)
        with (patch.object(self.runner,'prepare_fast_reset',return_value=None),
              patch.object(self.runner,'Evolution',side_effect=create),
              patch.object(self.runner,'ReplayBridge',return_value=self.bridge),
              patch.object(self.runner,'NativeEpisodes',return_value=env),
              patch.object(self.runner,'wait_for_handoff',side_effect=hold),
              patch.object(self.runner.signal,'signal'),patch.object(sys,'argv',argv),
              contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO())):
            self.runner.main()
    def test_interleaved_candidate_qualifies_before_restore_and_full_generation(self):
        self.run_main()
        self.assertEqual(self.events.count('evaluate'),5);self.assertEqual(self.events.count('restore'),5)
        self.assertEqual(self.rows,[(k,i==4)for i,k in enumerate(
            [(0,0,0,0),(0,1,0,0),(0,2,0,0),(0,3,0,0),(0,0,0,1)])])
        self.assertEqual(self.events[-2:],['hold',('close',False)])
        import sqlite3
        with contextlib.closing(sqlite3.connect(self.output/'evolution.sqlite')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM trials').fetchone()[0],5)
    def test_final_record_failure_retains_control_barrier_without_cleanup_input(self):
        self.record_failure=True
        with self.assertRaisesRegex(OSError,'DB final record failed'):self.run_main()
        self.assertEqual(self.events[-1],('close',False));self.assertNotIn('hold',self.events)
        with self.assertRaises(TrialStopped):assert_no_pending_handoff(self.root/'new-output',self.config)
    def test_default_training_still_finishes_generation_without_handoff(self):
        self.run_main(flags=())
        self.assertEqual(self.events.count('evaluate'),8);self.assertNotIn('hold',self.events)
        self.assertTrue(all(not marked for _,marked in self.rows))
    def test_flag_dependencies_reject_before_input_owner_creation(self):
        with self.assertRaises(SystemExit):self.run_main(flags=('--progression-handoff',))
        self.assertEqual(self.events,[])
        self.spec['stop_on_objective']=False
        with self.assertRaises(SystemExit):self.run_main()
        self.assertEqual(self.events,[])
