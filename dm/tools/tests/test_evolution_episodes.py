"""Whole-attempt terminal evidence and reset sequencing, without a live game."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from typing import Any
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evolution.episodes import NativeEpisodes,NativeUIInterruption,validate_episode
from evolution.live import Checkpoint,TrialStopped,validate_ui,ui_matches
from evolution.timeline import seed_genome


class FakeBridge:
    root: Path
    def __init__(self,rows):
        self.rows=iter(rows);self.calls=0;self.cancelled=0;self.status_calls=0;self.native_status={}
        self.stopped: dict[str,Any]|None=None
        self.cancel_override: dict[str,Any]|None=None
        self.post_cancel: dict[str,Any]|None=None
        self.last_state: dict[str,Any]={}
        self.config: dict[str,Any]=dict(recovery={'files':{}},reads={'health':{'address':'0x1000'}},ui={'gameplay':[dict(read='hud',equals=1)]})
    def sample(self,actors=False):
        self.calls+=1
        if self.calls==1:return dict(position=[0,0,0],health=10,reads={'hud':[1]})
        if self.stopped is not None:
            self.native_status={**self.stopped,'current_frame':self.stopped['current_frame']+1,
                                'observed_frame_end':self.stopped['current_frame']+1}
            return self.post_cancel if self.post_cancel is not None else self.last_state
        row=next(self.rows)
        if isinstance(row,dict):
            self.native_status=row['before'];self.after=row['after'];health=row['health'];position=row['position']
        else:
            self.native_status,health=row;position=[1,0,0]
            self.after={**self.native_status,'current_frame':self.native_status['observed_frame_end']}
        self.last_state=dict(position=position,health=health,reads={'hud':[row.get('hud',1) if isinstance(row,dict) else 1]})
        return self.last_state
    def rpc(self,verb):
        assert verb=='replay_status'
        self.status_calls+=1;self.native_status=self.after;return self.after
    def start_replay(self,*args):self.replay_args=args;return dict(start_frame=100,end_frame=200,epoch=7)
    def cancel_replay(self):
        self.cancelled+=1
        self.stopped=(self.cancel_override if self.cancel_override is not None else
            {**self.native_status,'active':False,'epoch':7,
             **(dict(reason='cancelled',stop_frame=self.native_status.get('current_frame',100))
                if self.native_status.get('active',True) else {})})
        return self.stopped


def status(frame,active=True,reason='running',health=10,stop=0):
    return dict(current_frame=frame,observed_frame_end=frame+1,active=active,reason=reason,
                start_frame=100,end_frame=200,stop_frame=stop,last_health=health,epoch=7)


class EpisodeTests(unittest.TestCase):
    def run_episode(self,rows,address=None):
        bridge=FakeBridge(rows)
        if address is not None:bridge.config['native_guard_health_address']=address
        spec=dict(start=[0,0,0],target=[10,0,0],horizon_frames=100,start_health=10,segments=1)
        with patch('evolution.episodes.time.sleep'):
            result=NativeEpisodes(bridge,spec,io.StringIO()).evaluate(seed_genome(1))
        return result,bridge

    def test_authoritative_guard_keeps_mirror_telemetry_separate(self):
        for address,expected in [(None,0x1000),('0x2000',0x2000)]:
            _,bridge=self.run_episode([(status(200,False,'completed',10,200),10)],address)
            self.assertEqual(bridge.replay_args[1],expected)
            self.assertEqual(bridge.config['reads']['health']['address'],'0x1000')

    def test_invalid_native_guard_never_installs_timeline(self):
        for address in [True,12,'nope','0x0','0x1001','0xfffffffffffffffc']:
            with self.subTest(address=address),self.assertRaisesRegex(ValueError,'native health guard'):
                self.run_episode([],address)

    def test_lethal_hook_epoch_and_cleanup_bracket_native_episode(self):
        bridge=FakeBridge([(status(140,False,'death',0,120),10)])
        events=[]
        class Backend:
            def arm_episode(self,status,task,address):events.append(('arm',status['epoch'],address))
            def poll_episode(self):events.append('poll')
            def finish_episode(self,status):events.append(('finish',status['reason'],status['last_health']))
        original_cancel=bridge.cancel_replay
        def cancel():events.append('cancel_replay');return original_cancel()
        bridge.cancel_replay=cancel
        spec=dict(start=[0,0,0],target=[10,0,0],horizon_frames=100,start_health=10,segments=1)
        result=NativeEpisodes(bridge,spec,io.StringIO(),Backend()).evaluate(seed_genome(1))
        self.assertEqual(result['reason'],'death');self.assertEqual(result['health'],0)
        self.assertEqual(events,[('arm',7,0x1000),'poll',('finish','death',0),'cancel_replay'])

    def test_lethal_arm_failure_still_releases_hooks_before_cancelling_replay(self):
        bridge=FakeBridge([]);events=[]
        class Backend:
            def arm_episode(self,*args):events.append('arm');raise TrialStopped('lethal arm failed')
            def finish_episode(self,status):events.append('finish')
        original_cancel=bridge.cancel_replay
        def cancel():events.append('cancel_replay');return original_cancel()
        bridge.cancel_replay=cancel
        spec=dict(start=[0,0,0],target=[10,0,0],horizon_frames=100,start_health=10,segments=1)
        with self.assertRaisesRegex(TrialStopped,'lethal arm failed'):
            NativeEpisodes(bridge,spec,io.StringIO(),Backend()).evaluate(seed_genome(1))
        self.assertEqual(events,['arm','finish','cancel_replay'])

    def ui_episode(self,terminal=None,backend=None,cancel=None):
        observed=status(150) if terminal is None else status(201,False,terminal,0 if terminal=='death' else 10,200)
        bridge=FakeBridge([dict(before=observed,after={**observed,'current_frame':202 if terminal else 152},
            position=[10,0,0],health=10,hud=0)])
        if cancel is not None:bridge.cancel_replay=cancel
        spec=dict(start=[0,0,0],target=[10,0,0],horizon_frames=100,start_health=10,segments=1)
        log=io.StringIO()
        return NativeEpisodes(bridge,spec,log,backend),bridge,log

    def test_living_ui_preserves_unscored_evidence_after_verified_cleanup(self):
        events=[]
        class Backend:
            def arm_episode(self,*args):pass
            def poll_episode(self):pass
            def finish_episode(self,status):events.append(('finish',status['reason']))
        env,bridge,log=self.ui_episode(backend=Backend())
        original=bridge.cancel_replay
        def cancel():events.append(('cancel',));return original()
        bridge.cancel_replay=cancel
        with self.assertRaises(NativeUIInterruption) as caught:env.evaluate(seed_genome(1))
        evidence=caught.exception.evidence
        self.assertEqual(events,[('finish','running'),('cancel',)])
        self.assertEqual(evidence['state']['reads']['hud'],[0]);self.assertEqual(evidence['native_epoch'],7)
        self.assertIn('f0:',evidence['timeline']);self.assertNotIn('score',evidence)
        self.assertFalse(evidence['cancel_status']['active'])
        event=json.loads(log.getvalue());self.assertEqual(event['genome'],seed_genome(1))

    def test_living_ui_overlapping_horizon_is_not_scored(self):
        env,_,_=self.ui_episode('completed')
        with self.assertRaises(NativeUIInterruption):env.evaluate(seed_genome(1))

    def test_native_death_precedes_non_gameplay_healed_mirror(self):
        env,_,_=self.ui_episode('death')
        result=env.evaluate(seed_genome(1));self.assertEqual(result['reason'],'death')
        self.assertEqual(result['health'],0);self.assertFalse(result['success'])

    def test_failed_lethal_cleanup_never_enters_handoff(self):
        class Backend:
            def arm_episode(self,*args):pass
            def poll_episode(self):pass
            def finish_episode(self,status):raise TrialStopped('cleanup uncertain')
        env,bridge,log=self.ui_episode(backend=Backend())
        with self.assertRaises(TrialStopped) as caught:env.evaluate(seed_genome(1))
        self.assertNotIsInstance(caught.exception,NativeUIInterruption)
        self.assertEqual(bridge.cancelled,1);self.assertIn('episode_ui_interrupted',log.getvalue())

    def test_failed_or_wrong_cancel_ack_never_enters_handoff(self):
        def failure():raise OSError('cancel unavailable')
        for cancel in [failure,lambda:None,lambda:dict(active=True,epoch=7,reason='cancelled'),
                       lambda:dict(active=False,epoch=8,reason='cancelled'),
                       lambda:dict(active=False,epoch=7,reason='death')]:
            env,_,_=self.ui_episode(cancel=cancel)
            with self.subTest(cancel=cancel),self.assertRaises((TrialStopped,OSError)) as caught:
                env.evaluate(seed_genome(1))
            self.assertNotIsInstance(caught.exception,NativeUIInterruption)

    def test_visible_hud_at_death_is_not_gameplay(self):
        config={'ui':{'gameplay':[dict(read='hud',equals=1)]}}
        self.assertFalse(ui_matches(config,dict(health=0,reads={'hud':[1]}),'gameplay'))
        self.assertTrue(ui_matches(config,dict(health=1,reads={'hud':[1]}),'gameplay'))

    def test_damage_continues_until_exact_native_death_frame(self):
        result,bridge=self.run_episode([(status(110),9),(status(115),0),(status(140,False,'death',0,120),0)])
        self.assertEqual(result['reason'],'death');self.assertEqual(result['alive_frames'],20)
        self.assertEqual(result['damage'],10);self.assertFalse(result['censored']);self.assertEqual(bridge.calls,4)
        self.assertEqual(bridge.cancelled,1)

    def test_horizon_is_censored_not_a_death(self):
        result,_=self.run_episode([(status(210,False,'completed',6,200),0)])
        self.assertEqual(result['reason'],'horizon');self.assertEqual(result['alive_frames'],100)
        self.assertTrue(result['censored']);self.assertEqual(result['health'],6)
        self.assertEqual(result['progress_bin'],0);self.assertEqual(result['position'],(0,0,0))

    def test_healed_postdeath_position_inside_read_bracket_is_not_credited(self):
        for read_end in (120,122):
            with self.subTest(read_end=read_end):
                before={**status(119),'observed_frame_end':read_end}
                after=status(123,False,'death',0,120)
                result,bridge=self.run_episode([dict(before=before,after=after,health=10,position=[10,0,0])])
                self.assertEqual(result['alive_frames'],20);self.assertEqual(result['health'],0)
                self.assertEqual(result['progress_bin'],0);self.assertEqual(result['position'],(0,0,0))
                self.assertFalse(result['success']);self.assertEqual(bridge.status_calls,1)

    def test_death_after_active_status_in_same_frame_discards_pending_position(self):
        first={**status(119),'observed_frame_end':119}
        result,_=self.run_episode([
            dict(before=first,after=first,health=10,position=[10,0,0]),
            dict(before=status(121,False,'death',0,119),after=status(122,False,'death',0,119),health=10,position=[10,0,0])])
        self.assertEqual(result['alive_frames'],19);self.assertEqual(result['progress_bin'],0)
        self.assertFalse(result['success']);self.assertEqual(result['position'],(0,0,0))

    def test_position_strictly_before_terminal_frame_is_preserved(self):
        before={**status(118),'observed_frame_end':119}
        result,_=self.run_episode([dict(before=before,after=status(122,False,'death',0,120),health=10,position=[8,0,0])])
        self.assertEqual(result['progress_bin'],80);self.assertEqual(result['position'],(8,0,0))
        self.assertEqual(result['health'],0);self.assertEqual(result['alive_frames'],20)

    def test_pending_position_is_committed_after_later_active_frame(self):
        first={**status(110),'observed_frame_end':110}
        result,_=self.run_episode([
            dict(before=first,after=first,health=10,position=[8,0,0]),
            dict(before=status(115),after=status(117),health=9,position=[7,0,0]),
            (status(121,False,'death',0,120),10)])
        self.assertEqual(result['progress_bin'],80);self.assertEqual(result['position'],(7,0,0))
        self.assertEqual(result['damage'],10)

    def test_overlapping_terminal_sample_cannot_complete_stable_objective(self):
        prior=[dict(before={**status(frame),'observed_frame_end':frame},after=status(frame+1),health=10,position=[10,0,0])
               for frame in (110,115,118)]
        for read_end,expected_success in [(119,True),(120,False),(122,False)]:
            with self.subTest(read_end=read_end):
                final=dict(before={**status(119),'observed_frame_end':read_end},after=status(123,False,'death',0,120),health=10,position=[10,0,0])
                result,_=self.run_episode(prior+[final])
                self.assertEqual(result['success'],expected_success)
                self.assertEqual(result['progress_bin'],100 if expected_success else 99)

    def test_reconciliation_rejects_epoch_change_and_frame_regression(self):
        before=status(119)
        for after in [{**status(122),'epoch':8},status(119)]:
            with self.subTest(after=after),self.assertRaises(TrialStopped):
                self.run_episode([dict(before=before,after=after,health=10,position=[10,0,0])])

    def test_guard_errors_never_receive_fitness(self):
        for reason in ('guard_read_error','cancelled','frame_regression'):
            with self.assertRaises(TrialStopped):self.run_episode([(status(110,False,reason),10)])

    def test_death_requires_native_zero_health(self):
        with self.assertRaisesRegex(TrialStopped,'zero health'):
            self.run_episode([(status(110,False,'death',1,110),0)])


    def objective_episode(self,*,enabled: bool|None=True,rows=None,backend=None):
        if rows is None:
            rows=[dict(before=status(frame),after=status(frame+2),health=10,position=[10,0,0])
                  for frame in (110,115,120,125)]
        bridge=FakeBridge(rows);log=io.StringIO()
        spec=dict(start=[0,0,0],target=[10,0,0],horizon_frames=100,start_health=10,segments=1)
        if enabled is not None:spec['stop_on_objective']=enabled
        return NativeEpisodes(bridge,spec,log,backend),bridge,log

    def test_stop_on_objective_requires_exact_boolean(self):
        for value in (0,1,None,'true',[],{}):
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'boolean'):
                validate_episode(dict(start=[0,0,0],target=[10,0,0],horizon_frames=100,
                    start_health=10,segments=1,stop_on_objective=value))

    def test_objective_cancels_before_disarming_and_records_first_stable_frame(self):
        events=[]
        class Backend:
            def arm_episode(self,*args):events.append('arm')
            def poll_episode(self):pass
            def finish_episode(self,status):events.append(('finish',status['reason']))
        env,b,log=self.objective_episode(backend=Backend());original=b.cancel_replay
        def cancel():events.append('cancel');return original()
        b.cancel_replay=cancel
        with patch('evolution.episodes.time.sleep'):
            result=env.evaluate(seed_genome(1))
        self.assertEqual(events,['arm','cancel',('finish','cancelled')])
        self.assertEqual(b.cancelled,1);self.assertEqual(result['reason'],'objective')
        self.assertTrue(result['success']);self.assertFalse(result['censored'])
        self.assertEqual(result['objective_frame'],25);self.assertEqual(result['alive_frames'],27)
        self.assertEqual(result['terminal_status'],b.stopped)
        self.assertIsNot(result['terminal_status'],b.stopped)
        self.assertEqual(result['score'],100*101+75)
        self.assertIsInstance(result['objective_elapsed'],float)
        confirmed=[json.loads(line)for line in log.getvalue().splitlines()if json.loads(line)['event']=='objective_confirmed']
        self.assertEqual(len(confirmed),1);self.assertEqual(confirmed[0]['frame'],25)
        self.assertEqual(confirmed[0]['elapsed'],result['objective_elapsed'])

    def test_default_survival_records_arrival_but_continues_to_horizon(self):
        rows=[dict(before=status(frame),after=status(frame+2),health=10,position=[10,0,0])
              for frame in (110,115,120,125,130)]
        rows.append(dict(before=status(200,False,'completed',10,200),after=status(202,False,'completed',10,200),health=10,position=[10,0,0]))
        for enabled in (None,False):
            env,b,log=self.objective_episode(enabled=enabled,rows=rows)
            with patch('evolution.episodes.time.sleep'):result=env.evaluate(seed_genome(1))
            self.assertEqual(result['reason'],'horizon');self.assertEqual(result['alive_frames'],100)
            self.assertEqual(result['score'],100*101+100);self.assertEqual(result['objective_frame'],25)
            self.assertEqual(b.calls,7);self.assertEqual(log.getvalue().count('objective_confirmed'),1)

    def test_terminal_overlap_precedes_objective(self):
        for reason,health,stop in [('death',0,126),('completed',10,200)]:
            rows=[dict(before=status(frame),after=status(frame+2),health=10,position=[10,0,0])
                  for frame in (110,115,120)]
            rows.append(dict(before=status(125),after=status(202,False,reason,health,stop),health=10,position=[10,0,0]))
            env,b,_=self.objective_episode(rows=rows)
            with patch('evolution.episodes.time.sleep'):result=env.evaluate(seed_genome(1))
            self.assertEqual(result['reason'],'death'if reason=='death'else'horizon')
            self.assertEqual(b.calls,5)

    def test_death_and_horizon_during_cancel_are_preserved(self):
        for reason,health,stop,current in [('death',0,128,129),('completed',10,200,201),('cancelled',10,200,201)]:
            events=[]
            class Backend:
                def arm_episode(self,*args):pass
                def poll_episode(self):pass
                def finish_episode(self,status,events=events):events.append((status['reason'],status['last_health']))
            env,b,_=self.objective_episode(backend=Backend())
            b.cancel_override=status(current,False,reason,health,stop)
            with patch('evolution.episodes.time.sleep'):result=env.evaluate(seed_genome(1))
            self.assertEqual(result['reason'],'death'if reason=='death'else'horizon')
            self.assertEqual(result['health'],health);self.assertEqual(events,[(reason,health)])
            self.assertFalse(result['success']);self.assertLessEqual(result['progress_bin'],99)
            self.assertEqual(result['objective_frame'],25)
            self.assertEqual(b.cancelled,1)

    def test_objective_cancel_bad_ack_or_failed_cleanup_never_scores(self):
        base=status(128,False,'cancelled',10,127)
        for changes in [dict(active=True),dict(epoch=8),dict(start_frame=99),dict(end_frame=201),
                        dict(current_frame=126),dict(stop_frame=99),dict(stop_frame=129),
                        dict(last_health=0),dict(last_health=True),dict(reason='guard_read_error'),
                        dict(reason='death',last_health=10),dict(reason='completed'),dict(stop_frame=None)]:
            env,b,_=self.objective_episode();b.cancel_override={**base,**changes}
            with self.subTest(changes=changes),patch('evolution.episodes.time.sleep'),self.assertRaises(TrialStopped):
                env.evaluate(seed_genome(1))
        class Backend:
            def arm_episode(self,*args):pass
            def poll_episode(self):pass
            def finish_episode(self,status):raise TrialStopped('lethal cleanup failed')
        env,b,_=self.objective_episode(backend=Backend())
        with patch('evolution.episodes.time.sleep'),self.assertRaisesRegex(TrialStopped,'lethal cleanup failed'):
            env.evaluate(seed_genome(1))
        self.assertEqual(b.cancelled,1)
        env,b,_=self.objective_episode()
        def failure():raise OSError('cancel unavailable')
        b.cancel_replay=failure
        with patch('evolution.episodes.time.sleep'),self.assertRaisesRegex(OSError,'cancel unavailable'):
            env.evaluate(seed_genome(1))

    def test_ui_before_or_during_objective_cancellation_is_unscored(self):
        for after_cancel in (False,True):
            rows=[dict(before=status(frame),after=status(frame+2),health=10,position=[10,0,0],
                       hud=0 if frame==125 and not after_cancel else 1)for frame in (110,115,120,125)]
            env,b,_=self.objective_episode(rows=rows)
            if after_cancel:b.post_cancel=dict(position=[10,0,0],health=10,reads={'hud':[0]})
            with self.subTest(after_cancel=after_cancel),patch('evolution.episodes.time.sleep'),self.assertRaises(NativeUIInterruption)as caught:
                env.evaluate(seed_genome(1))
            self.assertNotIn('score',caught.exception.evidence);self.assertEqual(b.cancelled,1)

    def test_postcancel_zero_health_or_identity_change_is_not_objective(self):
        env,b,_=self.objective_episode()
        b.post_cancel=dict(position=[10,0,0],health=0,reads={'hud':[1]})
        with patch('evolution.episodes.time.sleep'),self.assertRaisesRegex(TrialStopped,'Unlatched death'):
            env.evaluate(seed_genome(1))
        env,b,_=self.objective_episode();original=b.sample
        def sample(actors=False):
            value=original(actors)
            if b.stopped is not None:b.native_status={**b.native_status,'epoch':8}
            return value
        b.sample=sample
        with patch('evolution.episodes.time.sleep'),self.assertRaisesRegex(TrialStopped,'cancellation was not verified'):
            env.evaluate(seed_genome(1))

    def test_default_native_terminal_health_keeps_uint32_contract(self):
        result,_=self.run_episode([(status(201,False,'completed',81,200),10)])
        self.assertEqual(result['health'],81);self.assertEqual(result['reason'],'horizon')

    def test_wrong_final_cancel_ack_cannot_score_survival_terminal(self):
        for changes in [dict(epoch=8),dict(active=True),dict(reason='cancelled'),dict(last_health=9)]:
            env,b,_=self.objective_episode(enabled=False,rows=[(status(201,False,'completed',10,200),10)])
            b.cancel_override={**status(202,False,'completed',10,200),**changes}
            with self.subTest(changes=changes),self.assertRaises(TrialStopped):env.evaluate(seed_genome(1))


    def test_primary_error_survives_both_cleanup_failures(self):
        primary=TrialStopped('observation failed')
        class Backend:
            def arm_episode(self,*args):pass
            def poll_episode(self):raise primary
            def finish_episode(self,status):raise RuntimeError('script destroyed')
        env,b,_=self.objective_episode(backend=Backend())
        def cancel():raise ProcessLookupError('host disappeared')
        b.cancel_replay=cancel
        with self.assertRaises(TrialStopped)as caught:env.evaluate(seed_genome(1))
        self.assertIs(caught.exception,primary)
        self.assertEqual(primary.__notes__,['lethal disarm also failed: script destroyed',
                                           'input cancellation also failed: host disappeared'])

    def test_first_cleanup_error_survives_second_without_fitness(self):
        first=TrialStopped('script destroyed')
        class Backend:
            def arm_episode(self,*args):pass
            def poll_episode(self):pass
            def finish_episode(self,status):raise first
        env,b,_=self.objective_episode(enabled=False,backend=Backend(),
            rows=[(status(201,False,'completed',10,200),10)])
        def cancel():raise ProcessLookupError('host disappeared')
        b.cancel_replay=cancel
        with self.assertRaises(TrialStopped)as caught:env.evaluate(seed_genome(1))
        self.assertIs(caught.exception,first)
        self.assertEqual(first.__notes__,['input cancellation also failed: host disappeared'])

    def test_death_metadata_is_narrow_bounded_and_audited(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);option=root/'option';progress=root/'progress'
            option.write_bytes(b'old');progress.write_bytes(b'protected')
            files={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (option,progress)}
            bridge=FakeBridge([]);bridge.root=root
            bridge.config['recovery']=dict(files=files,mutable_after_death={str(option):20})
            bridge.config['checkpoint_state']=str(root/'checkpoint-state.json')
            log=io.StringIO();checkpoint=Checkpoint(bridge,log)
            task=dict(start=[0,0,0],min_health=10,bounds=[[-1,1]]*3)
            state=dict(position=[0,0,0],health=10,reads={'hud':[1]})
            option.write_bytes(b'new')
            with self.assertRaises(TrialStopped):checkpoint.verify_files()
            checkpoint.accept_death_metadata(state,task);checkpoint.verify_files()
            self.assertIn('death_metadata_changed',log.getvalue())
            Checkpoint(bridge,io.StringIO()).verify_files()
            option.write_bytes(b'again')
            with self.assertRaises(TrialStopped):Checkpoint(bridge,io.StringIO()).verify_files()
            with self.assertRaises(TrialStopped):checkpoint.accept_death_metadata(dict(state,position=[5,0,0]),task)
            option.write_bytes(b'new')
            self.assertEqual(bridge.config['recovery']['files'],files)
            self.assertEqual(len(list((root/'death-metadata').iterdir())),1)
            progress.write_bytes(b'changed')
            with self.assertRaises(TrialStopped):checkpoint.accept_death_metadata(state,task)
            progress.write_bytes(b'protected');option.write_bytes(b'x'*21)
            with self.assertRaisesRegex(TrialStopped,'size'):checkpoint.accept_death_metadata(state,task)

    def test_death_recovery_precedes_manual_checkpoint_and_uses_frame_settle(self):
        with tempfile.TemporaryDirectory() as td:
            save=Path(td)/'save';save.write_bytes(b'original')
            config: dict[str,Any]=dict(reads={'hud':{'format':'I'},'pause':{'format':'I'},'dead':{'format':'I'}},
                ui={name:[dict(read=read,equals=1)] for name,read in [('gameplay','hud'),('pause','pause'),('game_over','dead')]},
                recovery=dict(files={str(save):hashlib.sha256(save.read_bytes()).hexdigest()},
                    steps=[dict(keys='Plus',before='gameplay',after='pause'),dict(keys='A',before='pause',after='gameplay')],
                    death_steps=[dict(keys='A',before='game_over',after='gameplay',timeout=30)]))
            validate_ui(config)
            class RecoveryBridge:
                def __init__(self):self.config=config;self.health=0;self.events=[]
                def state(self):return dict(position=[0,0,0],health=self.health,reads={'hud':[1]})
                def sample(self,keys=None,*args,**kwargs):
                    if keys is not None:self.events.append(keys);self.health=10
                    return self.state()
                def wait_ui(self,name,timeout=3):self.events.append((name,timeout));return self.state()
                def settle(self,seconds):self.events.append(('settle',seconds))
            bridge=RecoveryBridge()
            with patch('evolution.live.guarded_pause',return_value={}):
                Checkpoint(bridge,io.StringIO()).restore(dict(start=[0,0,0],min_health=10,bounds=[[-1,1]]*3))
            self.assertEqual(bridge.events[0],('game_over',30));self.assertEqual(bridge.events[1],'A')
            self.assertIn(('settle',.3),bridge.events);self.assertEqual(save.read_bytes(),b'original')
            config['recovery']['death_steps'][0]['before']='gameplay'
            with self.assertRaises(ValueError):validate_ui(config)


if __name__=='__main__':unittest.main()
