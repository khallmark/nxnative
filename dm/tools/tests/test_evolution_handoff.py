"""Resident UI commands preserve provenance and cannot replay or reset an attempt."""
from collections.abc import Iterable
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evolution.episodes import NativeUIInterruption
from evolution.handoff import assert_no_pending_handoff,wait_for_handoff
from evolution.live import TrialStopped


class FakeBridge:
    def __init__(self):
        self.config: dict[str,Any]=dict(pid=123,identity={'self':[2]},reads={'hud':{},'self':{}},
                         ui={'gameplay':[dict(read='hud',equals=1)]})
        self.running=True;self.episode_epoch=None;self.owns_replay=False
        self.epoch=7;self.inputs=[];self.cancelled=0
        self.failure: BaseException | None=None
        self.cancel_failure: BaseException | None=None
    def rpc(self,verb):return dict(active=False,epoch=self.epoch,reason='cancelled',current_frame=20)
    def sample(self,keys=None,hold=None,actors=False):
        if keys is not None:
            self.inputs.append((keys,hold));self.epoch+=1;self.owns_replay=True
            if self.failure:raise self.failure
        return dict(position=[1,2,3],health=9,reads={'hud':[0],'self':[2]})
    def cancel_replay(self):
        self.cancelled+=1
        if self.cancel_failure:raise self.cancel_failure
        self.owns_replay=False;return self.rpc('replay_status')


class HandoffTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.output=Path(tmp.name)
        self.bridge=FakeBridge();self.log=io.StringIO();self.genome={'0000.lx':.4}
        self.event=NativeUIInterruption(dict(native_epoch=7,state=self.bridge.sample(),
            timeline='f0:X~f6\n',observation={'epoch':7},status={'epoch':7},
            cancel_status=dict(active=False,epoch=7,reason='cancelled')))
    def run_hold(self,commands: Iterable[dict[str,Any]]=(),backend=None,resident=True):
        commands=iter(commands)
        def next_command(seconds):
            pending=json.loads((self.output/'handoff.json').read_text())
            command=next(commands)
            command=dict(handoff_id=pending['handoff_id'],**command) if 'handoff_id' not in command else command
            temporary=self.output/'command.tmp';temporary.write_text(json.dumps(command));temporary.replace(self.output/'handoff-command.json')
        with patch('evolution.handoff.time.sleep',side_effect=next_command):
            return wait_for_handoff(self.bridge,backend,self.output,self.event,key=(4,1,0,1),genome=self.genome,
                objective={'id':'original'},implementation='test-sha',log=self.log,resident=resident)
    def input(self,sequence=1,**fields):
        return dict(sequence=sequence,action='input',expected_epoch=7,expected_reads={'hud':[0]},keys='A',hold_seconds=.1,**fields)
    def records(self):
        pending=json.loads((self.output/'handoff.json').read_text());archive=Path(pending['event_path']).parent
        return json.loads((archive/'event.json').read_text()),[json.loads(x) for x in (archive/'commands.jsonl').read_text().splitlines()]
    def test_holds_same_owner_and_archives_without_scoring_or_reset(self):
        self.assertEqual(self.run_hold([dict(sequence=1,action='shutdown')]),'shutdown')
        event,rows=self.records();self.assertEqual(event['key'],[4,1,0,1]);self.assertEqual(event['genome'],self.genome)
        self.assertEqual(event['evidence']['timeline'],'f0:X~f6\n');self.assertFalse(event['scored'])
        self.assertEqual(self.bridge.inputs,[]);self.assertFalse(self.bridge.running)
        self.assertEqual(rows[-1]['phase'],'shutdown_requested')
        with self.assertRaisesRegex(TrialStopped,'Unresolved'):assert_no_pending_handoff(self.output)
    def test_duplicate_stale_and_invalid_guards_cannot_double_press(self):
        duplicate={**self.input(),'extra':'duplicate envelope'}
        wrong={**self.input(2),'handoff_id':'stale'}
        changed={**self.input(2),'expected_epoch':8,'expected_reads':{'hud':[1]}}
        self.run_hold([self.input(),duplicate,wrong,changed,dict(sequence=3,action='shutdown')])
        _,rows=self.records();self.assertEqual(self.bridge.inputs,[('A',.1)])
        self.assertEqual(self.bridge.cancelled,1)
        self.assertEqual([r['sequence'] for r in rows if r['phase']=='accepted'],[1,2,3])
        self.assertEqual(len([r for r in rows if r['phase']=='rejected']),3)
    def test_input_sequence_is_durable_before_dispatch(self):
        original=self.bridge.sample
        def sample(keys=None,*args,**kwargs):
            if keys is not None:
                _,rows=self.records();self.assertEqual(rows[-1]['phase'],'accepted');self.assertEqual(rows[-1]['sequence'],1)
            return original(keys,*args,**kwargs)
        self.bridge.sample=sample
        self.run_hold([self.input(),dict(sequence=2,action='shutdown')])
    def test_unsupported_macro_resume_identity_only_and_bad_hold_are_rejected(self):
        bad=[dict(sequence=1,action='resume'),{**self.input(2),'keys':'A+X'},
             {**self.input(3),'expected_reads':{'self':[2]}},{**self.input(4),'hold_seconds':1.}]
        self.run_hold(bad+[dict(sequence=5,action='shutdown')]);self.assertEqual(self.bridge.inputs,[])
    def test_current_epoch_is_required_before_input(self):
        self.run_hold([{**self.input(),'expected_epoch':6},dict(sequence=2,action='shutdown')]);self.assertEqual(self.bridge.inputs,[])
    def test_dispatch_failure_keeps_original_and_releases_native_input(self):
        original=OSError('input ACK lost');self.bridge.failure=original
        self.bridge.cancel_failure=OSError('cancel ACK lost')
        with self.assertRaises(OSError) as caught:self.run_hold([self.input()])
        self.assertIs(caught.exception,original);self.assertEqual(self.bridge.cancelled,1)
        self.assertIn('cancel ACK lost',caught.exception.__notes__[0])
        event,rows=self.records();self.assertEqual(event['genome'],self.genome);self.assertEqual(rows[-1]['phase'],'failed')
    def test_uncertain_detached_or_busy_backend_never_acknowledges_ready(self):
        for change in [{'uncertain':True},{'session':SimpleNamespace(is_detached=True)},
                       {'lethal':SimpleNamespace(armed=True)}, {'armed':True}]:
            with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
                self.output=Path(tmp)
                attrs=dict(failed=False,uncertain=False,closed=False,armed=False,session=SimpleNamespace(is_detached=False),
                           lethal=None,workers=[],orphan_message=False,message_sink=None);attrs.update(change)
                with self.assertRaises(TrialStopped):self.run_hold(backend=SimpleNamespace(**attrs))
                _,rows=self.records();self.assertFalse(any(r['phase']=='ready' for r in rows))
    def test_control_marker_blocks_a_different_output_after_shutdown(self):
        self.bridge.config['control']=str(self.output/'native-control.txt')
        self.run_hold([dict(sequence=1,action='shutdown')])
        other=self.output/'different-output';other.mkdir()
        with self.assertRaisesRegex(TrialStopped,'Unresolved'):
            assert_no_pending_handoff(other,self.bridge.config)
        self.assertTrue((self.output/'native-control.txt.handoff.json').exists())

    def test_nonresident_mode_archives_and_retains_restart_barrier(self):
        with self.assertRaisesRegex(TrialStopped,'resident-handoff'):self.run_hold(resident=False)
        with self.assertRaises(TrialStopped):assert_no_pending_handoff(self.output)
    def test_observe_is_read_only_and_consumes_nonce_sequence(self):
        self.run_hold([dict(sequence=1,action='observe'),dict(sequence=1,action='observe',duplicate=True),
                       dict(sequence=2,action='shutdown')])
        _,rows=self.records()
        self.assertEqual(len([r for r in rows if r['phase']=='observed']),1)
        self.assertEqual(self.bridge.inputs,[]);self.assertEqual(self.bridge.cancelled,0)

    def test_foreign_epoch_before_handoff_is_not_accepted(self):
        self.bridge.epoch=8
        with self.assertRaisesRegex(TrialStopped,'epoch changed'):self.run_hold()
        self.assertEqual(self.bridge.inputs,[])


if __name__=='__main__':unittest.main()
