"""Bounded health tradeoffs and ignored pause input during damage stagger."""
import io
import itertools
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evolution.core import BASELINE
from evolution.live import LiveTrials,TrialStopped,Checkpoint,guarded_pause,validate_tasks


def state(x=0,hp=12):
    return dict(position=(x,0,0),health=hp,time=1.,hazard=None,camera_rotation=[1,0,0,0,1,0,0,0,1])


def task(**updates):
    return dict(dict(kind='checkpoint_navigation',start=[0,0,0],targets=[[1,0,0]],
        bounds=[[-1,5],[-1,1],[-1,1]],min_health=10,start_health=12,max_damage=2,timeout=10),**updates)


class DamageTests(unittest.TestCase):
    def run_route(self,states,spec):
        values=iter(states)
        class Bridge:
            def sample(self,*args):return next(values)
        return LiveTrials(Bridge(),io.StringIO()).route(spec,spec['targets'],BASELINE)

    def test_allowed_damage_finishes_and_is_recorded(self):
        result=self.run_route([state(),state(1,11),state(1,11)],task())
        self.assertTrue(result['success']);self.assertEqual(result['damage'],1)
        self.assertEqual(result['lowest_health'],11);self.assertEqual(result['initial_health'],12)

    def test_healing_cannot_hide_cumulative_damage(self):
        spec=task(targets=[[1,0,0],[2,0,0],[3,0,0]],settle_targets=[False,False,True],max_damage=1)
        result=self.run_route([state(),state(1,11),state(2,12),state(3,11)],spec)
        self.assertFalse(result['success']);self.assertEqual(result['damage'],2);self.assertEqual(result['reached'],2)

    def test_minimum_health_is_independent_of_damage_allowance(self):
        result=self.run_route([state(),state(1,10)],task(min_health=11,max_damage=3))
        self.assertFalse(result['success']);self.assertEqual(result['damage'],2)

    def test_damage_limits_and_start_health_are_validated(self):
        validate_tasks([task()])
        for values in [dict(max_damage=-1),dict(max_damage=80),dict(max_damage=1.2),dict(max_damage=True),
                       dict(start_health=9),dict(start_health=81),dict(kind='reciprocal_navigation')]:
            with self.subTest(values=values),self.assertRaises(ValueError):validate_tasks([task(**values)])

    def test_damage_is_penalized_in_fitness(self):
        class Bridge:
            def sample(self):return state()
        class Recovery(Checkpoint):
            def __init__(self):pass
            def verify_files(self):pass
            def restore(self,spec):return state()
        env=LiveTrials(Bridge(),io.StringIO());env.checkpoint=Recovery()
        scores=[]
        for damage in [0,1]:
            env.route=lambda *args,d=damage,**kwargs:dict(success=True,reached=1,elapsed=1.,error=0.,damage=d,state=state(1,12-d))
            scores.append(env.evaluate(task(),BASELINE)['score'])
        self.assertEqual(scores[0]-scores[1],25)

    def test_episode_restores_start_health_before_scoring(self):
        class Bridge:
            def sample(self):return state(0,10)
        class Recovery(Checkpoint):
            def __init__(self):pass
            count=0
            def verify_files(self):pass
            def restore(self,spec):self.count+=1;return state()
        env=LiveTrials(Bridge(),io.StringIO());recovery=Recovery();env.checkpoint=recovery
        env.route=lambda *args,**kwargs:dict(success=True,reached=1,elapsed=1.,error=0.,damage=0,state=state(1))
        env.evaluate(task(),BASELINE);self.assertEqual(recovery.count,2)


class PauseTests(unittest.TestCase):
    def run_pause(self,mode):
        class Bridge:
            config={'ui':{'gameplay':[dict(read='hud',equals=1)],'pause':[dict(read='pause',equals=1)]}}
            presses=0
            def sample(self,keys=None,*args,**kwargs):
                if keys=='Plus':self.presses+=1
                paused=mode=='already' or (mode=='stagger' and self.presses>=2)
                unknown=mode=='unknown' and self.presses>0
                return dict(health=0 if mode=='dead' else 12,reads={'hud':[int(not paused and not unknown)],'pause':[int(paused)]})
        bridge=Bridge();clock=itertools.count(0,.1)
        with patch('evolution.live.time.monotonic',side_effect=lambda:next(clock)),patch('evolution.live.time.sleep'):
            if mode in ('unknown','dead'):
                with self.assertRaises(TrialStopped):guarded_pause(bridge,timeout=2)
            else:guarded_pause(bridge,timeout=2)
        return bridge.presses

    def test_ignored_press_is_retried_only_while_gameplay_remains_observed(self):
        self.assertEqual(self.run_pause('stagger'),2)

    def test_unknown_screen_does_not_receive_retry(self):
        self.assertEqual(self.run_pause('unknown'),1)

    def test_already_paused_and_dead_receive_no_press(self):
        self.assertEqual(self.run_pause('already'),0);self.assertEqual(self.run_pause('dead'),0)


if __name__=='__main__':unittest.main()
