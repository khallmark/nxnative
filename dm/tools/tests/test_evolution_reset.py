"""Optional Frida reset identity, bounded RPC, and owned instrumentation lifecycle."""
import copy
import hashlib
import io
import importlib.util
import json
from pathlib import Path
import sys
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from typing import Any
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evolution.live import TrialStopped
from evolution.reset import FridaReset,prepare_fast_reset


class FakeCancellable:
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def cancel(self):pass


class FakeScript:
    def __init__(self,owner):
        self.owner=owner;self.exports_sync=self;self.callback: Any=None;self.nonce=None
    def on(self,event,callback):self.callback=callback
    def load(self):self.owner.events.append('load')
    def unload(self):self.owner.events.append('unload')
    def emit(self,event,**fields):
        self.callback(dict(type='send',payload=dict(nonce=self.nonce,event=event,**fields)),None)
    def arm(self,config,nonce):
        self.owner.events.append('arm');self.nonce=nonce;self.owner.configs.append(config)
        self.owner.arm(self)
    def cancel(self):
        self.owner.events.append('cancel');return self.owner.cancel(self)
    def snapshot(self):
        return dict(active=True,nonce=self.nonce,diagnostics=dict(state_after=5,warp_returned=True))


class FakeLethalScript(FakeScript):
    def load(self):self.owner.events.append('load_lethal')
    def unload(self):self.owner.events.append('unload_lethal')
    def prepare(self,config):
        self.config=config;self.owner.events.append('prepare_lethal')
        return dict(prepared=True,hooks_installed=False,health_address=config['address_binding']['health'])
    def arm(self,config,nonce):
        self.config=config;self.nonce=nonce;self.owner.events.append('arm_lethal')
        return self.owner.lethal_arm(self)
    def cancel(self):
        self.owner.events.append('cancel_lethal');return self.owner.lethal_cancel(self)
    def status(self):
        self.owner.events.append('status_lethal');return self.owner.lethal_status(self)
    def terminal(self,event='recovered'):
        return dict(event=event,terminal=True,nonce=self.nonce,descriptor_restored=True,
            native_epoch=self.config['native_epoch'],player=self.config['address_binding']['player'],
            health_address=self.config['address_binding']['health'],checkpoint_health=self.config['checkpoint_health'],
            old_health=1,terminal_health=0,callback_frame='120',thread_id=321,reset_requested=event=='recovered')


class FakeSession:
    def __init__(self,owner):self.owner=owner
    def create_script(self,source):
        self.owner.events.append('create');self.owner.sources.append(source)
        script=(FakeLethalScript if source.startswith('// lethal') else FakeScript)(self.owner)
        self.owner.scripts.append(script);return script
    def detach(self):self.owner.events.append('detach')


class FakeFrida:
    __version__='17.23.2'
    Cancellable=FakeCancellable
    def __init__(self):
        self.events=[];self.sources=[];self.scripts=[];self.configs=[]
        self.arm=lambda script:script.emit('restored',evidence={'engine_returned':True})
        self.cancel=lambda script:dict(descriptor_restored=True)
        self.lethal_arm: Any=lambda script:dict(armed=True,nonce=script.nonce,native_epoch=script.config['native_epoch'],health_address=script.config['address_binding']['health'])
        self.lethal_status: Any=lambda script:dict(active=True,native_epoch=script.config['native_epoch'],result=None)
        self.lethal_cancel: Any=lambda script:dict(descriptor_restored=True,native_epoch=script.config['native_epoch'],result=script.terminal('cancelled'))
    def attach(self,pid):self.events.append('attach');return FakeSession(self)


class FakeBridge:
    def __init__(self):
        self.config=dict(pid=123,ui={'gameplay':[dict(read='hud',equals=1)]})
        self.episode_epoch=None;self.active=False;self.health=12;self.hud=1
    def rpc(self,verb):return dict(active=self.active)
    def sample(self,**kwargs):return dict(health=self.health,reads={'hud':[self.hud]})


class ResetTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.path=Path(temporary.name)/'reset.js';self.path.write_text('// reviewed idle script\n')
        self.config: dict[str,Any]=dict(pid=123,fast_reset=dict(script=str(self.path),
            sha256=hashlib.sha256(self.path.read_bytes()).hexdigest(),build_id='a'*32,
            pid=123,mainbase='0x1000',address_binding={'player':'0x2000'},timeout_seconds=.15))
        self.frida=FakeFrida();self.bridge=FakeBridge();self.log=io.StringIO()
        self.task=dict(start=[1,2,3],start_health=12,min_health=1,start_tolerance=.2)
        importer=patch('evolution.reset.import_module',return_value=self.frida)
        importer.start();self.addCleanup(importer.stop)
        prepared=prepare_fast_reset(self.config);assert prepared is not None
        self.prepared=prepared
        self.backend=FridaReset(self.prepared,self.bridge,self.log)

    def test_disabled_backend_never_imports_frida(self):
        with patch('evolution.reset.import_module') as importer:
            self.assertIsNone(prepare_fast_reset({'pid':123}));importer.assert_not_called()

    def test_configuration_rejects_unbound_or_unbounded_targets(self):
        for update in [dict(script='relative.js'),dict(sha256='a'*63),dict(build_id='a'*64),
                       dict(pid=124),dict(mainbase='0'),dict(mainbase='0x1001'),dict(address_binding={}),
                       dict(timeout_seconds=True),dict(timeout_seconds=61),dict(timeout_seconds=float('inf'))]:
            config=dict(self.config,fast_reset={**self.config['fast_reset'],**update})
            with self.subTest(update=update),self.assertRaises(ValueError):prepare_fast_reset(config)

    def test_script_digest_is_checked_before_import(self):
        self.path.write_text('// changed\n')
        with patch('evolution.reset.import_module') as importer:
            with self.assertRaisesRegex(ValueError,'SHA256'):prepare_fast_reset(self.config)
            importer.assert_not_called()

    def test_missing_optional_binding_is_actionable(self):
        with patch('evolution.reset.import_module',side_effect=ImportError):
            with self.assertRaisesRegex(ValueError,'optional Python frida'):prepare_fast_reset(self.config)

    def test_fingerprint_and_executed_source_are_frozen(self):
        original=self.prepared.fingerprint();source=self.path.read_text()
        self.path.write_text('// changed after preparation\n')
        self.backend.restore(self.task);self.backend.close()
        self.assertEqual(self.frida.sources,[source]);self.assertEqual(original,self.prepared.fingerprint())
        self.config['fast_reset']['sha256']=hashlib.sha256(self.path.read_bytes()).hexdigest()
        updated=prepare_fast_reset(self.config);assert updated is not None
        self.assertNotEqual(original,updated.fingerprint())
        self.frida.__version__='different';versioned=prepare_fast_reset(self.config);assert versioned is not None
        self.assertNotEqual(updated.fingerprint(),versioned.fingerprint())

    def test_attach_once_with_ephemeral_scripts_and_objective_bound_arm(self):
        self.backend.restore(self.task);self.backend.restore(self.task);self.backend.close()
        self.assertEqual(self.frida.events,['attach','create','load','arm','cancel','unload',
                                            'create','load','arm','cancel','unload','detach'])
        self.assertNotEqual(self.frida.scripts[0].nonce,self.frida.scripts[1].nonce)
        self.assertEqual(self.frida.configs[0]['start'],[1,2,3])
        self.assertEqual(self.frida.configs[0]['start_health'],12)
        self.assertEqual(self.frida.configs[0]['start_tolerance'],.2)
        self.assertFalse(self.backend.uncertain)
        self.assertEqual(json.loads(self.log.getvalue().splitlines()[0])['event'],'fast_reset')

    def test_native_replay_or_wrong_ui_stops_before_attachment(self):
        for active,epoch,health,hud in [(True,None,12,1),(False,7,12,1),(False,None,0,1),(False,None,12,0)]:
            self.bridge.active=active;self.bridge.episode_epoch=epoch;self.bridge.health=health;self.bridge.hud=hud
            with self.subTest(active=active,epoch=epoch,health=health,hud=hud),self.assertRaises(TrialStopped):
                self.backend.restore(self.task)
        self.assertEqual(self.frida.events,[])

    def test_changed_session_pid_stops_before_attachment(self):
        self.bridge.config['pid']=124
        with self.assertRaisesRegex(TrialStopped,'PID changed'):self.backend.restore(self.task)
        self.assertEqual(self.frida.events,[])

    def test_phase_messages_and_pending_cleanup_are_audited(self):
        def arm(script):script.emit('load_started');script.emit('restored',evidence={'engine_returned':True})
        replies=iter([dict(descriptor_restored=False),dict(descriptor_restored=True)])
        self.frida.arm=arm;self.frida.cancel=lambda script:next(replies)
        self.backend.restore(self.task);self.backend.close()
        self.assertEqual(self.frida.events.count('cancel'),2)
        self.assertEqual([json.loads(line)['payload']['event'] for line in self.log.getvalue().splitlines()],['load_started','restored'])

    def test_failed_terminal_is_not_reusable_even_after_cleanup(self):
        self.frida.arm=lambda script:script.emit('failed',error='engine did not return')
        with self.assertRaisesRegex(TrialStopped,'engine did not return'):self.backend.restore(self.task)
        self.assertTrue(self.backend.uncertain);self.assertIn('unload',self.frida.events)
        with self.assertRaisesRegex(TrialStopped,'no longer available'):self.backend.restore(self.task)
        self.backend.close()

    def test_primary_failure_and_packet_survive_cleanup_failure(self):
        self.frida.arm=lambda script:script.emit('failed',error='readback never stable')
        def fail_cancel(script):
            script.emit('late_phase',state=5)
            raise RuntimeError('cancel transport failed')
        self.frida.cancel=fail_cancel
        with self.assertRaisesRegex(TrialStopped,'readback never stable') as caught:
            self.backend.restore(self.task)
        self.assertIn('cancel transport failed',str(caught.exception.__notes__))
        records=[json.loads(line) for line in self.log.getvalue().splitlines()]
        diagnostic=[r for r in records if r['event']=='fast_reset_diagnostic']
        snapshot=next(r for r in diagnostic if r['phase']=='before_cleanup_snapshot')
        self.assertTrue(snapshot['snapshot']['diagnostics']['warp_returned'])
        self.assertTrue(any(r['phase']=='cleanup_error' for r in diagnostic))
        self.assertTrue(any(r['phase']=='cleanup_message' and r['message']['payload']['event']=='late_phase' for r in diagnostic))
        self.assertIsNone(self.backend.message_sink)
        self.frida.cancel=lambda script:dict(descriptor_restored=True)
        self.backend.close()

    def test_snapshot_failure_does_not_replace_original_failure(self):
        self.frida.arm=lambda script:script.emit('failed',error='original native error')
        with patch.object(FakeScript,'snapshot',side_effect=RuntimeError('snapshot transport failed')):
            with self.assertRaisesRegex(TrialStopped,'original native error'):
                self.backend.restore(self.task)
        self.assertIn('snapshot_error',self.log.getvalue())
        self.backend.close()

    def test_wrong_nonce_cannot_claim_completion(self):
        def arm(script):script.nonce='other';script.emit('restored',evidence={'engine_returned':True})
        self.frida.arm=arm
        with self.assertRaisesRegex(TrialStopped,'identity'):self.backend.restore(self.task)
        self.backend.close()

    def test_missing_evidence_cannot_claim_completion(self):
        self.frida.arm=lambda script:script.emit('restored',evidence={})
        with self.assertRaisesRegex(TrialStopped,'completion evidence'):self.backend.restore(self.task)
        self.backend.close()

    def test_script_errors_are_terminal_and_cleaned(self):
        self.frida.arm=lambda script:script.callback(dict(type='error',description='bad pointer'),None)
        with self.assertRaisesRegex(TrialStopped,'script error'):self.backend.restore(self.task)
        self.backend.close();self.assertIn('unload',self.frida.events)

    def test_no_terminal_message_times_out_and_cancels(self):
        self.frida.arm=lambda script:None
        started=time.monotonic()
        with self.assertRaisesRegex(TrialStopped,'before timeout'):self.backend.restore(self.task)
        self.assertLess(time.monotonic()-started,1.)
        self.backend.close();self.assertTrue(self.backend.uncertain)

    def test_hung_arm_rpc_is_bounded_and_cleanup_releases_worker(self):
        release=threading.Event();self.addCleanup(release.set)
        self.frida.arm=lambda script:release.wait(2.)
        def cancel(script):release.set();return dict(descriptor_restored=True)
        self.frida.cancel=cancel
        started=time.monotonic()
        with self.assertRaisesRegex(TrialStopped,'arm timed out'):self.backend.restore(self.task)
        self.assertLess(time.monotonic()-started,1.)
        self.backend.close();self.assertEqual(self.backend.workers,[])

    def test_unacknowledged_cleanup_never_unloads_or_detaches(self):
        self.frida.cancel=lambda script:dict(descriptor_restored=False)
        with self.assertRaisesRegex(TrialStopped,'cleanup'):self.backend.restore(self.task)
        with self.assertRaisesRegex(TrialStopped,'cleanup'):self.backend.close()
        self.assertNotIn('unload',self.frida.events);self.assertNotIn('detach',self.frida.events)
        self.assertTrue(self.backend.uncertain)
        self.frida.cancel=lambda script:dict(descriptor_restored=True)
        self.backend.close();self.assertEqual(self.frida.events[-2:],['unload','detach'])

    def test_runner_suppresses_ui_cleanup_and_closes_logs_after_uncertain_reset(self):
        root=self.path.parent
        config={**self.config,'recovery':{'death_steps':[{}]},
                'ui':{'pause':[dict(read='hud',equals=0)],'gameplay':[dict(read='hud',equals=1)]}}
        session=root/'session.json';session.write_text(json.dumps(config))
        objective=root/'objective.json';objective.write_text(json.dumps(dict(start=[0,0,0],target=[1,0,0],
            horizon_frames=60,segments=1,start_health=12)))
        spec=importlib.util.spec_from_file_location('reset_test_runner',Path(__file__).resolve().parents[3]/'scripts'/'evolve_replay.py')
        assert spec is not None and spec.loader is not None
        runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
        events=[];logs=[]
        bridge=SimpleNamespace(config=config,last={},running=False,
            rpc=lambda verb:dict(active=False),sample=lambda **kwargs:dict(health=12,reads={'hud':[1]}),
            close=lambda:events.append(('bridge_closed',bridge.running)))
        backend=SimpleNamespace(uncertain=True,close=lambda:events.append('backend_closed'))
        evolution=SimpleNamespace(db=SimpleNamespace(close=lambda:events.append('db_closed')))
        def fail():raise TrialStopped('uncertain engine reload')
        def episodes(bridge,spec,log,backend):
            logs.append(log)
            return SimpleNamespace(checkpoint=SimpleNamespace(verify_files=lambda:None),restore=fail)
        with patch.object(runner,'ReplayBridge',return_value=bridge),patch.object(runner,'FridaReset',return_value=backend),\
             patch.object(runner,'Evolution',return_value=evolution),patch.object(runner,'NativeEpisodes',side_effect=episodes),\
             patch.object(runner.signal,'signal'),patch.object(sys,'argv',['evolve_replay.py','--session',str(session),
                '--objective',str(objective),'--output',str(root/'output')]):
            with self.assertRaisesRegex(TrialStopped,'uncertain engine reload'):runner.main()
        self.assertEqual(events,['backend_closed',('bridge_closed',False),'db_closed'])
        self.assertTrue(logs[0].closed)
        self.assertIn('uncertain engine reload',(root/'output'/'interruptions.jsonl').read_text())

    def test_message_flood_is_bounded(self):
        def arm(script):
            for _ in range(257):script.emit('phase')
        self.frida.arm=arm
        with self.assertRaisesRegex(TrialStopped,'message budget'):self.backend.restore(self.task)
        self.backend.close();self.assertIn('message budget exceeded',self.log.getvalue())


    def enable_player_checkpoint(self):
        config=self.config['fast_reset']
        config.update(mode='player_checkpoint',target_rotation=[1.,0.,0.,0.,1.,0.,0.,0.,1.],stable_frames=3)
        config['address_binding'].update(hud_flags='0x3000',plus_flags='0x4000')
        prepared=prepare_fast_reset(self.config);assert prepared is not None
        self.backend=FridaReset(prepared,self.bridge,self.log)

    def checkpoint_evidence(self):
        return dict(semantics='player_checkpoint',error_code=0,health_written=True,warp_called=True,
            stable_frames=3,first_stable_frame='100',last_stable_frame='102',authoritative_health=12,
            mirror_health=12,position_after=[1,2,3],rotation_after=[1.,0.,0.,0.,1.,0.,0.,0.,1.],
            main_instance='0x5555',pid=123,player='0x2000',
            dryrun=dict(stack_delta='1232',error_code=0,main_instance='0x5555'))

    def test_player_checkpoint_requires_distinct_frame_proof_and_records_semantics(self):
        self.enable_player_checkpoint()
        self.frida.arm=lambda script:script.emit('restored',terminal=True,descriptor_restored=True,
            semantics='player_checkpoint',evidence=self.checkpoint_evidence())
        result=self.backend.restore(self.task);self.backend.close()
        self.assertEqual(result['semantics'],'player_checkpoint')
        self.assertTrue(self.backend.verified_player_checkpoint)
        self.assertEqual(self.frida.configs[0]['mode'],'player_checkpoint')
        self.assertEqual(self.frida.configs[0]['start_health'],12)

    def test_player_checkpoint_reuses_loaded_script_but_cancels_each_arm(self):
        self.enable_player_checkpoint();nonces=[]
        def arm(script):
            nonces.append(script.nonce)
            script.emit('restored',terminal=True,descriptor_restored=True,
                semantics='player_checkpoint',evidence=self.checkpoint_evidence())
        self.frida.arm=arm
        self.backend.restore(self.task);self.backend.restore(self.task)
        self.assertEqual(self.frida.events,['attach','create','load','arm','cancel','arm','cancel'])
        self.assertNotEqual(nonces[0],nonces[1]);self.assertEqual(len(self.frida.scripts),1)
        self.backend.close();self.assertEqual(self.frida.events[-2:],['unload','detach'])

    def test_resident_player_checkpoint_rejects_old_nonce_on_next_arm(self):
        self.enable_player_checkpoint();previous=[]
        def arm(script):
            if previous:script.nonce=previous[0]
            else:previous.append(script.nonce)
            script.emit('restored',terminal=True,descriptor_restored=True,
                semantics='player_checkpoint',evidence=self.checkpoint_evidence())
        self.frida.arm=arm;self.backend.restore(self.task)
        with self.assertRaisesRegex(TrialStopped,'identity'):self.backend.restore(self.task)
        self.assertTrue(self.backend.failed);self.assertEqual(self.frida.events.count('unload'),1)
        self.backend.close()

    def test_resident_player_checkpoint_stops_on_unsolicited_idle_message(self):
        self.enable_player_checkpoint()
        self.frida.arm=lambda script:script.emit('restored',terminal=True,descriptor_restored=True,
            semantics='player_checkpoint',evidence=self.checkpoint_evidence())
        self.backend.restore(self.task);self.frida.scripts[0].emit('unexpected_idle')
        with self.assertRaisesRegex(TrialStopped,'while idle'):self.backend.restore(self.task)
        self.assertEqual(self.frida.events.count('arm'),1);self.backend.close()

    def enable_lethal(self):
        self.enable_player_checkpoint()
        path=self.path.with_name('lethal.js');path.write_text('// lethal idle script')
        self.config['native_guard_health_address']='0x6000'
        self.config['fast_reset'].update(lethal_script=str(path),lethal_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            lethal_address_binding=dict(player='0x2000',life='0x5000',health='0x6000',max_health_address='0x6004'),max_health=12)
        prepared=prepare_fast_reset(self.config);assert prepared is not None
        self.backend=FridaReset(prepared,self.bridge,self.log)
        self.frida.arm=lambda script:script.emit('restored',terminal=True,descriptor_restored=True,
            semantics='player_checkpoint',evidence=self.checkpoint_evidence())
        return path

    def test_lethal_scripts_are_frozen_and_fingerprinted_before_attachment(self):
        path=self.enable_lethal();original=self.backend.prepared.fingerprint()
        path.write_text('// lethal changed')
        with self.assertRaisesRegex(ValueError,'Lethal script'):prepare_fast_reset(self.config)
        self.config['fast_reset']['lethal_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        changed=prepare_fast_reset(self.config);assert changed is not None
        self.assertNotEqual(original,changed.fingerprint())
        self.backend.restore(self.task)
        self.assertIn('// lethal idle script',self.frida.sources);self.backend.close()

    def test_lethal_config_requires_authoritative_guard_and_checkpoint_mode(self):
        self.enable_lethal()
        for update in [dict(native_guard_health_address='0x6004'),dict(native_guard_health_address='invalid')]:
            with self.subTest(update=update),self.assertRaisesRegex(ValueError,'authoritative'):
                prepare_fast_reset({**self.config,**update})
        config=copy.deepcopy(self.config);config['fast_reset']['max_health']=0
        with self.assertRaises(ValueError):prepare_fast_reset(config)
        self.assertEqual(self.frida.events,[])

    def test_lethal_prepares_before_replay_and_reuses_resident_script(self):
        self.enable_lethal();self.backend.restore(self.task)
        self.assertIn('prepare_lethal',self.frida.events);self.assertNotIn('arm_lethal',self.frida.events)
        for epoch in (7,8):
            self.backend.arm_episode({'epoch':epoch},self.task,0x6000)
            self.backend.finish_episode(dict(epoch=epoch,reason='completed',last_health=12))
        self.assertEqual(self.frida.events.count('create'),2)
        self.assertEqual(self.frida.events.count('load_lethal'),1)
        self.assertEqual(self.frida.events.count('cancel_lethal'),2)
        self.assertNotIn('unload_lethal',self.frida.events)
        self.backend.close();self.assertEqual(self.frida.events[-3:],['unload_lethal','unload','detach'])

    def test_lethal_recovery_keeps_zero_terminal_health_despite_healing(self):
        self.enable_lethal();self.backend.restore(self.task)
        self.backend.arm_episode({'epoch':7},self.task,0x6000)
        script=self.frida.scripts[-1];assert isinstance(script,FakeLethalScript)
        recovered=script.terminal();script.callback(dict(type='send',payload=recovered),None)
        self.frida.lethal_cancel=lambda script:dict(descriptor_restored=True,native_epoch='7',result=recovered)
        self.backend.poll_episode();self.backend.finish_episode(dict(epoch=7,reason='death',last_health=0))
        self.assertFalse(self.backend.uncertain);self.assertNotIn('status_lethal',self.frida.events);self.backend.close()
        self.assertIn('"terminal_health": 0',self.log.getvalue())

    def test_hid_death_waits_for_post_cas_recovery_before_cancelling(self):
        self.enable_lethal();self.backend.restore(self.task)
        self.backend.arm_episode({'epoch':7},self.task,0x6000)
        script=self.frida.scripts[-1];assert isinstance(script,FakeLethalScript)
        replies=iter([dict(active=True,native_epoch='7',state=1,result=None),
            dict(active=False,native_epoch='7',state=3,result=script.terminal())])
        def observed(script):
            self.assertNotIn('cancel_lethal',self.frida.events)
            return next(replies)
        self.frida.lethal_status=observed
        self.frida.lethal_cancel=lambda script:dict(descriptor_restored=True,native_epoch='7',result=script.terminal())
        self.backend.finish_episode(dict(epoch=7,reason='death',last_health=0))
        self.assertEqual(self.frida.events[-3:],['status_lethal','status_lethal','cancel_lethal'])
        self.assertFalse(self.backend.uncertain);self.backend.close()

    def test_lethal_mismatched_epoch_and_unproven_death_fail_closed(self):
        self.enable_lethal();self.backend.restore(self.task)
        self.backend.arm_episode({'epoch':7},self.task,0x6000)
        with self.assertRaisesRegex(TrialStopped,'matching lethal recovery'):
            self.backend.finish_episode(dict(epoch=7,reason='death',last_health=0))
        self.assertTrue(self.backend.failed);self.assertTrue(self.backend.uncertain);self.backend.close()

    def test_lethal_message_wrong_epoch_cannot_claim_a_death(self):
        self.enable_lethal();self.backend.restore(self.task)
        self.backend.arm_episode({'epoch':7},self.task,0x6000)
        script=self.frida.scripts[-1];assert isinstance(script,FakeLethalScript)
        payload={**script.terminal(),'native_epoch':'8'}
        script.callback(dict(type='send',payload=payload),None)
        with self.assertRaisesRegex(TrialStopped,'identity'):self.backend.poll_episode()
        self.assertTrue(self.backend.uncertain);self.backend.close()

    def test_unacknowledged_lethal_cleanup_does_not_unload_or_detach(self):
        self.enable_lethal();self.backend.restore(self.task)
        self.backend.arm_episode({'epoch':7},self.task,0x6000)
        self.frida.lethal_cancel=lambda script:dict(descriptor_restored=False,native_epoch='7')
        with self.assertRaisesRegex(TrialStopped,'Lethal cleanup'):
            self.backend.finish_episode(dict(epoch=7,reason='completed',last_health=12))
        self.assertNotIn('unload_lethal',self.frida.events);self.assertNotIn('detach',self.frida.events)
        self.frida.lethal_cancel=lambda script:dict(descriptor_restored=True,native_epoch='7',result=script.terminal('cancelled'))
        self.backend.close()

    def test_lethal_arm_rejects_address_or_epoch_before_dispatch(self):
        self.enable_lethal();self.backend.restore(self.task)
        with self.assertRaisesRegex(TrialStopped,'epoch/address'):
            self.backend.arm_episode({'epoch':7},self.task,0x6004)
        self.assertNotIn('arm_lethal',self.frida.events);self.backend.close()

    def test_player_checkpoint_rejects_incomplete_or_inconsistent_completion(self):
        updates=[dict(last_stable_frame='100'),dict(stable_frames=1),dict(authoritative_health=0),
                 dict(mirror_health=11),dict(health_written=False),dict(warp_called=False),
                 dict(error_code=9),dict(position_after=[4,2,3]),dict(rotation_after=[0.]*9),
                 dict(dryrun={}),dict(pid=124),dict(player='0x2008'),dict(first_stable_frame='nan')]
        for update in updates:
            with self.subTest(update=update):
                self.enable_player_checkpoint()
                evidence={**self.checkpoint_evidence(),**update}
                self.frida.arm=lambda script,evidence=evidence:script.emit('restored',terminal=True,descriptor_restored=True,
                    semantics='player_checkpoint',evidence=evidence)
                with self.assertRaisesRegex(TrialStopped,'distinct-frame'):self.backend.restore(self.task)
                self.assertFalse(self.backend.verified_player_checkpoint);self.backend.close()

    def test_player_checkpoint_warp_return_alone_is_not_completion(self):
        self.enable_player_checkpoint()
        self.frida.arm=lambda script:script.emit('warped',terminal=True,evidence=self.checkpoint_evidence())
        with self.assertRaisesRegex(TrialStopped,'before timeout'):self.backend.restore(self.task)
        self.assertFalse(self.backend.verified_player_checkpoint);self.backend.close()

    def test_player_checkpoint_configuration_is_checked_before_attachment(self):
        self.enable_player_checkpoint()
        updates=[dict(mode='warp'),dict(target_rotation=[1.]*8),dict(target_rotation=[float('nan')]*9),
                 dict(stable_frames=1),dict(stable_frames=True),dict(rotation_tolerance=1.),
                 dict(address_binding={'player':'0x2000'})]
        for update in updates:
            config=copy.deepcopy(self.config);config['fast_reset'].update(update)
            with self.subTest(update=update),self.assertRaises(ValueError):prepare_fast_reset(config)
        self.assertEqual(self.frida.events,[])


HEADER = r'''
#include <stdint.h>
#include <stddef.h>
typedef int gint; typedef unsigned int guint; typedef uint32_t guint32;
typedef uint64_t guint64; typedef uint8_t guint8; typedef size_t gsize;
typedef void *gpointer; typedef const void *gconstpointer;
typedef struct { uint64_t sp; uint64_t x[29]; } GumCpuContext;
typedef struct { void *function; GumCpuContext *cpu_context; unsigned claimed; } GumInvocationContext;
void *gum_invocation_context_get_listener_invocation_data(GumInvocationContext *, size_t);
unsigned int gum_invocation_context_get_thread_id(GumInvocationContext *);
void *gum_invocation_context_get_nth_argument(GumInvocationContext *, unsigned int);
void *gum_invocation_context_get_return_address(GumInvocationContext *);
'''

HARNESS = r'''
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
BridgePacket packet;
gsize g_key18=0, g_keygate=1, g_keytls=2, g_keytlr=3;
guint8 dm_gate_stubs[1024*16];
guint64 dm_gate_top[1024];
static void *slots[4];
static guint64 current_frame;
static int warp_count, race_mirror, change_tls;
static int readonly_health;
static int action_count, action_failure, action_bad_selection, action_bad_tls, action_cancel, warp_cancel;
static gpointer expected_warp_x18;
guint32 task_self_port=123;
gint region_info(guint32 task, guint64 *address, guint64 *size,
                 gint flavor, gint *info, guint32 *count, guint32 *object) {
  assert(task==123 && flavor==9 && *count>=9);
  *address &= ~4095ULL; *size=4096; *count=9;
  info[0]=readonly_health ? 1 : 3; *object=456; return 0;
}
gint port_deallocate(guint32 task, guint32 object) {
  assert(task==123 && object==456); return 0;
}
static guint8 player_data[0x400], life_data[0x1cb0], table_data[88*8];
static guint64 hp_data[2], max_data[2], hud_data[64], plus_data[64];
static guint8 bsa_data[0x1c8], action_data[0x80];
void *gum_invocation_context_get_listener_invocation_data(GumInvocationContext *ic, size_t n) {
  (void)n; return &ic->claimed;
}
unsigned int gum_invocation_context_get_thread_id(GumInvocationContext *ic) { (void)ic; return 321; }
void *gum_invocation_context_get_nth_argument(GumInvocationContext *ic, unsigned int n) {
  (void)n; return ic->function;
}
void *gum_invocation_context_get_return_address(GumInvocationContext *ic) { (void)ic; return (void *)0x1234; }
guint64 frame_now(void) { return current_frame; }
gpointer test_getspecific(gsize key) { return slots[key]; }
gint test_setspecific(gsize key, gconstpointer value) { slots[key]=(void *)value; return 0; }
unsigned char native_cas(gint old, gint value, volatile gint *address) {
  return __sync_bool_compare_and_swap(address,old,value);
}
gint action_guest(gpointer bsa, const void *request, guint32 flags) {
  const guint32 *words=request;
  const guint64 *wide=request;
  guint i;
  assert(bsa==bsa_data && flags==1 && packet.diagnostic_phase==17);
  for(i=0;i!=24;++i) {
    guint32 expected=0;
    if(i==0 || i==3 || i==6 || i==11 || i==12) expected=0x7fc00000;
    if(i==9) expected=4;
    if(i==10) expected=0x11a;
    if(i==13) expected=0x10;
    if(i==19) expected=40;
    assert(words[i]==expected);
  }
  assert(wide[11]==0);
  ++action_count;
  *(guint32 *)(action_data+0x4c)=40;
  *(guint32 *)(action_data+0x28)=action_bad_selection ? 41 : 0x11a;
  slots[g_key18]=(void *)0xface;
  if(action_bad_tls) slots[g_keytls]=(void *)0xbad;
  if(action_cancel) { cancel_pending(); assert(packet.state==6); }
  return action_failure;
}
void warp_guest(gpointer actor, const float *matrix, guint32 flags) {
  guint i;
  assert(actor==player_data && flags==0);
  assert(packet.diagnostic_phase==5 && packet.warp_called && !packet.warp_returned);
  if(packet.action_enabled) {
    assert(action_count==1 && packet.action_context_restored && slots[g_key18]==expected_warp_x18);
    assert(current_frame>packet.action_frame && packet.state==6);
  }
  ++warp_count;
  for(i=0;i!=3;++i) ((float *)(player_data+0x31c))[i]=matrix[i*4+3];
  for(i=0;i!=9;++i) ((float *)(player_data+0x328))[i]=matrix[(i/3)*4+i%3];
  slots[g_key18]=(void *)0xbeef;
  if(race_mirror) *(guint32 *)(life_data+0x1ca8)=3;
  if(change_tls) slots[g_keytls]=(void *)0xbad;
  if(warp_cancel) { cancel_pending(); assert(packet.state==6); }
}
static void initialize(void) {
  memset(&packet,0,sizeof(packet)); memset(player_data,0,sizeof(player_data));
  memset(life_data,0,sizeof(life_data)); memset(table_data,0,sizeof(table_data));
  memset(hud_data,0,sizeof(hud_data)); memset(plus_data,0,sizeof(plus_data));
  memset(bsa_data,0,sizeof(bsa_data)); memset(action_data,0,sizeof(action_data));
  packet.player=(guint64)(gsize)player_data; packet.player_vtable=0x123456;
  packet.components=(guint64)(gsize)table_data; packet.life=(guint64)(gsize)life_data;
  packet.hp_record=(guint64)(gsize)hp_data; packet.max_record=(guint64)(gsize)max_data;
  packet.life_vtable=0x456789;
  *(guint64 *)player_data=packet.player_vtable;
  *(guint64 *)(player_data+0x168)=packet.player;
  *(guint64 *)(player_data+0x270)=packet.components;
  *(guint64 *)(table_data+87*8)=packet.life;
  packet.action_bsa=(guint64)(gsize)bsa_data; packet.action_bsa_vtable=0x888;
  packet.action_slot0=(guint64)(gsize)action_data;
  *(guint64 *)(table_data+0x188)=packet.action_bsa;
  *(guint64 *)bsa_data=packet.action_bsa_vtable;
  *(guint64 *)(bsa_data+0x18)=packet.player;
  *(guint64 *)(bsa_data+0x1c0)=packet.action_slot0;
  *(guint64 *)life_data=packet.life_vtable;
  *(guint64 *)(life_data+0x18)=packet.player;
  *(guint64 *)(life_data+0x1c10)=packet.hp_record;
  *(guint64 *)(life_data+0x1c18)=packet.max_record;
  hp_data[1]=4; max_data[1]=12;
  *(guint32 *)(life_data+0x1ca8)=4; *(guint32 *)(life_data+0x1cac)=12;
  packet.hud_flags=(guint64)(gsize)hud_data+0x1a4;
  packet.plus_flags=(guint64)(gsize)plus_data+0x1a4;
  packet.hud_vtable=0x333; packet.plus_vtable=0x444;
  hud_data[0]=packet.hud_vtable; plus_data[0]=packet.plus_vtable;
  *(guint32 *)(gsize)packet.hud_flags=0x20000;
  packet.target_health=10; packet.maximum=12; packet.required_frames=3;
  packet.tolerance=.3f; packet.rotation_tolerance=.02f;
  packet.matrix[0]=packet.matrix[5]=packet.matrix[10]=1;
  packet.matrix[3]=1; packet.matrix[7]=2; packet.matrix[11]=3;
  packet.state=1; packet.mode=2;
  slots[0]=(void *)0xabcd; slots[1]=dm_gate_stubs;
  slots[2]=(void *)0x111; slots[3]=(void *)0x222;
  dm_gate_top[0]=0x100000000ULL;
  current_frame=100; warp_count=race_mirror=change_tls=readonly_health=0;
  action_count=action_failure=action_bad_selection=action_bad_tls=action_cancel=warp_cancel=0;
  expected_warp_x18=(void *)0xabcd;
}
static void main_tick(void) {
  GumCpuContext cpu; GumInvocationContext ic;
  cpu.sp=(guint64)(gsize)&cpu & ~15ULL;
  ic.function=(void *)0x778899; ic.cpu_context=&cpu; ic.claimed=0;
  on_enter(&ic); on_leave(&ic);
}
static void tick(void) {
  GumCpuContext cpu; GumInvocationContext ic;
  cpu.sp=(guint64)(gsize)&cpu & ~15ULL;
  ic.function=(void *)0x778899; ic.cpu_context=&cpu; ic.claimed=0;
  if(packet.action_enabled && packet.mode==2 && (packet.state==1 || packet.state==7)) {
    if(packet.state==1) {
      // Main cannot run Wait before the actor's normal slot-clear phase.
      on_enter(&ic); assert(!ic.claimed && packet.state==1);
    }
    cpu.x[20]=packet.player; cpu.x[24]=packet.action_bsa;
    on_action_enter(&ic);
  }
  main_tick();
}
int main(void) {
  initialize(); packet.mode=0; tick();
  assert(packet.state==3 && !packet.error && !warp_count && hp_data[1]==4);
  packet.mode=2; packet.state=1; tick();
  assert(packet.state==5 && packet.stable_count==1 && warp_count==1);
  assert(packet.warp_returned && packet.diagnostic_phase==14 && packet.readback_count==1 && packet.last_readback_stable);
  assert(hp_data[1]==10 && *(guint32 *)(life_data+0x1ca8)==10);
  assert(packet.health_written && slots[0]==(void *)0xabcd);
  tick(); assert(packet.stable_count==1 && warp_count==1);
  ++current_frame; tick(); assert(packet.state==5 && packet.stable_count==2);
  ++current_frame; tick(); assert(packet.state==3 && packet.stable_count==3);
  assert(packet.first_stable_frame==100 && packet.last_sample_frame==102);
  assert(packet.diagnostic_phase==15 && packet.readback_count==4 && packet.last_readback_stable);
  assert(packet.post_warp_captured && packet.post_warp_position[0]==1 && packet.post_warp_rotation[0]==1);
  assert(packet.first_readback_captured && packet.first_readback_frame==100);
  assert(packet.first_readback_position[0]==1 && packet.first_readback_rotation[0]==1);
  assert(packet.first_readback_health==10 && packet.first_readback_mirror==10);
  assert(packet.first_readback_stable && !packet.first_readback_error);
  initialize(); {
    GumCpuContext cpu; GumInvocationContext ic;
    cpu.sp=(guint64)(gsize)&cpu & ~15ULL;
    ic.function=(void *)0x778899; ic.cpu_context=&cpu; ic.claimed=0;
    on_enter(&ic);
    // Simulate the original selector overwriting the just-warped pose.
    ((float *)(player_data+0x31c))[0]=99;
    ((float *)(player_data+0x328))[0]=.5f;
    on_leave(&ic);
    assert(packet.post_warp_position[0]==1 && packet.post_warp_rotation[0]==1);
    assert(packet.first_readback_position[0]==99 && packet.first_readback_rotation[0]==.5f);
    assert(packet.first_readback_frame==100 && !packet.first_readback_stable);
    ++current_frame; ((float *)(player_data+0x31c))[0]=88;
    hp_data[1]=9; *(guint32 *)(life_data+0x1ca8)=9; tick();
    assert(packet.position_after[0]==88 && packet.health_after==9);
    assert(packet.first_readback_position[0]==99 && packet.first_readback_health==10);
    assert(packet.post_warp_position[0]==1 && packet.readback_count==2);
  }
  initialize(); *(guint64 *)(life_data+0x18)=0; tick();
  assert(packet.error==8 && packet.state==3 && !warp_count && hp_data[1]==4);
  initialize(); hp_data[1]=0; tick(); assert(packet.error==8 && !warp_count);
  initialize(); readonly_health=1; tick(); assert(packet.error==12 && !warp_count && hp_data[1]==4);
  initialize(); *(guint32 *)(gsize)packet.plus_flags=0x20000; tick();
  assert(packet.error==10 && !warp_count);
  initialize(); race_mirror=1; tick();
  assert(packet.error==9 && packet.state==3 && !packet.health_written);
  initialize(); change_tls=1; tick();
  assert(packet.error==7 && packet.state==3 && !packet.health_written);
  initialize(); cancel_pending(); tick();
  assert(packet.state==4 && !warp_count && hp_data[1]==4);
  initialize(); tick(); cancel_pending(); ++current_frame; tick();
  assert(packet.state==4 && packet.stable_count==1 && warp_count==1);
  initialize(); tick(); ++current_frame; hp_data[1]=9;
  *(guint32 *)(life_data+0x1ca8)=9; tick();
  assert(packet.state==5 && packet.stable_count==0 && warp_count==1);
  assert(packet.diagnostic_phase==14 && packet.warp_returned && !packet.last_readback_stable && packet.readback_count==2);
  initialize(); tick();
  ((float *)(player_data+0x31c))[0]=99;
  hp_data[1]=9; *(guint32 *)(life_data+0x1ca8)=9;
  current_frame=159; tick(); assert(packet.state==5 && !packet.error);
  current_frame=160; tick();
  assert(packet.state==3 && packet.error==13 && packet.diagnostic_phase==15);
  assert(warp_count==1 && hp_data[1]==9 && *(guint32 *)(life_data+0x1ca8)==9);
  assert(packet.readback_count==3 && packet.first_readback_frame==100);
  ++current_frame; tick(); assert(warp_count==1 && packet.readback_count==3 && hp_data[1]==9);
  initialize(); tick(); ++current_frame; ((float *)(player_data+0x328))[0]=0; tick();
  assert(packet.state==5 && packet.stable_count==0);
  initialize(); {
    GumCpuContext cpu; GumInvocationContext ic;
    cpu.sp=((guint64)(gsize)&cpu & ~15ULL)+0x200000;
    ic.function=(void *)0x778899; ic.cpu_context=&cpu; ic.claimed=0;
    on_enter(&ic); on_leave(&ic); assert(packet.error==4 && !warp_count);
  }
  initialize(); {
    GumCpuContext cpu; GumInvocationContext ic;
    cpu.sp=(guint64)(gsize)&cpu & ~15ULL; dm_gate_top[0]=cpu.sp+4096;
    ic.function=(void *)0x778899; ic.cpu_context=&cpu; ic.claimed=0;
    on_enter(&ic); on_leave(&ic); assert(packet.error==3 && !warp_count);
  }
  initialize(); packet.action_enabled=1; tick();
  assert(action_count==1 && packet.action_called && packet.action_returned && packet.action_result==0);
  assert(packet.action_context_restored && packet.action_selected==40 && packet.action_requested==40);
  assert(packet.action_tid==321 && packet.action_frame==100 && packet.action_stack_delta<65536);
  assert(packet.state==7 && warp_count==0 && !packet.error && hp_data[1]==4);
  assert(packet.diagnostic_phase==20 && packet.readback_count==0);
  tick(); main_tick();
  assert(packet.state==7 && action_count==1 && warp_count==0 && packet.action_frame==100);
  ++current_frame; tick();
  assert(packet.state==5 && packet.warp_frame==101 && packet.action_frame==100);
  assert(warp_count==1 && hp_data[1]==10 && packet.stable_count==1);
  ++current_frame; tick(); ++current_frame; tick();
  assert(packet.state==3 && action_count==1 && warp_count==1);
  initialize(); packet.action_enabled=1; {
    GumCpuContext cpu; GumInvocationContext ic;
    cpu.sp=(guint64)(gsize)&cpu & ~15ULL; cpu.x[20]=0; cpu.x[24]=packet.action_bsa;
    ic.function=(void *)0x778899; ic.cpu_context=&cpu; ic.claimed=0;
    on_action_enter(&ic); assert(!ic.claimed && !action_count && packet.state==1);
  }
  initialize(); packet.action_enabled=1; action_data[0x7c]=2; tick();
  assert(packet.error==15 && action_count==0 && !warp_count && hp_data[1]==4);
  initialize(); packet.action_enabled=1; *(guint64 *)(bsa_data+0x18)=0; tick();
  assert(packet.error==14 && action_count==0 && !warp_count);
  initialize(); packet.action_enabled=1; action_failure=3; tick();
  assert(packet.error==16 && action_count==1 && !warp_count && hp_data[1]==4 && slots[0]==(void *)0xabcd);
  initialize(); packet.action_enabled=1; action_bad_selection=1; tick();
  assert(packet.error==18 && !warp_count && hp_data[1]==4);
  initialize(); packet.action_enabled=1; action_cancel=1; tick();
  assert(packet.state==4 && !warp_count && hp_data[1]==4 && slots[0]==(void *)0xabcd);
  initialize(); packet.action_enabled=1; action_bad_tls=1; tick();
  assert(packet.error==17 && !warp_count && hp_data[1]==4 && slots[0]==(void *)0xabcd);
  // Cancelling the pending interval never invokes warp or heals.
  initialize(); packet.action_enabled=1; tick(); cancel_pending();
  assert(packet.state==4 && action_count==1 && !warp_count && hp_data[1]==4);
  ++current_frame; tick(); assert(packet.state==4 && !warp_count);
  // A missing actor callback has a separate 60-guest-frame Main watchdog.
  initialize(); packet.action_enabled=1; tick();
  current_frame=159; main_tick(); assert(packet.state==7 && !packet.error);
  current_frame=160; main_tick();
  assert(packet.state==3 && packet.error==19 && !warp_count && hp_data[1]==4);
  tick(); assert(action_count==1 && !warp_count);
  // The actor path applies the same deadline before any delayed warp.
  initialize(); packet.action_enabled=1; tick(); current_frame=160; tick();
  assert(packet.error==19 && packet.state==3 && !warp_count);
  // Every later callback repeats identity, permission and selection guards.
  initialize(); packet.action_enabled=1; tick(); ++current_frame;
  *(guint64 *)(bsa_data+0x18)=0; tick(); assert(packet.error==14 && !warp_count);
  initialize(); packet.action_enabled=1; tick(); ++current_frame;
  *(guint64 *)(life_data+0x18)=0; tick(); assert(packet.error==8 && !warp_count);
  initialize(); packet.action_enabled=1; tick(); ++current_frame;
  readonly_health=1; tick(); assert(packet.error==12 && !warp_count && hp_data[1]==4);
  initialize(); packet.action_enabled=1; tick(); ++current_frame;
  *(guint32 *)(action_data+0x28)=41; tick(); assert(packet.error==18 && !warp_count);
  initialize(); packet.action_enabled=1; tick(); ++current_frame;
  action_data[0x7c]=2; tick(); assert(packet.error==15 && !warp_count);
  initialize(); packet.action_enabled=1; tick(); ++current_frame;
  slots[g_keygate]=(void *)1; tick(); assert(packet.error==1 && !warp_count);
  // The later actor callback owns a fresh TSD context; never restore frameN's.
  initialize(); packet.action_enabled=1; tick(); ++current_frame;
  expected_warp_x18=slots[g_key18]=(void *)0x777;
  slots[g_keytls]=(void *)0x333; slots[g_keytlr]=(void *)0x444; tick();
  assert(warp_count==1 && !packet.error && slots[g_key18]==(void *)0x777);
  assert(packet.action_tls_after==0x111 && packet.action_tlr_after==0x222);
  assert(packet.action_frame==100 && packet.warp_frame==101);
  initialize(); packet.action_enabled=1; tick(); ++current_frame; change_tls=1; tick();
  assert(packet.error==7 && warp_count==1 && !packet.health_written);
  initialize(); packet.action_enabled=1; tick(); ++current_frame; warp_cancel=1; tick();
  assert(packet.state==4 && warp_count==1 && slots[g_key18]==(void *)0xabcd);
  ++current_frame; tick(); assert(warp_count==1 && action_count==1);
  // The post-warp budget starts at warp_frame, not the earlier Wait frame.
  initialize(); packet.action_enabled=1; tick(); current_frame=101; tick();
  ((float *)(player_data+0x31c))[0]=99;
  current_frame=160; tick(); assert(packet.state==5 && !packet.error);
  current_frame=161; tick(); assert(packet.error==13 && warp_count==1 && action_count==1);
  // Inside the anchor radius is insufficient when motion persists.
  initialize(); tick(); ++current_frame; ((float *)(player_data+0x31c))[0]=1.02f; tick();
  assert(packet.stable_count==0 && !packet.last_readback_stable);
  assert(packet.position_delta_squared>.005f*.005f);
  ((float *)(player_data+0x31c))[0]=1.04f; tick();
  assert(packet.previous_position[0]==1.02f); // duplicate frame cannot replace baseline
  ++current_frame; ((float *)(player_data+0x31c))[0]=1.044f; tick();
  assert(packet.stable_count==0 && !packet.last_readback_stable && !packet.error);
  ++current_frame; tick(); ++current_frame; tick(); ++current_frame; tick();
  assert(packet.state==3 && packet.stable_count==3 && warp_count==1);
  initialize(); tick(); ++current_frame; ((float *)(player_data+0x328))[0]=.99f; tick();
  assert(packet.stable_count==0 && packet.rotation_delta>.005f && !packet.error);
  initialize(); tick(); ++current_frame; ((float *)(player_data+0x31c))[0]=1.004f;
  ((float *)(player_data+0x328))[0]=.996f; tick(); assert(packet.stable_count==2);
  ++current_frame; tick(); assert(packet.state==3 && packet.stable_count==3);
  initialize(); tick(); current_frame+=2; tick();
  assert(packet.stable_count==0 && !packet.last_readback_stable);
  ++current_frame; tick(); ++current_frame; tick(); ++current_frame; tick();
  assert(packet.state==3 && packet.stable_count==3);
  puts("native checkpoint simulated-memory checks passed"); return 0;
}
'''


class NativeCheckpointTests(unittest.TestCase):
    def test_diagnostic_snapshot_is_bounded_and_does_not_run_cleanup(self):
        node=shutil.which('node')
        if node is None:self.skipTest('node unavailable for diagnostic snapshot test')
        script=Path(__file__).resolve().parents[2]/'games'/'0400486024DEC000'/'player_checkpoint.js'
        harness=r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const bytes=Buffer.alloc(760);
class Pointer {
  constructor(offset=0){this.offset=offset;}
  add(value){return new Pointer(this.offset+value);}
  readS32(){return bytes.readInt32LE(this.offset);}
  readU32(){return bytes.readUInt32LE(this.offset);}
  readU64(){return bytes.readBigUInt64LE(this.offset);}
  readFloat(){return bytes.readFloatLE(this.offset);}
  readPointer(){return {isNull:()=>true};}
}
const fail=()=>{throw Error('snapshot invoked a native function or cleanup');};
const actor={readPointer:()=>({equals:()=>true}),add:()=>({readPointer:()=>({equals:()=>true}),readFloat:()=>1})};
const context={rpc:{},packet:new Pointer(),actor,NativeFunction:fail,Interceptor:{flush:fail},
  Process:{findRangeByAddress:fail}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
vm.runInContext("active={packet,player:actor,expectedVtable:{},inactive:false,nonce:'diagnostic-test',result:null,readState:()=>{throw Error('native readState called')}}",context);
bytes.writeInt32LE(2,0);bytes.writeUInt32LE(2,4);bytes.writeUInt32LE(5,432);
bytes.writeUInt32LE(1,416);bytes.writeBigUInt64LE(25441n,424);
let observed=context.rpc.exports.snapshot();
assert.equal(observed.diagnostics.phase_name,'warp_entered');assert(observed.diagnostics.warp_called);
assert.equal(observed.diagnostics.warp_returned,false);assert.equal(observed.diagnostics.snapshot_consistent,false);
assert.equal(observed.diagnostics.warp_frame,'25441');assert.equal(observed.diagnostics.live_observation.atomic,false);
bytes.writeInt32LE(5,0);bytes.writeUInt32LE(3,4);bytes.writeUInt32LE(14,432);
bytes.writeUInt32LE(1,436);bytes.writeUInt32LE(600,440);bytes.writeUInt32LE(0,444);
bytes.writeUInt32LE(10,332);bytes.writeUInt32LE(10,336);bytes.writeUInt32LE(3,344);
bytes.writeFloatLE(-80.5,204);bytes.writeFloatLE(122,208);bytes.writeFloatLE(34.5,212);
bytes.writeUInt32LE(1,568);bytes.writeUInt32LE(1,572);
bytes.writeFloatLE(1,448);bytes.writeFloatLE(2,496);bytes.writeBigUInt64LE(25442n,544);
bytes.writeUInt32LE(10,552);bytes.writeUInt32LE(10,556);
const before=Buffer.from(bytes);observed=context.rpc.exports.status();
assert.equal(observed.diagnostics.phase_name,'waiting_stability');assert(observed.diagnostics.warp_returned);
assert.equal(observed.diagnostics.readback_count,600);assert.equal(observed.diagnostics.stable_frames,0);
assert.equal(observed.diagnostics.last_readback_stable,false);assert(observed.diagnostics.snapshot_consistent);
assert.equal(observed.diagnostics.authoritative_health,10);assert.equal(observed.diagnostics.position_after[0],-80.5);
assert(observed.diagnostics.post_warp.captured);assert.equal(observed.diagnostics.post_warp.position[0],1);
assert(observed.diagnostics.first_readback.captured);assert.equal(observed.diagnostics.first_readback.position[0],2);
assert.equal(observed.diagnostics.first_readback.frame,'25442');assert.equal(observed.diagnostics.first_readback.authoritative_health,10);
assert(bytes.equals(before));assert(JSON.stringify(observed).length<8192);
bytes.writeInt32LE(3,0);bytes.writeUInt32LE(15,432);
observed=context.rpc.exports.status();assert.equal(observed.state,3);assert(observed.active);
assert.equal(observed.descriptor_restored,false); // A snapshot never calls finish/detach.
bytes.writeInt32LE(7,0);bytes.writeUInt32LE(20,432);
bytes.writeFloatLE(.000004,752);bytes.writeFloatLE(.003,756);
observed=context.rpc.exports.status();
assert.equal(observed.diagnostics.phase_name,'waiting_later_actor_update');
assert.equal(observed.diagnostics.pre_warp_frame_budget,60);
assert.equal(observed.diagnostics.motion_delta.limit,.005);
assert(Math.abs(observed.diagnostics.motion_delta.position-.002)<1e-8);
assert(Math.abs(observed.diagnostics.motion_delta.rotation_element-.003)<1e-8);
assert(observed.diagnostics.snapshot_consistent && !observed.descriptor_restored);
// Exercise real cancel/finish/detach logic; a busy actor cannot acknowledge early.
let detached=0,flushed=0,sent=0;
context.Interceptor.flush=()=>{++flushed;};context.send=()=>{++sent;};
context.metrics=()=>({error_code:0,diagnostics:{}});
context.setupRun=()=>({packet:new Pointer(),player:actor,expectedVtable:{},
  mode:'player_checkpoint',nativePhase:'restore',inactive:false,nonce:'cancel-test',timer:null,
  listener:{detach(){++detached;}},actionListener:{detach(){++detached;}},
  readState:()=>bytes.readInt32LE(0),cancelPending:()=>{
    bytes.writeInt32LE(1,348);
    if([1,5,7].includes(bytes.readInt32LE(0)))bytes.writeInt32LE(4,0);
    return bytes.readInt32LE(0);
  }});
vm.runInContext('active=setupRun()',context);bytes.writeInt32LE(6,0);
let cancelled=context.rpc.exports.cancel();
assert(!cancelled.descriptor_restored && cancelled.active && !detached && !flushed && !sent);
bytes.writeInt32LE(4,0); // native callback restored context and returned from its claim
cancelled=context.rpc.exports.cancel();
assert(cancelled.descriptor_restored && !cancelled.active && detached===2 && flushed===1 && sent===1);
vm.runInContext('active=setupRun()',context);bytes.writeInt32LE(7,0);
cancelled=context.rpc.exports.cancel();
assert(cancelled.descriptor_restored && !cancelled.active && detached===4 && flushed===2 && sent===2);
"""
        result=subprocess.run([node,'-e',harness,str(script)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_physics_snapshot_checks_chain_and_method_without_native_calls(self):
        node=shutil.which('node')
        if node is None:self.skipTest('node unavailable for physics snapshot test')
        script=Path(__file__).resolve().parents[2]/'games'/'0400486024DEC000'/'player_checkpoint.js'
        harness=r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const pointers=new Map(),reads=[];
class Pointer {
  constructor(value){this.value=value;}
  add(value){return new Pointer(this.value+value);}
  equals(other){return this.value===other.value;}
  isNull(){return this.value===0;}
  toString(){return '0x'+this.value.toString(16);}
  readPointer(){reads.push(this.value);return new Pointer(pointers.get(this.value)||0);}
  readU32(){reads.push(this.value);return this.value===0x7070 ? bodyFlags : 3;}
  readU64(){reads.push(this.value);return BigInt(this.value===0x7070 ? bodyFlags : 64);}
  readFloat(){reads.push(this.value);return this.value;}
}
let bodyFlags=0;
const base=new Pointer(0x10000000),player=new Pointer(0x1000),packet=new Pointer(0x9000);
const entries=[[0x1270,0x2000],[0x90f8,0x2000],[0x2050,0x3000],
 [0x3038,0x1000],[0x3020,0x4000],[0x3000,0x30000],[0x4000,0x40000],
 [0x4190,0x7000],[0x4010,0x5000],[0x5008,0x6000],[0x6000,0xa000],
 [0xa0c8,0x106b2330],[0x6020,0x7000],[0x7000,0x70000],[0x7060,0x8000]];
for(const entry of entries)pointers.set(...entry);
const fail=()=>{throw Error('snapshot performed a native call or mapping lookup');};
const context={rpc:{},Process:{findRangeByAddress:fail},NativeFunction:fail};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
let result=context.physicsObservation({base,player,packet});
assert(result.available && !result.atomic && result.body_layout_verified);
assert.equal(result.model_pending_matrix.length,12);assert.equal(result.body_pending_matrix.length,12);
assert.equal(result.body_pending_flags,'64');assert(reads.length<100);
bodyFlags=0x100;result=context.physicsObservation({base,player,packet});
assert(result.body_uses_global_scratch);assert.equal(result.body_pending_matrix,undefined);
bodyFlags=0;pointers.set(0xa0c8,0xbad);reads.length=0;
result=context.physicsObservation({base,player,packet});
assert(result.available && !result.body_layout_verified);assert(!reads.includes(0x7060));
pointers.set(0x3038,0xbad);reads.length=0;result=context.physicsObservation({base,player,packet});
assert(!result.available);assert(!reads.includes(0x4000));
pointers.set(0x3038,0x1000);pointers.set(0x5008,0);
result=context.physicsObservation({base,player,packet});assert(result.available);assert.equal(result.controller,'0x0');
"""
        result=subprocess.run([node,'-e',harness,str(script)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_mapping_cache_reuses_containment_but_rejects_missing_permission(self):
        node=shutil.which('node')
        if node is None:self.skipTest('node unavailable for mapping-cache test')
        script=Path(__file__).resolve().parents[2]/'games'/'0400486024DEC000'/'player_checkpoint.js'
        harness=r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Pointer {
  constructor(value) { this.value=BigInt(value); }
  add(value) { return new Pointer(this.value+BigInt(value)); }
  compare(other) { return this.value<other.value ? -1 : this.value>other.value ? 1 : 0; }
}
let calls=0;
const context={rpc:{},Process:{findRangeByAddress(address) {
  ++calls;
  if(address.value>=0x1000n && address.value<0x2000n)
    return {base:new Pointer(0x1000),size:0x1000,protection:'rw-'};
  return null;
}}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
context.requireRange(new Pointer(0x1100),16,'r','first');
context.requireRange(new Pointer(0x1800),32,'w','second');
assert.strictEqual(calls,1);
assert.throws(()=>context.requireRange(new Pointer(0x1800),16,'x','code'),/suitable/);
assert.strictEqual(calls,2);
assert.throws(()=>context.requireRange(new Pointer(0x1ff0),32,'r','crossing'),/suitable/);
assert.strictEqual(calls,3);
assert.throws(()=>context.requireRange(new Pointer(0x3000),16,'r','unmapped'),/suitable/);
assert.strictEqual(calls,4);
"""
        result=subprocess.run([node,'-e',harness,str(script)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_native_callback_guards_and_distinct_frame_completion(self):
        compiler = shutil.which('clang')
        if compiler is None:
            self.skipTest('clang unavailable for native callback test')
        script = (Path(__file__).resolve().parents[2]/'games'/'0400486024DEC000'/'player_checkpoint.js').read_text()
        source = script.split('const NATIVE_SOURCE = `', 1)[1].split('`;', 1)[0]
        with tempfile.TemporaryDirectory(prefix='native-checkpoint-test-') as directory:
            root = Path(directory)
            (root/'gum').mkdir()
            (root/'gum'/'guminterceptor.h').write_text(HEADER)
            source_path = root/'checkpoint.c'
            source_path.write_text('#define pthread_getspecific test_getspecific\n'
                                   '#define pthread_setspecific test_setspecific\n'+source+HARNESS)
            binary = root/'checkpoint'
            built = subprocess.run([compiler, '-std=c11', '-Wall', '-Wextra', '-Werror',
                '-I', str(root), str(source_path), '-o', str(binary)], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(ran.returncode, 0, ran.stderr)


if __name__=='__main__':unittest.main()
