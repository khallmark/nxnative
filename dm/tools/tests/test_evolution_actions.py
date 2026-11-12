"""Action waypoint contracts, landing evidence and ordinary walking compatibility."""
import io
import itertools
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from typing import Any
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evolution.core import BASELINE
from evolution.live import LiveTrials,validate_tasks


def state(x:float=0,y:float=0,hp=12):
    return dict(position=(x,y,0),health=hp,time=1.,hazard=None,camera_rotation=[1,0,0,0,1,0,0,0,1])


def task(**updates):
    result: dict[str,Any] = dict(kind='checkpoint_navigation',start=[0,0,0],targets=[[1,0,0]],
        bounds=[[-1,5],[-.1,2],[-1,1]],min_health=12,timeout=10)
    result.update(updates)
    return result


class ActionTests(unittest.TestCase):
    def run_route(self,states,spec):
        values=iter(states);actions=[];log=io.StringIO()
        class Bridge:
            def sample(self,keys=None,hold=.15):
                if keys is not None:actions.append((keys,hold))
                return next(values)
        result=LiveTrials(Bridge(),log).route(spec,spec['targets'],BASELINE)
        return result,actions,log.getvalue()

    def test_action_list_is_bounded_and_checkpoint_only(self):
        for button in ['', 'A', 'X', 'A+X']:validate_tasks([task(target_buttons=[button])])
        for buttons in [None,[],['X','X'],'X',[None],['Plus'],['X~4'],['X\nA'],['Y'],['X+A']]:
            with self.subTest(buttons=buttons),self.assertRaises(ValueError):validate_tasks([task(target_buttons=buttons)])
        with self.assertRaises(ValueError):validate_tasks([task(kind='reciprocal_navigation',target_buttons=['X'])])
        with self.assertRaises(ValueError):validate_tasks([task(settle_targets=None)])
        with self.assertRaises(ValueError):validate_tasks([task(targets=[[1,0,0],[2,0,0]],target_buttons=['X',''],settle_targets=[False,True])])

    def test_walk_default_retains_single_settled_arrival(self):
        result,actions,_=self.run_route([state(),state(1),state(1)],task())
        self.assertTrue(result['success']);self.assertEqual(len(actions),2)
        self.assertNotIn('+',actions[0][0]);self.assertEqual(actions[-1],('',.25))

    def test_dash_button_is_combined_with_bounded_steering(self):
        result,actions,_=self.run_route([state(),state(1),state(1)],task(target_buttons=['A']))
        self.assertTrue(result['success']);self.assertTrue(actions[0][0].endswith('+A'))
        self.assertLessEqual(actions[0][1],.4);self.assertEqual(actions[-1],('',.25))

    def test_jump_requires_three_quiet_landing_observations(self):
        result,actions,log=self.run_route([state(),state(1),state(1),state(1),state(1)],task(target_buttons=['X']))
        self.assertTrue(result['success']);self.assertTrue(actions[0][0].endswith('+X'))
        self.assertEqual(actions[1:],[('',.25)]*3);self.assertEqual(log.count('"event": "settle"'),3)

    def test_transient_airborne_arrival_is_not_credited_or_rejumped(self):
        states=[state(),state(1,.1),state(1,.22),state(1,.12),state(1),state(1),state(1),state(1)]
        result,actions,_=self.run_route(states,task(target_buttons=['A+X']))
        self.assertTrue(result['success']);self.assertTrue(actions[0][0].endswith('+A+X'))
        self.assertEqual(actions[1:],[('',.25)]*6)

    def test_landing_below_terrain_floor_is_a_failed_trial(self):
        result,actions,_=self.run_route([state(),state(1),state(1,-.5)],task(target_buttons=['X']))
        self.assertFalse(result['success']);self.assertEqual(result['reached'],0)
        self.assertEqual(actions[-1],('',.25))

    def test_nonsettling_arrival_obeys_deadline_without_more_jump_input(self):
        states=itertools.chain([state(1)],itertools.cycle([state(1,.1),state(1,.2)]))
        clock=itertools.count(0,.25)
        with patch('evolution.live.time.monotonic',side_effect=lambda:next(clock)):
            result,actions,_=self.run_route(states,task(target_buttons=['X'],timeout=1))
        self.assertFalse(result['success']);self.assertEqual(result['reason'],'timeout')
        self.assertTrue(all(keys=='' for keys,_ in actions))


if __name__=='__main__':unittest.main()
