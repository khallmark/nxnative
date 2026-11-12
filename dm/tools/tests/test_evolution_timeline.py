"""Frame-genome compilation and objective-aware survival scoring."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evolution.timeline import gene_space,seed_genome,compile_timeline,EpisodeScore


class TimelineTests(unittest.TestCase):
    def test_many_genes_compile_to_contiguous_frame_holds(self):
        genes=seed_genome(128,(0,1));self.assertEqual(len(genes),768)
        text=compile_timeline(genes,128,3600)
        lines=text.splitlines();end=0
        for line in lines:
            start,action=line.split(':');keys,hold=action.split('~f')
            self.assertEqual(int(start[1:]),end);self.assertIn('+A',keys)
            end+=int(hold)
        self.assertEqual(end,3600)
        self.assertNotIn('~0.',text)

    def test_short_horizon_truncates_last_hold_without_a_gap(self):
        g=seed_genome(2,(1,0));g['0000.frames']=7;g['0001.frames']=8;g['0001.jump']=1
        self.assertEqual(compile_timeline(g,2,10).splitlines(),['f0:L1.0000/0.0000+A~f7','f7:L1.0000/0.0000+A+X~f3'])

    def test_unknown_nonfinite_or_out_of_range_genes_are_rejected(self):
        original=seed_genome(2)
        for g in [{**original,'unknown':1},{**original,'0000.lx':float('nan')},{**original,'0000.frames':0}]:
            with self.assertRaises(ValueError):compile_timeline(g,2,10)
        for n in [0,3000]:
            with self.assertRaises(ValueError):gene_space(n)

    def test_progress_gates_survival_and_same_progress_rewards_longer_life(self):
        idle=EpisodeScore((0,0,0),(10,0,0),600)
        idle.observe(600,(0,0,0),12)
        moving=EpisodeScore((0,0,0),(10,0,0),600)
        moving.observe(10,(5,0,0),12);moving.observe(11,(5,0,0),0)
        self.assertGreater(moving.finish('death')['score'],idle.finish('horizon')['score'])
        long=EpisodeScore((0,0,0),(10,0,0),600);long.observe(100,(5,0,0),12)
        self.assertGreater(long.finish('horizon')['score'],moving.finish('death')['score'])
        self.assertEqual(moving.finish('death')['alive_frames'],11)

    def test_objective_requires_stable_observations_and_tracks_damage(self):
        s=EpisodeScore((0,0,0),(1,0,0),600,tolerance=.3)
        s.observe(1,(1,.2,0),12);s.observe(2,(1,.1,0),11);self.assertFalse(s.success)
        for frame in [3,4,5,6]:s.observe(frame,(1,0,0),11)
        self.assertTrue(s.success);self.assertEqual(s.finish('objective')['damage'],1)
        self.assertTrue(s.finish('objective')['success'])

    def test_invalid_or_reversed_observations_are_not_scored(self):
        s=EpisodeScore((0,0,0),(1,0,0),600);s.observe(5,(0,0,0),12)
        for frame,position,hp in [(4,(0,0,0),12),(6,(float('nan'),0,0),12),(6,(0,0,0),-1)]:
            with self.assertRaises(ValueError):s.observe(frame,position,hp)


if __name__=='__main__':unittest.main()
