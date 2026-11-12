"""Regression tests for resumability, selection and live-controller boundaries."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from typing import Any
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evolution.core import Evolution, BASELINE, closest_approach, steer
from evolution.live import LiveTrials, TrialStopped, validate_tasks

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

    def test_wrong_height_is_not_arrival(self):
        import io
        s=self.state((1,5,0))
        class FakeBridge:
            def sample(self,*args):return s
        task=dict(timeout=10,bounds=[[-2,2],[-1,1],[-2,2]],min_health=8)
        with self.assertRaises(TrialStopped):LiveTrials(FakeBridge(),io.StringIO()).route(task,[(1,0,0)],BASELINE)


if __name__=='__main__':unittest.main()
