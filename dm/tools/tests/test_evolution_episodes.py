"""Whole-attempt terminal evidence and reset sequencing, without a live game."""
import hashlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from typing import Any
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evolution.episodes import NativeEpisodes
from evolution.live import Checkpoint,TrialStopped,validate_ui,ui_matches
from evolution.timeline import seed_genome


class FakeBridge:
    root: Path
    def __init__(self,rows):
        self.rows=iter(rows);self.calls=0;self.cancelled=0;self.native_status={}
        self.config: dict[str,Any]=dict(recovery={'files':{}},reads={'health':{'address':'0x1000'}},ui={'gameplay':[dict(read='hud',equals=1)]})
    def sample(self,actors=False):
        self.calls+=1
        if self.calls==1:return dict(position=[0,0,0],health=10,reads={'hud':[1]})
        self.native_status,health=next(self.rows)
        return dict(position=[1,0,0],health=health,reads={'hud':[1]})
    def start_replay(self,*args):self.replay_args=args;return dict(start_frame=100,epoch=7)
    def cancel_replay(self):self.cancelled+=1


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
        bridge.cancel_replay=lambda:events.append('cancel_replay')
        spec=dict(start=[0,0,0],target=[10,0,0],horizon_frames=100,start_health=10,segments=1)
        result=NativeEpisodes(bridge,spec,io.StringIO(),Backend()).evaluate(seed_genome(1))
        self.assertEqual(result['reason'],'death');self.assertEqual(result['health'],0)
        self.assertEqual(events,[('arm',7,0x1000),'poll',('finish','death',0),'cancel_replay'])

    def test_lethal_arm_failure_still_releases_hooks_before_cancelling_replay(self):
        bridge=FakeBridge([]);events=[]
        class Backend:
            def arm_episode(self,*args):events.append('arm');raise TrialStopped('lethal arm failed')
            def finish_episode(self,status):events.append('finish')
        bridge.cancel_replay=lambda:events.append('cancel_replay')
        spec=dict(start=[0,0,0],target=[10,0,0],horizon_frames=100,start_health=10,segments=1)
        with self.assertRaisesRegex(TrialStopped,'lethal arm failed'):
            NativeEpisodes(bridge,spec,io.StringIO(),Backend()).evaluate(seed_genome(1))
        self.assertEqual(events,['arm','finish','cancel_replay'])

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

    def test_guard_errors_never_receive_fitness(self):
        for reason in ('guard_read_error','cancelled','frame_regression'):
            with self.assertRaises(TrialStopped):self.run_episode([(status(110,False,reason),10)])

    def test_death_requires_native_zero_health(self):
        with self.assertRaisesRegex(TrialStopped,'zero health'):
            self.run_episode([(status(110,False,'death',1,110),0)])

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
