"""Bounded checkpoint metadata reconciliation across asynchronous death saves."""
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from typing import Any

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evolution.live import Checkpoint,TrialStopped,validate_ui


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Clock:
    def __init__(self):self.now=0.;self.events=[]
    def later(self,seconds,callback):self.events.append((self.now+seconds,callback))
    def advance(self,seconds):
        self.now+=seconds
        ready=[event for event in self.events if event[0]<=self.now]
        self.events=[event for event in self.events if event[0]>self.now]
        for _,callback in ready:callback()


class RecoveryBridge:
    def __init__(self,root,clock):
        self.root=root;self.clock=clock;self.screen='game_over';self.position=[0,0,0]
        self.on_death=lambda:None;self.on_load=lambda:None;self.events=[];self.episode_epoch=None
        self.option=root/'option.sav';self.progress=root/'progress.sav'
        self.option.write_bytes(b'old option');self.progress.write_bytes(b'protected progress')
        self.config: dict[str,Any]=dict(checkpoint_state=str(root/'checkpoint-state.json'),
            reads={name:{'format':'I'} for name in ('hud','pause','dead')},
            ui={name:[dict(read=read,equals=1)] for name,read in [('gameplay','hud'),('pause','pause'),('game_over','dead')]},
            recovery=dict(files={str(p):digest(p.read_bytes()) for p in (self.option,self.progress)},
                mutable_after_death={str(self.option):40},
                death_metadata_quiet_seconds=.2,death_metadata_timeout_seconds=.8,
                steps=[dict(keys='Plus',before='gameplay',after='pause',settle=.1),
                       dict(keys='A',before='pause',after='gameplay',settle=.1)],
                death_steps=[dict(keys='A',before='game_over',after='gameplay',settle=0)]))
    def state(self):
        return dict(position=self.position,health=0 if self.screen=='game_over' else 12,
                    reads={'hud':[int(self.screen=='gameplay')],'pause':[int(self.screen=='pause')],
                           'dead':[int(self.screen=='game_over')]})
    def sample(self,keys=None,*args,**kwargs):
        if keys is not None:self.events.append(keys)
        if keys=='A' and self.screen=='game_over':self.screen='gameplay';self.on_death()
        elif keys=='Plus' and self.screen=='gameplay':self.screen='pause'
        elif keys=='A' and self.screen=='pause':self.screen='gameplay';self.on_load()
        return self.state()
    def wait_ui(self,name,*args):
        if name!=self.screen:raise TrialStopped('Expected UI state: '+name)
        return self.state()
    def settle(self,seconds):self.clock.advance(seconds)
    def rpc(self,verb):return dict(active=False)


class CheckpointQuiescenceTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.clock=Clock();self.bridge=RecoveryBridge(Path(temporary.name),self.clock)
        self.log=io.StringIO();self.checkpoint=Checkpoint(self.bridge,self.log)
        self.task=dict(start=[0,0,0],start_health=12,min_health=1,bounds=[[-1,1]]*3)
        self.addCleanup(patch.stopall)
        patch('evolution.live.time.monotonic',side_effect=lambda:self.clock.now).start()
        def pause(bridge,*args):bridge.sample('Plus');return bridge.wait_ui('pause')
        patch('evolution.live.guarded_pause',side_effect=pause).start()
        validate_ui(self.bridge.config)

    def restore(self):return self.checkpoint.restore(self.task)
    def manifest(self):return json.loads(Path(self.bridge.config['checkpoint_state']).read_text())

    def test_write_during_death_quiet_interval_restarts_stability_window(self):
        self.bridge.on_death=lambda:self.clock.later(.1,lambda:self.bridge.option.write_bytes(b'first death write'))
        before_load=[]
        self.bridge.on_load=lambda:before_load.append((self.clock.now,self.manifest()))
        self.restore()
        self.assertGreaterEqual(before_load[0][0],.3)
        self.assertEqual(before_load[0][1]['expected'][str(self.bridge.option)],digest(b'first death write'))
        self.checkpoint.verify_files()

    def test_write_after_final_load_is_reconciled_in_same_verified_death(self):
        initial_manifest=[]
        def loaded():
            initial_manifest.append(self.manifest())
            self.clock.later(.15,lambda:self.bridge.option.write_bytes(b'late death write'))
        self.bridge.on_load=loaded
        self.restore()
        path=str(self.bridge.option)
        self.assertEqual(initial_manifest[0]['expected'][path],digest(b'old option'))
        self.assertEqual(self.manifest()['expected'][path],digest(b'late death write'))
        self.assertGreaterEqual(self.clock.now,.6)
        self.assertEqual(self.bridge.progress.read_bytes(),b'protected progress')
        self.assertEqual(self.bridge.config['recovery']['files'][path],digest(b'old option'))
        self.assertEqual(len(list((self.bridge.root/'death-metadata').iterdir())),1)
        self.assertIn('death_metadata_changed',self.log.getvalue())
        Checkpoint(self.bridge,io.StringIO()).verify_files()

    def test_ordinary_load_does_not_accept_metadata_mutation(self):
        self.bridge.screen='gameplay'
        self.bridge.on_load=lambda:self.clock.later(.05,lambda:self.bridge.option.write_bytes(b'unrelated write'))
        with self.assertRaisesRegex(TrialStopped,'Checkpoint save changed'):self.restore()
        self.assertFalse(Path(self.bridge.config['checkpoint_state']).exists())

    def test_prior_death_permission_does_not_leak_to_next_restore(self):
        self.restore()
        self.bridge.on_load=lambda:self.clock.later(.05,lambda:self.bridge.option.write_bytes(b'later ordinary write'))
        with self.assertRaisesRegex(TrialStopped,'Checkpoint save changed'):self.restore()
        self.assertEqual(self.manifest()['expected'][str(self.bridge.option)],digest(b'old option'))

    def test_unverified_death_ui_cannot_authorize_metadata(self):
        self.bridge.screen='gameplay'
        original=self.bridge.sample
        first=True
        def false_death(*args,**kwargs):
            nonlocal first
            state=original(*args,**kwargs)
            if first:first=False;state['health']=0
            return state
        self.bridge.sample=false_death
        with self.assertRaisesRegex(TrialStopped,'Expected UI state: game_over'):self.restore()
        self.assertEqual(self.bridge.events,[])
        self.assertFalse(Path(self.bridge.config['checkpoint_state']).exists())

    def test_wrong_final_anchor_cannot_accept_a_late_write(self):
        def wrong_load():
            self.bridge.position=[5,0,0];self.bridge.option.write_bytes(b'late death write')
        self.bridge.on_load=wrong_load
        with self.assertRaisesRegex(TrialStopped,'does not match'):self.restore()
        self.assertEqual(self.manifest()['expected'][str(self.bridge.option)],digest(b'old option'))
        self.assertNotIn('death_metadata_changed',self.log.getvalue())

    def test_immutable_write_during_quiescence_is_rejected(self):
        self.bridge.on_death=lambda:self.clock.later(.1,lambda:self.bridge.progress.write_bytes(b'changed progress'))
        with self.assertRaisesRegex(TrialStopped,'Checkpoint save changed'):self.restore()
        self.assertFalse(Path(self.bridge.config['checkpoint_state']).exists())

    def test_oversize_late_metadata_is_rejected(self):
        self.bridge.on_load=lambda:self.clock.later(.15,lambda:self.bridge.option.write_bytes(b'x'*41))
        with self.assertRaisesRegex(TrialStopped,'Unexpected death metadata size'):self.restore()
        self.assertEqual(self.manifest()['expected'][str(self.bridge.option)],digest(b'old option'))

    def test_continuously_changing_metadata_stops_at_timeout(self):
        def changing():
            self.bridge.option.write_bytes(str(self.clock.now).encode())
            self.clock.later(.05,changing)
        self.bridge.on_death=lambda:self.clock.later(.05,changing)
        with self.assertRaisesRegex(TrialStopped,'quiescent before timeout'):self.restore()
        self.assertAlmostEqual(self.clock.now,.8)
        self.assertFalse(Path(self.bridge.config['checkpoint_state']).exists())
        self.assertEqual(self.checkpoint.expected,self.bridge.config['recovery']['files'])

    def fast_backend(self,operation=None,verified=False):
        bridge=self.bridge;bridge.episode_epoch=None;bridge.rpc=lambda verb:dict(active=False)
        class Backend:
            def __init__(self):self.calls=0;self.verified_player_checkpoint=verified
            def restore(self,task):
                self.calls+=1
                if operation is not None:operation()
        backend=Backend();self.checkpoint=Checkpoint(bridge,self.log,backend)
        return backend

    def test_fast_backend_skips_normal_menu_inputs(self):
        self.bridge.screen='gameplay'
        backend=self.fast_backend();self.restore()
        self.assertEqual(backend.calls,1);self.assertEqual(self.bridge.events,[''])

    def test_verified_player_checkpoint_uses_independent_read_without_neutral_replay(self):
        self.bridge.screen='gameplay'
        backend=self.fast_backend(verified=True);self.restore()
        self.assertEqual(backend.calls,1);self.assertEqual(self.bridge.events,[])
        self.assertIn('checkpoint_restored',self.log.getvalue())

    def test_verified_player_checkpoint_still_checks_the_independent_anchor(self):
        self.bridge.screen='gameplay'
        self.fast_backend(lambda:setattr(self.bridge,'position',[5,0,0]),verified=True)
        with self.assertRaisesRegex(TrialStopped,'does not match'):self.restore()
        self.assertEqual(self.bridge.events,[])

    def test_fast_backend_retains_death_prefix_and_late_metadata_transaction(self):
        backend=self.fast_backend(lambda:self.clock.later(.15,lambda:self.bridge.option.write_bytes(b'late fast reset write')))
        self.restore()
        self.assertEqual(backend.calls,1);self.assertEqual(self.bridge.events,['A',''])
        self.assertEqual(self.manifest()['expected'][str(self.bridge.option)],digest(b'late fast reset write'))
        Checkpoint(self.bridge,io.StringIO()).verify_files()

    def test_fast_backend_cannot_accept_an_ordinary_metadata_change(self):
        self.bridge.screen='gameplay'
        backend=self.fast_backend(lambda:self.bridge.option.write_bytes(b'unrelated write'))
        with self.assertRaisesRegex(TrialStopped,'Checkpoint save changed'):self.restore()
        self.assertEqual(backend.calls,1)
        self.assertFalse(Path(self.bridge.config['checkpoint_state']).exists())

    def test_failed_fast_backend_never_falls_back_to_menu_inputs(self):
        self.bridge.screen='gameplay'
        def fail():raise TrialStopped('reset completion uncertain')
        backend=self.fast_backend(fail)
        with self.assertRaisesRegex(TrialStopped,'completion uncertain'):self.restore()
        self.assertEqual(backend.calls,1);self.assertEqual(self.bridge.events,[])

    def test_fast_backend_still_requires_loaded_anchor(self):
        self.bridge.screen='gameplay'
        backend=self.fast_backend(lambda:setattr(self.bridge,'position',[5,0,0]))
        with self.assertRaisesRegex(TrialStopped,'does not match'):self.restore()
        self.assertEqual(backend.calls,1)

    def test_fast_backend_checks_inactive_replay_before_invocation(self):
        self.bridge.screen='gameplay';backend=self.fast_backend()
        self.bridge.rpc=lambda verb:dict(active=True)
        with self.assertRaisesRegex(TrialStopped,'inactive native replay'):self.restore()
        self.assertEqual(backend.calls,0);self.assertEqual(self.bridge.events,[])

    def test_quiescence_configuration_is_finite_and_bounded(self):
        for update in [dict(death_metadata_quiet_seconds=0),dict(death_metadata_quiet_seconds=True),
                       dict(death_metadata_quiet_seconds=float('nan')),dict(death_metadata_quiet_seconds=11),
                       dict(death_metadata_timeout_seconds=.1),dict(death_metadata_timeout_seconds=61),
                       dict(death_metadata_timeout_seconds=float('inf'))]:
            config=copy.deepcopy(self.bridge.config);config['recovery'].update(update)
            with self.subTest(update=update),self.assertRaises(ValueError):validate_ui(config)


if __name__=='__main__':unittest.main()
