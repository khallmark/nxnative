"""Persistent genetic search. No game data, addresses or input side effects."""
import hashlib
import json
import math
import random
import sqlite3
import statistics
from dataclasses import dataclass
from typing import Any

BOUNDS = {'step': (.10, .40), 'gain': (.4, 2.), 'lookahead': (.25, 1.2),
          'avoid': (0., 3.), 'side': (-1., 1.), 'brake': (.2, .9)}
BASELINE = dict(step=.3219230361491248, gain=1.1889556121468041,
                lookahead=.6, avoid=1.2, side=.5, brake=.6)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def distance(a, b):
    return math.dist(a, b)


def closest_approach(relative, velocity, horizon):
    vv = sum(v*v for v in velocity)
    t = max(0., min(horizon, -sum(p*v for p,v in zip(relative, velocity))/vv)) if vv else 0.
    return math.sqrt(sum((p+t*v)**2 for p,v in zip(relative, velocity)))


@dataclass
class Decision:
    keys: str
    hold: float
    risk: float


def steer(state, previous, target, genome):
    """Camera-relative feedback with a predictive repulsion/tangent component.

    Prediction is local, constant-velocity and intentionally NOT a terrain planner.
    Bounds, vertical arrival and damage checks belong to the episode supervisor.
    """
    p = state['position']; dx, dz = target[0]-p[0], target[2]-p[2]
    d = math.hypot(dx, dz)
    if d < 1e-6:
        return Decision('', .1, math.inf)
    ux, uz = dx/d, dz/d
    h = state.get('hazard'); risk = math.inf
    if h is not None and abs(h[1]-(p[1]+1.)) < 3.:
        vx = vz = 0.
        if previous and previous.get('hazard') is not None:
            dt = state['time']-previous['time']
            if .02 < dt < 2.:
                vx = (h[0]-previous['hazard'][0])/dt
                vz = (h[2]-previous['hazard'][2])/dt
                if math.hypot(vx,vz)>15:
                    raise ValueError('Hazard discontinuity; rediscover the scene')
        rel = (h[0]-p[0],h[2]-p[2])
        risk = closest_approach(rel, (vx-4*ux,vz-4*uz), genome['lookahead'])
        separation = math.hypot(*rel)
        if risk<4. and separation>1e-4:
            weight = genome['avoid']*(4.-risk)/4.
            ax,az = -rel[0]/separation,-rel[1]/separation
            ux += weight*(ax-genome['side']*az)
            uz += weight*(az+genome['side']*ax)
    n=math.hypot(ux,uz)
    if n<1e-5:
        return Decision('', .1, risk)
    ux,uz=ux/n,uz/n
    r=state['camera_rotation']; right=(r[0],r[6]); forward=(-r[2],-r[8])
    rn,fn=math.hypot(*right),math.hypot(*forward)
    if min(rn,fn)<.1:
        raise ValueError('Camera basis is degenerate')
    strength=min(1.,max(.3,d*genome['gain']))
    lx=max(-1.,min(1.,(ux*right[0]+uz*right[1])/rn*strength))
    ly=max(-1.,min(1.,(ux*forward[0]+uz*forward[1])/fn*strength))
    hold=min(genome['step'],max(.1,d/4*genome['brake']))
    return Decision(f'L{lx:.4f}/{ly:.4f}',hold,risk)


class Evolution:
    """Checkpoint every evaluation, including RNG and partially tested populations.

    All elites are re-evaluated each generation. Repeats and every curriculum task
    must finish before selection. A crash never turns a partial score into a winner.
    """
    def __init__(self, path, curriculum, population=8, repeats=2, seed=7401, implementation="v1", initial_genome=None):
        if population<4 or repeats<2 or not curriculum:
            raise ValueError('Need >=4 candidates, >=2 repeats and a curriculum')
        self.db=sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS trials (generation INTEGER, candidate INTEGER, task INTEGER,
          repeat INTEGER, result TEXT NOT NULL, PRIMARY KEY(generation,candidate,task,repeat));''')
        signature=hashlib.sha256(canonical(dict(curriculum=curriculum,population=population,repeats=repeats,implementation=implementation)).encode()).hexdigest()
        row=self.db.execute('SELECT value FROM state WHERE id=1').fetchone()
        self.rng=random.Random(seed)
        self.state: dict[str, Any]
        if row:
            self.state=json.loads(row[0])
            if self.state['signature']!=signature:
                self.db.close()
                raise ValueError('Curriculum/config changed: use a new database')
            def tuples(x):
                return tuple(map(tuples,x)) if isinstance(x,list) else x
            self.rng.setstate(tuples(self.state['rng']))
        else:
            initial=BASELINE if initial_genome is None else initial_genome
            if set(initial)!=set(BOUNDS) or any(not isinstance(initial[k],(int,float)) or not math.isfinite(initial[k]) or not lo<=initial[k]<=hi for k,(lo,hi) in BOUNDS.items()):
                self.db.close()
                raise ValueError('Seed genome does not match the current parameter bounds')
            candidates=[initial.copy()]+[self.mutate(initial,initial,.35) for _ in range(population-1)]
            self.state=dict(signature=signature,generation=0,candidates=candidates,champion=None)
            self.save()
            self.db.commit()
        self.tasks=curriculum; self.repeats=repeats

    def mutate(self,a,b,sigma=.12):
        return {k:max(lo,min(hi,self.rng.choice((a[k],b[k]))+self.rng.gauss(0,sigma*(hi-lo))))
                for k,(lo,hi) in BOUNDS.items()}

    def save(self):
        self.state['rng']=self.rng.getstate()
        self.db.execute('INSERT OR REPLACE INTO state VALUES (1,?)',(canonical(self.state),))

    def pending(self):
        g=self.state['generation']
        # Interleave candidates across repeats to reduce drift/order advantage.
        for rep in range(self.repeats):
            for task in range(len(self.tasks)):
                for candidate in range(len(self.state['candidates'])):
                    if not self.db.execute('SELECT 1 FROM trials WHERE generation=? AND candidate=? AND task=? AND repeat=?',(g,candidate,task,rep)).fetchone():
                        return g,candidate,task,rep
        return None

    def record(self,key,result):
        if not math.isfinite(result['score']):
            raise ValueError('Nonfinite fitness')
        result={**result,'genome':self.state['candidates'][key[1]].copy()}
        with self.db:
            self.db.execute('INSERT INTO trials VALUES (?,?,?,?,?)',(*key,canonical(result)))
            self.save()

    def advance(self):
        if self.pending() is not None:
            raise ValueError('Cannot select an incompletely evaluated generation')
        scores=[]
        for i in range(len(self.state['candidates'])):
            rows=[json.loads(x[0]) for x in self.db.execute('SELECT result FROM trials WHERE generation=? AND candidate=?',(self.state['generation'],i))]
            # Worst task/repeat matters; no easy-route average can hide a failure.
            fitness=min(r['score'] for r in rows)
            scores.append((all(r['success'] for r in rows),fitness,statistics.mean(r['score'] for r in rows),i))
        scores.sort(reverse=True)
        parents=[self.state['candidates'][x[3]] for x in scores[:max(2,len(scores)//3)]]
        best=scores[0]
        report=dict(generation=self.state['generation'],fitness=best[1],mean=best[2],
                    genome=parents[0],all_trials_successful=best[0])
        with self.db:
            self.state['champion']=report
            self.state['candidates']=parents[:2]+[self.mutate(self.rng.choice(parents),self.rng.choice(parents)) for _ in range(len(scores)-2)]
            self.state['generation']+=1
            self.save()
        return report
