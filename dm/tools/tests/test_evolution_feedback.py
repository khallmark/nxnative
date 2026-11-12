"""Explicit target-feedback policies, guarded acknowledgements, and honest scoring."""
import io
from pathlib import Path
import sys
import tempfile
import unittest
from typing import Any
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evolution.episodes import NativeEpisodes,validate_episode
from evolution.live import TrialStopped
from evolution.replay import ReplayBridge,validate_position_xyz
from evolution.timeline import EpisodeScore,compile_timeline,gene_space,seed_genome


class FeedbackTests(unittest.TestCase):
    policy='target_feedback_v1'
    world=dict(rotation_address='0x1000',guard1_address='0x2000',guard1_value='0x3000',guard2_address='0x4000',guard2_value='0x5000')
    position=dict(position_address='0x6000',guard1_address='0x7000',guard1_value='0x8000',guard2_address='0x9000',guard2_value='0xa000')

    def test_feedback_seed_is_many_gene_walking_and_contiguous(self):
        genes=seed_genome(policy=self.policy);self.assertEqual(len(genes),1024)
        text=compile_timeline(genes,128,601,policy=self.policy,target=[-58,117.3,-6])
        end=0
        for line in text.splitlines():
            at,action=line.split(':');keys,hold=action.split('~f')
            self.assertEqual(int(at[1:]),end);end+=int(hold)
            self.assertNotIn('+',keys);self.assertNotIn('L',keys)
            self.assertEqual([float(x) for x in keys[1:].split('/')],[-58,117.3,-6,1,.1,.75])
        self.assertEqual(end,601)

    def test_offsets_strength_braking_and_buttons_are_genes(self):
        genes=seed_genome(2,policy=self.policy)
        genes.update({'0000.target_dx':1.25,'0000.target_dz':-2.,'0000.strength':.3,
                      '0000.slow_radius':1.2,'0000.jump':1.,'0000.frames':7.})
        rows=compile_timeline(genes,2,10,policy=self.policy,target=[-10,2,3]).splitlines()
        self.assertEqual(rows,['f0:G-8.750000000/2.000000000/1.000000000/0.300000000/0.100000000/1.200000000+X~f7',
                               'f7:G-10.000000000/2.000000000/3.000000000/1.000000000/0.100000000/0.750000000~f3'])

    def test_feedback_rejects_old_genomes_nonfinite_targets_and_invalid_radii(self):
        genes=seed_genome(1,policy=self.policy)
        for target in (None,[],[0,0],[0,True,0],[0,float('nan'),0],[1e6,0,0]):
            with self.subTest(target=target),self.assertRaises(ValueError):
                compile_timeline(genes,1,30,policy=self.policy,target=target)
        for radius in (-.1,.21,True,float('inf')):
            with self.subTest(radius=radius),self.assertRaises(ValueError):
                compile_timeline(genes,1,30,policy=self.policy,target=[0,0,1],stop_radius=radius)
        for invalid in (seed_genome(1),{**genes,'0000.slow_radius':0}, {**genes,'0000.strength':float('nan')}):
            with self.assertRaises(ValueError):compile_timeline(invalid,1,30,policy=self.policy,target=[0,0,1])
        with self.assertRaises(ValueError):
            compile_timeline({**genes,'0000.target_dx':8},1,30,policy=self.policy,target=[999999,0,1])
        invalid_policies: list[Any]=['unknown',True,[]]
        for policy in invalid_policies:
            with self.assertRaises(ValueError):gene_space(1,policy)

    def test_stop_radius_must_fit_objective_and_horizontal_arrival_is_not_success(self):
        spec=dict(start=[0,0,0],target=[1,1,1],start_health=6,horizon_frames=600,policy=self.policy)
        validate_episode(spec)
        for changed in ({'tolerance':.05},{'feedback_stop_radius':.25},{'feedback_stop_radius':True},{'target':None}):
            with self.subTest(changed=changed),self.assertRaises(ValueError):validate_episode({**spec,**changed})
        with self.assertRaises(ValueError):validate_episode({**spec,'policy':'timeline_v1','feedback_stop_radius':.1})
        score=EpisodeScore([0,0,0],[1,1,1],600)
        for frame in range(1,8):score.observe(frame,[1,0,1],6)
        self.assertFalse(score.finish('horizon')['success'])

    def test_position_binding_validation_is_canonical_and_bounded(self):
        self.assertIsNone(validate_position_xyz(None))
        self.assertEqual(validate_position_xyz(self.position),self.position)
        for value in ({},True,[],{**self.position,'extra':'0x1'}):
            with self.assertRaises(ValueError):validate_position_xyz(value)
        for key in self.position:
            for value in (True,123,'123','0x_1','0x10000000000000000'):
                with self.subTest(key=key,value=value),self.assertRaises(ValueError):validate_position_xyz({**self.position,key:value})
        for key in ('position_address','guard1_address','guard2_address'):
            for value in ('0x0','0x1001','0xfffffffffffffffc'):
                with self.subTest(key=key,value=value),self.assertRaises(ValueError):validate_position_xyz({**self.position,key:value})
        canonical=validate_position_xyz({**self.position,'guard2_value':'0xFFFFFFFFFFFFFFFF'})
        assert canonical is not None
        self.assertEqual(canonical['guard2_value'],'0xffffffffffffffff')

    def bridge(self,path):
        b=ReplayBridge.__new__(ReplayBridge);b.root=path;b.world_xz=self.world;b.position_xyz=self.position
        b.episode_epoch=None;b.native_status={};b.owns_replay=False
        return b

    def test_feedback_ack_exact_binding_mode_and_position_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            bridge=self.bridge(Path(td));calls=[]
            valid=dict(epoch=7,current_frame=100,left_stick_space='world_xz',world_xz=self.world,last_camera_frame=0,
                       movement_mode='target_feedback',position_xyz=self.position,last_position_frame=0,last_position=None)
            def rpc(verb,before=(),after=()):calls.append(after);return valid
            bridge.rpc=rpc
            bridge.start_replay('f0:G1/2/3/1/.1/.75~f30\n',0xb000,target_feedback=True)
            self.assertEqual(bridge.episode_epoch,7)
            self.assertEqual(calls,[('health=b000:0','world_xz=1000:2000:3000:4000:5000','position_xyz=6000:7000:8000:9000:a000')])
            for changed in ({'movement_mode':'timeline'},{'position_xyz':None},
                            {'position_xyz':{**self.position,'guard1_value':'0x99'}},
                            {'last_position_frame':True},{'last_position_frame':101},
                            {'last_position_frame':99}, {'last_position':[1,2]},
                            {'last_position':[1,float('nan'),3]}, {'last_position':[True,2,3]}):
                bridge.episode_epoch=None;bridge.rpc=lambda *a,c=changed,**k:{**valid,**c}
                with self.subTest(changed=changed),self.assertRaises(TrialStopped):
                    bridge.start_replay('f0:G1/2/3/1/.1/.75~f30\n',target_feedback=True)
                self.assertTrue(bridge.owns_replay);self.assertIsNone(bridge.episode_epoch)
                self.assertEqual(list(Path(td).iterdir()),[])
            bridge.rpc=lambda *a,**k:{**valid,'last_position_frame':99,'last_position':[1,2,3]}
            bridge.start_replay('f0:G1/2/3/1/.1/.75~f30\n',target_feedback=True)

    def test_ui_and_legacy_timelines_never_send_position_binding(self):
        with tempfile.TemporaryDirectory() as td:
            b=self.bridge(Path(td));calls=[]
            def rpc(verb,before=(),after=()):calls.append(after);return dict(epoch=7,current_frame=100)
            b.rpc=rpc
            b.start_replay('f0:A~f6\n',episode=False)
            self.assertEqual(calls,[()])
            with self.assertRaises(ValueError):b.start_replay('f0:G1/2/3/1/.1/.75~f30\n',episode=False,target_feedback=True)
            b.position_xyz=None
            with self.assertRaises(ValueError):b.start_replay('f0:G1/2/3/1/.1/.75~f30\n',target_feedback=True)
            self.assertEqual(len(calls),1)

    def test_uncertain_start_is_cancelled_before_runner_cleanup(self):
        class Bridge:
            config={'reads':{'health':{'address':'0x1000'}},'recovery':{'files':{}},'ui':{'gameplay':[{'read':'hud','equals':1}]}}
            world_xz=self.world;position_xyz:dict[str,str]|None=self.position;owns_replay=False;cancelled=False
            def sample(self,**kwargs):return dict(position=[0,0,0],health=6,reads={'hud':[1]})
            def start_replay(self,*args,**kwargs):
                self.owns_replay=True;raise TrialStopped('wrong feedback ACK')
            def cancel_replay(self):self.cancelled=True
        b=Bridge();spec=dict(start=[0,0,0],target=[1,0,1],start_health=6,horizon_frames=600,segments=1,policy=self.policy)
        with self.assertRaisesRegex(TrialStopped,'wrong feedback ACK'):
            NativeEpisodes(b,spec,io.StringIO()).evaluate(seed_genome(1,policy=self.policy))
        self.assertTrue(b.cancelled)
        b.position_xyz=None
        with self.assertRaisesRegex(ValueError,'guarded position'):NativeEpisodes(b,spec,io.StringIO())


if __name__=='__main__':unittest.main()
