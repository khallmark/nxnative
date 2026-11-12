"""nxnative DM_CTL adapter: validated memory reads and bounded ordinary input."""
import fcntl
import hashlib
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
        validate_ui(config)
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
                           health=hp,max_health=maximum,hazard=result.get('hazard'),reads=result)
            return self.last
        finally:
            for p in paths.values():
                p.unlink(missing_ok=True)

    def unpause(self):
        if self.running:
            raise TrialStopped('Already running')
        if self.config.get('ui'):
            self.wait_ui('pause')
        # Without configured UI probes, the caller must visually confirm pause.
        # Mark before dispatch so an uncertain response still triggers cleanup.
        self.running=True
        self.sample('Plus',.15)
        self.sample('',.3)
        if self.config.get('ui'):
            self.wait_ui('gameplay')

    def wait_ui(self,name,timeout=3.):
        deadline=time.monotonic()+timeout
        while True:
            state=self.sample()
            if ui_matches(self.config,state,name):
                return state
            if time.monotonic()>=deadline:
                raise TrialStopped('Expected UI state: '+name)
            time.sleep(.1)

    def pause(self):
        if self.running:
            if self.config.get('ui'):
                state=self.sample()
                if ui_matches(self.config,state,'gameplay'):
                    self.sample('Plus',.15)
                    self.wait_ui('pause')
            elif self.last is not None and self.last['health']>0:
                self.send('Plus~0.15')
            self.running=False

    def close(self):
        try:
            self.pause()
        finally:
            self.lock.close()



def number(value):
    return int(value,0) if isinstance(value,str) else value


def ui_matches(config,state,name):
    """Every named screen is an AND of validated integer telemetry predicates."""
    for rule in config['ui'][name]:
        value=state['reads'][rule['read']][rule.get('index',0)]
        if 'mask' in rule:
            value &= number(rule['mask'])
        if value!=number(rule['equals']):
            return False
    return True


def validate_ui(config):
    ui=config.get('ui')
    if ui is None:
        if config.get('recovery'):
            raise ValueError('Checkpoint recovery requires UI observations')
        return
    if not isinstance(ui,dict) or not {'pause','gameplay'}<=ui.keys():
        raise ValueError('UI needs pause and gameplay predicates')
    for rules in ui.values():
        if not isinstance(rules,list) or not rules:
            raise ValueError('UI predicates cannot be empty')
        for rule in rules:
            if not isinstance(rule,dict) or rule.get('read') not in config['reads']:
                raise ValueError('UI predicate references an unknown read')
            spec=config['reads'][rule['read']]
            values=struct.unpack('<'+spec['format'],bytes(struct.calcsize('<'+spec['format'])))
            index=rule.get('index',0)
            if not isinstance(index,int) or not 0<=index<len(values) or not isinstance(values[index],int):
                raise ValueError('UI predicates require an integer read/index')
            if not isinstance(number(rule.get('equals')),int) or ('mask' in rule and not isinstance(number(rule['mask']),int)):
                raise ValueError('UI comparison must be an integer')
    recovery=config.get('recovery')
    if recovery is None:
        return
    if not isinstance(recovery,dict) or not recovery.get('files') or not recovery.get('steps'):
        raise ValueError('Recovery needs save hashes and observed input steps')
    for path,digest in recovery['files'].items():
        if not Path(path).is_absolute() or not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('Recovery save hashes must be absolute paths and SHA256')
    for step in recovery['steps']:
        if step.get('keys') not in ('Plus','R','L','LUp','LDown','A','B','') or step.get('before') not in ui or step.get('after') not in ui:
            raise ValueError('Each recovery step needs bounded menu input and UI guards')
        for key,default,lo,hi in [('hold',.15,.05,.5),('settle',.3,0,2),('timeout',3.,.1,60)]:
            value=step.get(key,default)
            if not isinstance(value,(int,float)) or not math.isfinite(value) or not lo<=value<=hi:
                raise ValueError('Invalid recovery '+key)
    if recovery['steps'][0]['before']!='gameplay' or recovery['steps'][-1]['after']!='gameplay':
        raise ValueError('Recovery must begin and finish in observed gameplay')


