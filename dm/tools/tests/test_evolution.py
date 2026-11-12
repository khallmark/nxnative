"""Regression tests for resumability, selection and live-controller boundaries."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from typing import Any
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evolution.core import Evolution, BASELINE, closest_approach, steer
from evolution.live import LiveTrials, TrialStopped, Checkpoint, ui_matches, validate_ui, validate_tasks

TASKS=[{'id':'a'},{'id':'b'}]


class EvolutionTests(unittest.TestCase):
    def test_resume_exact_pending_and_random_state(self):
        with tempfile.TemporaryDirectory() as td:
            a=Evolution(Path(td)/'a.db',TASKS,4,2)
            b=Evolution(Path(td)/'b.db',TASKS,4,2)
            for count in range(16):
                ka,kb=a.pending(),b.pending()
                self.assertEqual(ka,kb)
                assert ka is not None and kb is not None
                result=dict(score=float(ka[1]),success=True)
                a.record(ka,result);b.record(kb,result)
                if count==5:
                    b.db.close();b=Evolution(Path(td)/'b.db',TASKS,4,2)
            recorded=json.loads(a.db.execute('SELECT result FROM trials WHERE candidate=0 LIMIT 1').fetchone()[0])
            self.assertEqual(recorded['genome'],a.state['candidates'][0])
            self.assertEqual(a.advance(),b.advance())
            self.assertEqual(a.state,b.state)
            self.assertEqual(b.pending(),(1,0,0,0))

    def test_partial_generation_cannot_promote(self):
        with tempfile.TemporaryDirectory() as td:
            e=Evolution(Path(td)/'a.db',TASKS,4,2)
            e.record(e.pending(),dict(score=999.,success=True))
            with self.assertRaises(ValueError):e.advance()

    def test_worst_task_blocks_lucky_champion(self):
        with tempfile.TemporaryDirectory() as td:
            e=Evolution(Path(td)/'a.db',TASKS,4,2)
            while (key:=e.pending()) is not None:
                score=1000 if key[1]==0 else 10-key[1]
                if key[1]==0 and key[2]==1:score=-100
                e.record(key,dict(score=score,success=score>0))
            expected=e.state['candidates'][1]
            self.assertEqual(e.advance()['genome'],expected)

    def test_failed_long_route_cannot_outscore_full_success(self):
        with tempfile.TemporaryDirectory() as td:
            e=Evolution(Path(td)/'a.db',TASKS,4,2)
            while (key:=e.pending()) is not None:
                e.record(key,dict(score=1000 if key[1]==0 else 10-key[1],success=key[1]!=0))
            expected=e.state['candidates'][1]
            report=e.advance()
            self.assertEqual(report['genome'],expected)
            self.assertTrue(report['all_trials_successful'])

    def test_curriculum_changes_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            e=Evolution(Path(td)/'a.db',TASKS,4,2);e.db.close()
            with self.assertRaises(ValueError):Evolution(Path(td)/'a.db',[{'id':'different'}],4,2)

    def test_implementation_changes_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            e=Evolution(Path(td)/'a.db',TASKS,4,2,implementation='before');e.db.close()
            with self.assertRaises(ValueError):
                Evolution(Path(td)/'a.db',TASKS,4,2,implementation='after')

    def test_transfer_retains_genes_but_not_old_fitness(self):
        with tempfile.TemporaryDirectory() as td:
            seed={**BASELINE,'gain':1.8}
            e=Evolution(Path(td)/'new.db',TASKS,4,2,initial_genome=seed)
            self.assertEqual(e.state['candidates'][0],seed)
            self.assertEqual(e.pending(),(0,0,0,0))
            self.assertIsNone(e.state['champion'])

    def test_search_improves_a_fixed_parameter_objective(self):
        with tempfile.TemporaryDirectory() as td:
            e=Evolution(Path(td)/'a.db',[{'id':'synthetic'}],8,2)
            best=[]
            for generation in range(15):
                while (key:=e.pending()) is not None:
                    p=e.state['candidates'][key[1]]
                    score=-((p['gain']-1.75)**2+(p['avoid']-2.5)**2+(p['side']+.75)**2)
                    e.record(key,dict(score=score,success=True))
                best.append(e.advance()['fitness'])
            self.assertGreater(best[-1],best[0]+.2)


class CurriculumTests(unittest.TestCase):
    def test_rejects_incomplete_or_nonfinite_safeguards(self):
        task=dict(kind='reciprocal_navigation',start=[0,0,0],targets=[[1,0,0]],
                  bounds=[[-2,2],[-1,1],[-2,2]],min_health=8,timeout=10)
        validate_tasks([task])
        for field,value in [('bounds',[[-2,2]]),('targets',[]),('targets',[[1,0,float('nan')]]),
                            ('min_health',0),('timeout',float('inf')),('targets',[[10,0,0]])]:
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):
                validate_tasks([{**task,field:value}])

    def test_pass_through_is_checkpoint_only_and_final_target_must_settle(self):
        task=dict(kind='checkpoint_navigation',start=[0,0,0],targets=[[1,0,0],[2,0,0]],
                  bounds=[[-3,3],[-1,1],[-2,2]],min_health=12,timeout=10,settle_targets=[False,True])
        validate_tasks([task])
        for bad in [dict(settle_targets=[]),dict(settle_targets=[False,False]),dict(settle_targets=[0,True]),
                    dict(settle_targets=[True]),dict(kind='reciprocal_navigation')]:
            with self.subTest(bad=bad),self.assertRaises(ValueError):validate_tasks([{**task,**bad}])


class PolicyTests(unittest.TestCase):
    def state(self,position=(0,0,0),health=12) -> dict[str, Any]:
        return dict(position=position,health=health,time=1.,hazard=None,
                    camera_rotation=[1,0,0,0,1,0,0,0,1])

    def test_crossing_hazard_changes_action(self):
        s=self.state();p=copy.deepcopy(s);s['hazard']=(2,1,-2);p['hazard']=(2,1,-3);p['time']=.8
        plain=steer(self.state(),None,(4,0,0),BASELINE)
        avoid=steer(s,p,(4,0,0),BASELINE)
        self.assertLess(avoid.risk,4)
        self.assertNotEqual(plain.keys,avoid.keys)
        self.assertAlmostEqual(closest_approach((3,0),(-6,0),1),0)

    def test_hazard_below_platform_does_not_repel(self):
        s=self.state((0,10,0));s['hazard']=(1,1,0)
        self.assertEqual(steer(s,None,(4,10,0),BASELINE).keys,steer(self.state(),None,(4,0,0),BASELINE).keys)

    def test_arrival_does_not_hide_final_action_damage(self):
        import io
        initial=self.state();final=self.state((1,0,0),11)
        class FakeBridge:
            def sample(self,keys=None,hold=.15):return final if keys else initial
        env=LiveTrials(FakeBridge(),io.StringIO())
        task=dict(timeout=10,bounds=[[-2,2],[-1,1],[-2,2]],min_health=8)
        with self.assertRaises(TrialStopped):env.route(task,[(1,0,0)],BASELINE)

    def test_pass_through_does_not_wait_on_intermediate_hazard_lane(self):
        import io
        snapshots=iter([self.state(),self.state((1,0,0)),self.state((2,0,0)),self.state((2,0,0))])
        actions=[]
        class FakeBridge:
            def sample(self,keys=None,hold=.15):
                actions.append(keys);return next(snapshots)
        task=dict(kind='checkpoint_navigation',timeout=10,bounds=[[-3,3],[-1,1],[-2,2]],min_health=12,
                  settle_targets=[False,True])
        result=LiveTrials(FakeBridge(),io.StringIO()).route(task,[(1,0,0),(2,0,0)],BASELINE)
        self.assertTrue(result['success']);self.assertEqual(result['reached'],2)
        self.assertEqual(actions.count(''),1);self.assertEqual(actions[-1],'')

    def test_pass_through_cannot_hide_damage_on_arrival(self):
        import io
        snapshots=iter([self.state(),self.state((1,0,0),11)])
        class FakeBridge:
            def sample(self,*args):return next(snapshots)
        task=dict(kind='checkpoint_navigation',timeout=10,bounds=[[-3,3],[-1,1],[-2,2]],min_health=12,
                  settle_targets=[False,True])
        result=LiveTrials(FakeBridge(),io.StringIO()).route(task,[(1,0,0),(2,0,0)],BASELINE)
        self.assertFalse(result['success']);self.assertEqual(result['reached'],0)

    def test_wrong_height_is_not_arrival(self):
        import io
        s=self.state((1,5,0))
        class FakeBridge:
            def sample(self,*args):return s
        task=dict(timeout=10,bounds=[[-2,2],[-1,1],[-2,2]],min_health=8)
        with self.assertRaises(TrialStopped):LiveTrials(FakeBridge(),io.StringIO()).route(task,[(1,0,0)],BASELINE)


class CheckpointTests(unittest.TestCase):
    def config(self,path) -> dict[str, Any]:
        import hashlib
        return dict(reads={'hud':{'format':'I'},'pause':{'format':'I'}},
            ui={'gameplay':[dict(read='hud',equals=1)],'pause':[dict(read='pause',equals=1)]},
            recovery=dict(files={str(path):hashlib.sha256(path.read_bytes()).hexdigest()},
                steps=[dict(keys='Plus',before='gameplay',after='pause',settle=0),
                       dict(keys='A',before='pause',after='gameplay',settle=0)]))

    def test_invalid_ui_and_recovery_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'save';path.write_bytes(b'checkpoint')
            c=self.config(path);validate_ui(c)
            for update in [dict(ui={'pause':[],'gameplay':[]}),
                           dict(ui={'pause':[dict(read='missing',equals=0)],'gameplay':[dict(read='hud',equals=1)]})]:
                with self.assertRaises(ValueError):validate_ui({**c,**update})
            c['recovery']['steps'][0]['keys']='A~30'
            with self.assertRaises(ValueError):validate_ui(c)

    def test_ui_mask_does_not_confuse_inactive_flags(self):
        c={'ui':{'pause':[dict(read='flags',mask='0x20000',equals='0x20000')]}}
        self.assertFalse(ui_matches(c,{'reads':{'flags':[0xfc01]}},'pause'))
        self.assertTrue(ui_matches(c,{'reads':{'flags':[0x20001]}},'pause'))

    def test_save_changes_abort_before_any_input(self):
        import io
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'save';path.write_bytes(b'checkpoint')
            c=self.config(path)
            class FakeBridge:
                config=c
                def wait_ui(self,*args):raise AssertionError('No UI operation allowed')
            checkpoint=Checkpoint(FakeBridge(),io.StringIO())
            path.write_bytes(b'new checkpoint')
            with self.assertRaisesRegex(TrialStopped,'save changed'):checkpoint.restore({})
            path.unlink()
            with self.assertRaisesRegex(TrialStopped,'Cannot read checkpoint save'):checkpoint.restore({})

    def test_wrong_menu_stops_before_next_input(self):
        import io
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'save';path.write_bytes(b'checkpoint');c=self.config(path)
            class FakeBridge:
                config=c
                actions=[]
                def wait_ui(self,name,*args):
                    if name=='pause':raise TrialStopped('Expected pause')
                    return {}
                def sample(self,keys,*args,**kwargs):self.actions.append(keys)
            bridge=FakeBridge()
            with self.assertRaises(TrialStopped):Checkpoint(bridge,io.StringIO()).restore({})
            self.assertEqual(bridge.actions,['Plus'])

    def test_wrong_loaded_position_is_rejected(self):
        import io
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'save';path.write_bytes(b'checkpoint');c=self.config(path)
            class FakeBridge:
                config=c
                def wait_ui(self,*args):return {}
                def sample(self,*args,**kwargs):return dict(position=(9,0,0),health=12,reads={'hud':[1]})
            task=dict(start=[0,0,0],bounds=[[-1,1]]*3,min_health=12)
            with self.assertRaisesRegex(TrialStopped,'does not match'):Checkpoint(FakeBridge(),io.StringIO()).restore(task)

    def test_failed_episode_is_scored_and_restored(self):
        import io
        state=PolicyTests().state()
        class FakeBridge:
            def sample(self,*args):return state
        class Recovery(Checkpoint):
            def __init__(self):self.restored=0
            def verify_files(self):pass
            def restore(self,task):self.restored+=1;return state
        env=LiveTrials(FakeBridge(),io.StringIO());recovery=Recovery();env.checkpoint=recovery
        task=dict(kind='checkpoint_navigation',start=[0,0,0],targets=[[1,0,0]],bounds=[[-2,2]]*3,min_health=12)
        env.route=lambda task,targets,genome,reset=False:dict(success=False,reason='timeout',reached=0,elapsed=1.,error=1.,state=state)
        result=env.evaluate(task,BASELINE)
        self.assertFalse(result['success']);self.assertLess(result['score'],0)
        self.assertEqual(recovery.restored,1)

    def test_failed_restore_preserves_result_and_stops(self):
        import io
        state=PolicyTests().state()
        class FakeBridge:
            def sample(self,*args):return state
        class Recovery(Checkpoint):
            def __init__(self):pass
            def verify_files(self):pass
            def restore(self,task):raise TrialStopped('wrong checkpoint')
        env=LiveTrials(FakeBridge(),io.StringIO());recovery=Recovery();env.checkpoint=recovery
        task=dict(kind='checkpoint_navigation',start=[0,0,0],targets=[[1,0,0]],bounds=[[-2,2]]*3,min_health=12)
        env.route=lambda task,targets,genome,reset=False:dict(success=False,reason='damage',reached=0,elapsed=1.,error=1.,state=state)
        with self.assertRaises(TrialStopped) as error:env.evaluate(task,BASELINE)
        self.assertIsNotNone(error.exception.result)
        assert error.exception.result is not None
        self.assertFalse(error.exception.result['success'])


if __name__=='__main__':unittest.main()
