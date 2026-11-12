"""nxnative DM_CTL adapter: validated memory reads and bounded ordinary input."""
import fcntl
import json
import math
import os
from pathlib import Path
import struct
import time
import uuid
from typing import Any
from .core import BASELINE, distance, steer


class TrialStopped(RuntimeError):
    def __init__(self, message, result=None):
        super().__init__(message)
        self.result=result


class Bridge:
    def __init__(self, config):
        self.config=config
        self.root=Path(config['scratch']).resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.control=Path(config['control']).resolve()
        if any(c.isspace() for c in str(self.root)):
            raise ValueError('DM_CTL scratch path must not contain whitespace')
        self.lock=(self.control.parent/(self.control.name+'.evolution.lock')).open('a')
        fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.running=False
        self.last: dict[str, Any] | None = None

    def send(self,*lines):
        # Never create/replay a control file belonging to a missing host.
        os.kill(self.config['pid'],0)
        if not self.control.is_file():
            raise TrialStopped('The live control file is missing')
        if any('\n' in x or '\r' in x or len(x)>1000 for x in lines):
            raise ValueError('Invalid DM_CTL line')
        with self.control.open('a') as f:
            f.write('\n'.join(lines)+'\n')

    def sample(self,keys=None,hold=.15):
        if keys is not None and (not .05<=hold<=.5 or '\n' in keys or '~' in keys):
            raise ValueError('Input must be a single bounded press')
        specs=self.config['reads']; paths={k:self.root/(uuid.uuid4().hex+'.bin') for k in specs}
        lines=[] if keys is None else [f'{keys}~{hold:.4f}']
        for name,spec in specs.items():
            fmt='<'+spec['format']; size=struct.calcsize(fmt)
            if size>65536:
                raise ValueError('Telemetry read too large')
            lines.append(f"memdump {int(spec['address'],0):x} {size} {paths[name]}")
        self.send(*lines)
        deadline=time.monotonic()+hold+4.
        try:
            while not all(p.exists() and p.stat().st_size==struct.calcsize('<'+specs[k]['format']) for k,p in paths.items()):
                if time.monotonic()>deadline:
                    raise TrialStopped('Telemetry timed out; no more actions sent')
                time.sleep(.005)
            result={k:struct.unpack('<'+specs[k]['format'],p.read_bytes()) for k,p in paths.items()}
            for name,expected in self.config['identity'].items():
                if list(result[name])!=[int(v,0) if isinstance(v,str) else v for v in expected]:
                    raise TrialStopped(f'{name} identity changed; rediscover addresses')
            hp,maximum=result['health']
            values=result['position']+result['camera_rotation']+result.get('hazard',())
            if not all(math.isfinite(v) and abs(v)<1e6 for v in values) or not 0<=hp<=maximum<=80 or not maximum:
                raise TrialStopped('Invalid position/health telemetry')
            self.last=dict(time=time.monotonic(),position=result['position'],camera_rotation=result['camera_rotation'],
                           health=hp,max_health=maximum,hazard=result.get('hazard'))
            return self.last
        finally:
            for p in paths.values():
                p.unlink(missing_ok=True)

    def unpause(self):
        if self.running:
            raise TrialStopped('Already running')
        # Caller must have visually confirmed a normal pause menu before first use.
        # Mark before dispatch so an uncertain response still triggers cleanup.
        self.running=True
        self.sample('Plus',.15)
        self.sample('',.3)

    def pause(self):
        if self.running:
            if self.last is not None and self.last['health']>0:
                self.send('Plus~0.15')
            self.running=False

    def close(self):
        self.pause()
        self.lock.close()