class Checkpoint:
    """Replay a verified normal-game Load flow; never write game memory or saves."""
    def __init__(self,bridge,log):
        self.bridge=bridge;self.log=log
        self.config=bridge.config['recovery']

    def verify_files(self):
        for path,expected in self.config['files'].items():
            try:
                actual=hashlib.sha256(Path(path).read_bytes()).hexdigest()
            except OSError as exc:
                raise TrialStopped('Cannot read checkpoint save: '+path) from exc
            if actual!=expected:
                raise TrialStopped('Checkpoint save changed: '+path)

    def restore(self,task):
        self.verify_files()
        for index,step in enumerate(self.config['steps']):
            state=self.bridge.wait_ui(step['before'])
            self.bridge.sample(step['keys'],step.get('hold',.15))
            # These are measured menu animation intervals, not retries of input.
            time.sleep(step.get('settle',.3))
            state=self.bridge.wait_ui(step['after'],step.get('timeout',3.))
            self.log.write(json.dumps(dict(event='recovery_step',step=index,state=state))+'\n')
            self.log.flush()
        self.verify_files()
        state=self.bridge.sample('',.3)
        if not ui_matches(self.bridge.config,state,'gameplay') or not in_bounds(state,task) or state['health']<task['min_health'] or distance(state['position'],task['start'])>task.get('start_tolerance',.3):
            raise TrialStopped('Loaded checkpoint does not match the declared start/health')
        self.log.write(json.dumps(dict(event='checkpoint_restored',state=state))+'\n')
        self.log.flush()
        return state


def validate_tasks(tasks):
    """Reject incomplete safeguards before opening the live control channel."""
    def vector(value):
        return isinstance(value,list) and len(value)==3 and all(isinstance(v,(int,float)) and math.isfinite(v) for v in value)
    if not isinstance(tasks,list) or not tasks:
        raise ValueError('Curriculum must be a nonempty task list')
    for task in tasks:
        if not isinstance(task,dict) or task.get('kind') not in ('reciprocal_navigation','checkpoint_navigation'):
            raise ValueError('Unsupported navigation task kind')
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
        self.checkpoint=Checkpoint(bridge,log) if getattr(bridge,'config',{}).get('recovery') else None

    def route(self,task,targets,genome,reset=False) -> dict[str, Any]:
        state=self.bridge.sample(); initial=state; previous=None
        start=time.monotonic(); visited=0; steps=0
        deadline=start+task['timeout']
        def failed(reason,target):
            if task.get('kind')!='checkpoint_navigation':
                raise TrialStopped(reason)
            return dict(success=False,reason=reason,elapsed=time.monotonic()-start,reached=visited,
                        error=distance(state['position'],target),steps=steps,state=state)
        for target in targets:
            last_moving=time.monotonic(); anchor=state['position']
            while True:
                if getattr(self.bridge,'config',{}).get('ui') and not ui_matches(self.bridge.config,state,'gameplay'):
                    raise TrialStopped('Gameplay interrupted by an unrecognized UI state')
                if distance(state['position'],target)<=task.get('tolerance',.25):
                    # A released stick does not stop momentum immediately. Require
                    # settled arrival, otherwise the next episode starts elsewhere.
                    state=self.bridge.sample('',.25)
                    if getattr(self.bridge,'config',{}).get('ui') and not ui_matches(self.bridge.config,state,'gameplay'):
                        raise TrialStopped('Gameplay interrupted while settling')
                    if state['health']<initial['health'] or not in_bounds(state,task):
                        return failed('Unsafe state while settling at target',target)
                    if distance(state['position'],target)<=task.get('tolerance',.25):
                        break
                if not in_bounds(state,task):
                    return failed('Left the validated 3D training bounds',target)
                if state['health']<initial['health'] or state['health']<task['min_health']:
                    return failed('Damage/low health',target)
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
                    return failed('Damage or unsafe terrain transition',target)
                previous=before
            visited+=1
        return dict(success=True,reason='arrived',elapsed=time.monotonic()-start,reached=visited,
                    error=distance(state['position'],targets[-1]),steps=steps,state=state)

    def evaluate(self,task,genome):
        state=self.bridge.sample()
        use_checkpoint=task.get('kind')=='checkpoint_navigation'
        if use_checkpoint:
            if self.checkpoint is None:
                raise TrialStopped('Checkpoint curriculum requires recovery configuration')
            self.checkpoint.verify_files()
            if distance(state['position'],task['start'])>task.get('start_tolerance',.3) or state['health']<task['min_health']:
                state=self.checkpoint.restore(task)
        if distance(state['position'],task['start'])>task.get('start_tolerance',.3):
            raise TrialStopped('Not at the declared episode start; refusing an uncontrolled reset')
        if not in_bounds(state,task) or state['health']<task['min_health']:
            raise TrialStopped('Episode preconditions failed')
        result=self.route(task,task['targets'],genome)
        result['score']=(100 if result['success'] else -100)+10*result['reached']-result['elapsed']-5*result['error']
        if use_checkpoint:
            assert self.checkpoint is not None
            try:
                self.checkpoint.restore(task)
            except (TrialStopped,OSError) as exc:
                raise TrialStopped(str(exc),result) from exc
            return result
        # The curriculum explicitly declares reciprocal paths. For a failed
        # segment, reverse only completed segments, never unvisited waypoints.
        completed=task['targets'][:result['reached']]
        reverse=list(reversed(completed[:-1] if result['success'] else completed))+[task['start']]
        reset=self.route(task,reverse,BASELINE,reset=True)
        if not reset['success']:
            raise TrialStopped('Could not restore episode start: '+json.dumps(reset))
        return result
