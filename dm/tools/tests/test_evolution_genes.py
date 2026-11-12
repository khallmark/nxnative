"""Large-genome mutation and durable evaluation without per-candidate SQL polling."""
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evolution.core import Evolution,canonical


class ManyGeneTests(unittest.TestCase):
    def test_large_genome_resumes_exact_population_and_pending_trial(self):
        bounds={f'g{i:04d}':(-1.,1.) for i in range(768)}
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'ga.db'
            a=Evolution(path,[{'id':'objective'}],4,2,bounds=bounds,mutation_rate=.08,initial_sigma=.08)
            a.record(a.pending(),dict(score=2.,success=False))
            expected=canonical(a.state);pending=a.pending();a.db.close()
            b=Evolution(path,[{'id':'objective'}],4,2,bounds=bounds,mutation_rate=.08,initial_sigma=.08)
            self.addCleanup(b.db.close)
            self.assertEqual(canonical(b.state),expected);self.assertEqual(b.pending(),pending)
            self.assertEqual(len(b.state['candidates'][0]),768)
            self.assertTrue(any(v!=0 for v in b.state['candidates'][1].values()))
            statements=[];b.db.set_trace_callback(statements.append)
            for _ in range(100):self.assertEqual(b.pending(),pending)
            self.assertEqual(statements,[])
            while (key:=b.pending()) is not None:b.record(key,dict(score=float(key[1]),success=False))
            self.assertEqual(b.advance()['genome'],b.state['candidates'][0])
            self.assertEqual(b.pending(),(1,0,0,0))
            self.assertTrue(all(-1<=v<=1 for genome in b.state['candidates'] for v in genome.values()))

    def test_gene_schema_and_mutation_changes_invalidate_old_fitness(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'ga.db';bounds={'a':(-1.,1.),'b':(0.,2.)}
            a=Evolution(path,[{'id':'o'}],4,2,bounds=bounds);a.db.close()
            for kwargs in [dict(bounds={'a':(-2.,2.),'b':(0.,2.)}),dict(bounds=bounds,mutation_rate=.1)]:
                with self.assertRaises(ValueError):Evolution(path,[{'id':'o'}],4,2,**kwargs)

    def test_invalid_gene_space_and_mutation_settings_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            for kwargs in [dict(bounds={}),dict(bounds={'a':(2.,1.)}),dict(bounds={'a':(0.,float('inf'))}),
                           dict(mutation_rate=0),dict(mutation_rate=2),dict(mutation_sigma=-1),dict(initial_sigma=float('nan'))]:
                with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):Evolution(Path(td)/'ga.db',[{'id':'o'}],4,2,**kwargs)


if __name__=='__main__':unittest.main()