def validate_tasks(tasks):
    """Reject incomplete safeguards before opening the live control channel."""
    def vector(value):
        return isinstance(value,list) and len(value)==3 and all(isinstance(v,(int,float)) and math.isfinite(v) for v in value)
    if not isinstance(tasks,list) or not tasks:
        raise ValueError('Curriculum must be a nonempty task list')
    for task in tasks:
        if not isinstance(task,dict) or task.get('kind')!='reciprocal_navigation':
            raise ValueError('Only reciprocal_navigation tasks are supported')
        bounds=task.get('bounds')
        if not isinstance(bounds,list) or len(bounds)!=3 or any(not isinstance(b,list) or len(b)!=2 or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in b) or b[0]>=b[1] for b in bounds):
            raise ValueError('Each task needs three finite, ordered bounds')
        targets=task.get('targets')
        if not vector(task.get('start')) or not isinstance(targets,list) or not targets or not all(vector(t) for t in targets):
            raise ValueError('Start and nonempty targets must be finite 3D coordinates')
        if any(not all(lo<=v<=hi for v,(lo,hi) in zip(point,bounds)) for point in [task['start']]+targets):
            raise ValueError('Start/target outside declared bounds')
        if not isinstance(task.get('min_health'),int) or not 1<=task['min_health']<=80:
            raise ValueError('Minimum health must be a positive integer <=80')
        for key,default,lo,hi in [('timeout',None,.1,300),('tolerance',.25,.01,1),('start_tolerance',.3,.01,1)]:
            value=task.get(key,default)
            if not isinstance(value,(int,float)) or not math.isfinite(value) or not lo<=value<=hi:
                raise ValueError('Invalid task '+key)
        if task['start']!=tasks[0]['start']:
            raise ValueError('Tasks must share the same start anchor')


def in_bounds(state, task):
    return all(lo<=v<=hi for v,(lo,hi) in zip(state['position'],task['bounds']))


class LiveTrials:
    def __init__(self,bridge,log):
        self.bridge=bridge; self.log=log

    def route(self,task,targets,genome,reset=False) -> dict[str, Any]:
        state=self.bridge.sample(); initial=state; previous=None
        start=time.monotonic(); visited=0; steps=0
        deadline=start+task['timeout']
        for target in targets:
            last_moving=time.monotonic(); anchor=state['position']
            while True:
                if distance(state['position'],target)<=task.get('tolerance',.25):
                    # A released stick does not stop momentum immediately. Require
                    # settled arrival, otherwise the next episode starts elsewhere.
                    state=self.bridge.sample('',.25)
                    if state['health']<initial['health'] or not in_bounds(state,task):
                        raise TrialStopped('Unsafe state while settling at target')
                    if distance(state['position'],target)<=task.get('tolerance',.25):
                        break
                if not in_bounds(state,task):
                    raise TrialStopped('Left the validated 3D training bounds')
                if state['health']<initial['health'] or state['health']<task['min_health']:
                    raise TrialStopped('Damage/low health: recovery required before more trials')
                if time.monotonic()>deadline:
                    return dict(success=False,reason='timeout',elapsed=time.monotonic()-start,
                                reached=visited,error=distance(state['position'],target),steps=steps,state=state)
                if distance(anchor,state['position'])>.1:
                    anchor=state['position'];last_moving=time.monotonic()
                if time.monotonic()-last_moving>2.:
                    return dict(success=False,reason='stuck',elapsed=time.monotonic()-start,
                                reached=visited,error=distance(state['position'],target),steps=steps,state=state)
                decision=steer(state,previous,target,genome)
                before=state
                state=self.bridge.sample(decision.keys,decision.hold)
                steps+=1
                self.log.write(json.dumps(dict(reset=reset,target=target,action=decision.keys,
                    hold=decision.hold,risk=decision.risk if math.isfinite(decision.risk) else None,**state))+'\n')
                self.log.flush()
                # Check even the final action: arrival cannot hide damage or an unsafe fall.
                if state['health']<initial['health'] or not in_bounds(state,task):
                    raise TrialStopped('Damage or unsafe terrain transition')
                previous=before
            visited+=1
        return dict(success=True,reason='arrived',elapsed=time.monotonic()-start,reached=visited,
                    error=distance(state['position'],targets[-1]),steps=steps,state=state)

    def evaluate(self,task,genome):
        state=self.bridge.sample()
        if distance(state['position'],task['start'])>task.get('start_tolerance',.3):
            raise TrialStopped('Not at the declared episode start; refusing an uncontrolled reset')
        if not in_bounds(state,task) or state['health']<task['min_health']:
            raise TrialStopped('Episode preconditions failed')
        result=self.route(task,task['targets'],genome)
        result['score']=(100 if result['success'] else -100)+10*result['reached']-result['elapsed']-5*result['error']
        # The curriculum explicitly declares reciprocal paths. For a failed
        # segment, reverse only completed segments, never unvisited waypoints.
        completed=task['targets'][:result['reached']]
        reverse=list(reversed(completed[:-1] if result['success'] else completed))+[task['start']]
        reset=self.route(task,reverse,BASELINE,reset=True)
        if not reset['success']:
            raise TrialStopped('Could not restore episode start: '+json.dumps(reset))
        return result
