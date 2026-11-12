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
from .actors import ActorReader, ActorUnavailable


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
        self.actors=None
        if config.get('actor_discovery'):
            if not config.get('ui') or 'hazard' in config['reads']:
                raise ValueError('Dynamic actors require UI probes and cannot duplicate fixed hazard reads')
            self.actors=ActorReader(config['actor_discovery'],self.read_memory)
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

    def read_memory(self,ranges,commands=()):
        if len(ranges)>4096 or sum(ranges.values())>128*1024*1024 or any(not 0<length<=65536 or not 0<=address<2**64-length for address,length in ranges.items()):
            raise ValueError('Invalid memory read batch')
        paths={address:self.root/(uuid.uuid4().hex+'.bin') for address in ranges}
        try:
            self.send(*commands,*(f'memdump {address:x} {ranges[address]} {path}' for address,path in paths.items()))
            deadline=time.monotonic()+5.
            while not all(path.exists() and path.stat().st_size==ranges[address] for address,path in paths.items()):
                if time.monotonic()>deadline:raise TrialStopped('Memory telemetry timed out')
                time.sleep(.005)
            return {address:path.read_bytes() for address,path in paths.items()}
        finally:
            for path in paths.values():path.unlink(missing_ok=True)

    def with_actors(self,state,data=None):
        if self.actors is None:return state
        observed=self.actors.observe(data)
        self.last={**state,'actors':observed,'hazard':observed['hazard']['position'] if 'hazard' in observed else None,
                   'time':time.monotonic()}
        return self.last

    def wait_actors(self,timeout=5.):
        deadline=time.monotonic()+timeout
        while True:
            state=self.sample(actors=False)
            if not ui_matches(self.config,state,'gameplay'):raise TrialStopped('Gameplay ended while discovering actors')
            try:return self.with_actors(state)
            except ActorUnavailable as exc:
                if time.monotonic()>=deadline:raise TrialStopped(str(exc)) from exc
                time.sleep(.1)

    def sample(self,keys=None,hold=.15,actors=True):
        if keys is not None and (not .05<=hold<=.5 or '\n' in keys or '~' in keys):
            raise ValueError('Input must be a single bounded press')
        # DM_CTL polls the control file every 100 ms. Read cached actor identities
        # in the same request as player/UI state to avoid a second idle poll.
        layout={name:(int(spec['address'],0),'<'+spec['format']) for name,spec in self.config['reads'].items()}
        actor_reads=self.actors.current_reads() if actors and self.actors is not None else {}
        ranges=dict(actor_reads)
        for address,fmt in layout.values():
            ranges[address]=max(ranges.get(address,0),struct.calcsize(fmt))
        commands=() if keys is None else (f'{keys}~{hold:.4f}',)
        data=self.read_memory(ranges,commands)
        result={name:struct.unpack(fmt,data[address][:struct.calcsize(fmt)]) for name,(address,fmt) in layout.items()}
        for name,expected in self.config['identity'].items():
            if list(result[name])!=[int(v,0) if isinstance(v,str) else v for v in expected]:
                raise TrialStopped(f'{name} identity changed; rediscover addresses')
        hp,maximum=result['health']
        values=result['position']+result['camera_rotation']+result.get('hazard',())
        if not all(math.isfinite(v) and abs(v)<1e6 for v in values) or not 0<=hp<=maximum<=80 or not maximum:
            raise TrialStopped('Invalid position/health telemetry')
        self.last=dict(time=time.monotonic(),position=result['position'],camera_rotation=result['camera_rotation'],
                       health=hp,max_health=maximum,hazard=result.get('hazard'),reads=result)
        if self.actors is not None:
            if not ui_matches(self.config,self.last,'gameplay'):
                self.actors.invalidate()
            elif actors:
                try:return self.with_actors(self.last,data if actor_reads else None)
                except ActorUnavailable as exc:raise TrialStopped(str(exc)) from exc
        return self.last

    def unpause(self):
        if self.running:
            raise TrialStopped('Already running')
        if self.config.get('ui'):
            self.wait_ui('pause')
        # Without configured UI probes, the caller must visually confirm pause.
        # Mark before dispatch so an uncertain response still triggers cleanup.
        self.running=True
        self.sample('Plus',.15,actors=False)
        self.sample('',.3,actors=False)
        if self.config.get('ui'):
            self.wait_ui('gameplay')
            if self.actors is not None:self.wait_actors()

    def wait_ui(self,name,timeout=3.):
        deadline=time.monotonic()+timeout
        while True:
            state=self.sample(actors=False)
            if ui_matches(self.config,state,name):
                return state
            if time.monotonic()>=deadline:
                raise TrialStopped('Expected UI state: '+name)
            time.sleep(.1)

    def pause(self):
        if self.running:
            if self.config.get('ui'):
                state=self.sample(actors=False)
                if ui_matches(self.config,state,'gameplay'):
                    guarded_pause(self)
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
    # Some games leave the HUD visible through death and the Game Over menu.
    if name=='gameplay' and state.get('health',1)==0:return False
    for rule in config['ui'][name]:
        value=state['reads'][rule['read']][rule.get('index',0)]
        if 'mask' in rule:
            value &= number(rule['mask'])
        if value!=number(rule['equals']):
            return False
    return True


def guarded_pause(bridge,timeout=3.,hold=.15):
    """Damage stagger can ignore Plus; retry only in positively observed gameplay."""
    deadline=time.monotonic()+timeout;next_press=0.
    while True:
        state=bridge.sample(actors=False)
        gameplay=ui_matches(bridge.config,state,'gameplay')
        if ui_matches(bridge.config,state,'pause') and not gameplay:return state
        if state['health']<=0:raise TrialStopped('Cannot pause during death')
        now=time.monotonic()
        if now>=deadline:raise TrialStopped('Pause was not observed before timeout')
        if gameplay and now>=next_press:
            bridge.sample('Plus',hold,actors=False)
            next_press=time.monotonic()+.5
        else:
            time.sleep(.05)


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
    for step in recovery['steps']+recovery.get('death_steps',[]):
        if step.get('keys') not in ('Plus','R','L','LUp','LDown','A','B','') or step.get('before') not in ui or step.get('after') not in ui:
            raise ValueError('Each recovery step needs bounded menu input and UI guards')
        for key,default,lo,hi in [('hold',.15,.05,.5),('settle',.3,0,2),('timeout',3.,.1,60)]:
            value=step.get(key,default)
            if not isinstance(value,(int,float)) or not math.isfinite(value) or not lo<=value<=hi:
                raise ValueError('Invalid recovery '+key)
    if recovery['steps'][0]['before']!='gameplay' or recovery['steps'][-1]['after']!='gameplay':
        raise ValueError('Recovery must begin and finish in observed gameplay')
    death=recovery.get('death_steps')
    if death is not None and (not death or death[0]['before']!='game_over' or death[-1]['after']!='gameplay'):
        raise ValueError('Death recovery must begin at observed game_over and finish in gameplay')
    mutable=recovery.get('mutable_after_death',{})
    if not isinstance(mutable,dict) or (mutable and not death) or any(path not in recovery['files'] or type(limit) is not int or not 1<=limit<=16*1024*1024 for path,limit in mutable.items()):
        raise ValueError('Death metadata needs known save paths and bounded byte limits')


class Checkpoint:
    """Replay a verified normal-game Load flow; never write game memory or saves."""
    def __init__(self,bridge,log):
        self.bridge=bridge;self.log=log
        self.config=bridge.config['recovery']
        self.expected=dict(self.config['files'])
        path=bridge.config.get('checkpoint_state')
        self.state_path=Path(path) if path else None
        if self.state_path is not None and not self.state_path.is_absolute():raise ValueError('Checkpoint state path must be absolute')
        if self.state_path is not None and self.state_path.exists():
            saved=json.loads(self.state_path.read_text());updates=saved.get('expected',{})
            if saved.get('files')!=self.config['files'] or saved.get('mutable')!=self.config.get('mutable_after_death',{}) or not isinstance(updates,dict) or any(path not in self.config.get('mutable_after_death',{}) or not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value) for path,value in updates.items()):
                raise TrialStopped('Checkpoint metadata manifest does not match this experiment')
            for path,digest in updates.items():
                if digest==self.expected[path]:continue
                evidence=self.bridge.root/'death-metadata'/(Path(path).name+'.'+digest+'.bin')
                try:
                    with evidence.open('rb') as source:data=source.read(self.config['mutable_after_death'][path]+1)
                except OSError as exc:raise TrialStopped('Missing accepted death metadata evidence') from exc
                if len(data)>self.config['mutable_after_death'][path] or hashlib.sha256(data).hexdigest()!=digest:
                    raise TrialStopped('Accepted death metadata evidence changed')
            self.expected.update(updates)

    def verify_files(self,ignore=()):
        for path,expected in self.expected.items():
            if path in ignore:continue
            try:
                actual=hashlib.sha256(Path(path).read_bytes()).hexdigest()
            except OSError as exc:
                raise TrialStopped('Cannot read checkpoint save: '+path) from exc
            if actual!=expected:
                raise TrialStopped('Checkpoint save changed: '+path)

    def validate_anchor(self,state,task):
        if not ui_matches(self.bridge.config,state,'gameplay') or not in_bounds(state,task) or state['health']<task.get('start_health',task['min_health']) or distance(state['position'],task['start'])>task.get('start_tolerance',.3):
            raise TrialStopped('Loaded checkpoint does not match the declared start/health')

    def accept_death_metadata(self,state,task):
        # Only a calibrated normal-game death transition may refresh these hashes.
        # Progress/caption files remain strict unless explicitly named by the profile.
        self.validate_anchor(state,task)
        mutable=self.config.get('mutable_after_death',{})
        self.verify_files(ignore=mutable)
        updates={}
        for path,limit in mutable.items():
            with Path(path).open('rb') as source:data=source.read(limit+1)
            if not data or len(data)>limit:raise TrialStopped('Unexpected death metadata size: '+path)
            digest=hashlib.sha256(data).hexdigest()
            if digest!=self.expected[path]:
                archive=self.bridge.root/'death-metadata';archive.mkdir(exist_ok=True)
                evidence=archive/(Path(path).name+'.'+digest+'.bin');evidence.write_bytes(data)
                self.log.write(json.dumps(dict(event='death_metadata_changed',path=path,previous=self.expected[path],sha256=digest,size=len(data),evidence=str(evidence)))+'\n')
            updates[path]=digest
        self.log.flush();self.expected.update(updates)
        if self.state_path is not None and mutable:
            temporary=self.state_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(dict(files=self.config['files'],mutable=mutable,
                expected={path:self.expected[path] for path in mutable}),sort_keys=True)+'\n')
            temporary.replace(self.state_path)

    def restore(self,task):
        self.verify_files()
        state=self.bridge.sample(actors=False)
        steps=self.config['steps'];death_count=0
        if state['health']==0:
            if not self.config.get('death_steps'):
                raise TrialStopped('Death recovery has not been calibrated')
            death_count=len(self.config['death_steps']);steps=self.config['death_steps']+steps
        for index,step in enumerate(steps):
            state=self.bridge.wait_ui(step['before'],step.get('timeout',3.))
            if step['keys']=='Plus' and step['before']=='gameplay' and step['after']=='pause':
                state=guarded_pause(self.bridge,step.get('timeout',3.),step.get('hold',.15))
            else:
                self.bridge.sample(step['keys'],step.get('hold',.15),actors=False)
                # Other menu inputs are never retried on an uncertain result.
                getattr(self.bridge,'settle',time.sleep)(step.get('settle',.3))
                state=self.bridge.wait_ui(step['after'],step.get('timeout',3.))
            if death_count and index+1==death_count:self.accept_death_metadata(state,task)
            self.log.write(json.dumps(dict(event='recovery_step',step=index,state=state))+'\n')
            self.log.flush()
        self.verify_files()
        state=self.bridge.sample('',.3,actors=False)
        self.validate_anchor(state,task)
        if self.bridge.config.get('actor_discovery'):
            state=self.bridge.wait_actors()
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
        settle=task.get('settle_targets')
        if 'settle_targets' in task and (task['kind']!='checkpoint_navigation' or not isinstance(settle,list) or len(settle)!=len(targets) or any(type(v) is not bool for v in settle) or not settle[-1]):
            raise ValueError('Checkpoint settle_targets must match targets and settle at the final target')
        buttons=task.get('target_buttons')
        if 'target_buttons' in task:
            if task['kind']!='checkpoint_navigation' or not isinstance(buttons,list) or len(buttons)!=len(targets) or any(v not in ('','A','X','A+X') for v in buttons):
                raise ValueError('Checkpoint target_buttons must match targets and use empty, A, X, or A+X')
            if settle is not None and any('X' in button and not settle[i] for i,button in enumerate(buttons)):
                raise ValueError('Jump targets must settle before continuing')
        if any(not all(lo<=v<=hi for v,(lo,hi) in zip(point,bounds)) for point in [task['start']]+targets):
            raise ValueError('Start/target outside declared bounds')
        if not isinstance(task.get('min_health'),int) or not 1<=task['min_health']<=80:
            raise ValueError('Minimum health must be a positive integer <=80')
        damage=task.get('max_damage',0);start_health=task.get('start_health',task['min_health'])
        if type(damage) is not int or not 0<=damage<80 or (damage and task['kind']!='checkpoint_navigation'):
            raise ValueError('Damage allowance requires checkpoint navigation and an integer below80')
        if type(start_health) is not int or not task['min_health']<=start_health<=80:
            raise ValueError('Start health must be at least minimum health and at most80')
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
        state=self.bridge.sample();initial=state;previous=None;damage=0;lowest_health=state['health']
        start=time.monotonic();visited=0;steps=0;deadline=start+task['timeout']
        def observe(new_state):
            nonlocal state,damage,lowest_health
            damage+=max(0,state['health']-new_state['health'])
            state=new_state;lowest_health=min(lowest_health,state['health'])
        def result(success,reason,target):
            return dict(success=success,reason=reason,elapsed=time.monotonic()-start,reached=visited,
                        error=distance(state['position'],target),steps=steps,state=state,damage=damage,
                        initial_health=initial['health'],lowest_health=lowest_health)
        def unsafe():
            return not in_bounds(state,task) or state['health']<task['min_health'] or damage>task.get('max_damage',0)
        def failed(reason,target):
            if task.get('kind')!='checkpoint_navigation':raise TrialStopped(reason)
            return result(False,reason,target)
        for index,target in enumerate(targets):
            settle=reset or task.get('settle_targets',[True]*len(targets))[index]
            buttons='' if reset else task.get('target_buttons',['']*len(targets))[index]
            jumping='X' in buttons
            last_moving=time.monotonic();anchor=state['position']
            while True:
                if getattr(self.bridge,'config',{}).get('ui') and not ui_matches(self.bridge.config,state,'gameplay'):
                    raise TrialStopped('Gameplay interrupted by an unrecognized UI state')
                if unsafe():return failed('Damage allowance/health or terrain limit exceeded',target)
                if distance(state['position'],target)<=task.get('tolerance',.25):
                    if not settle:break
                    stable=True
                    for _ in range(3 if jumping else 1):
                        before_position=state['position'];observe(self.bridge.sample('',.25))
                        if getattr(self.bridge,'config',{}).get('ui') and not ui_matches(self.bridge.config,state,'gameplay'):
                            raise TrialStopped('Gameplay interrupted while settling')
                        if unsafe():return failed('Unsafe state while settling at target',target)
                        if jumping:
                            stable=stable and distance(before_position,state['position'])<=.05
                            self.log.write(json.dumps(dict(event='settle',target=target,action='',hold=.25,
                                damage=damage,stable=stable,**state))+'\n');self.log.flush()
                        if distance(state['position'],target)>task.get('tolerance',.25):
                            stable=False;break
                    if stable:break
                    if jumping and distance(state['position'],target)<=task.get('tolerance',.25):
                        if time.monotonic()>deadline:return result(False,'timeout',target)
                        # Keep releasing input while an apparent landing is still moving.
                        continue
                if time.monotonic()>deadline:return result(False,'timeout',target)
                if distance(anchor,state['position'])>.1:
                    anchor=state['position'];last_moving=time.monotonic()
                if time.monotonic()-last_moving>2.:return result(False,'stuck',target)
                decision=steer(state,previous,target,genome);before=state
                keys='+'.join(value for value in (decision.keys,buttons) if value)
                observe(self.bridge.sample(keys,decision.hold));steps+=1
                self.log.write(json.dumps(dict(reset=reset,target=target,action=keys,
                    hold=decision.hold,risk=decision.risk if math.isfinite(decision.risk) else None,
                    damage=damage,**state))+'\n');self.log.flush()
                if unsafe():return failed('Damage allowance/health or terrain limit exceeded',target)
                previous=before
            visited+=1
        return result(True,'arrived',targets[-1])

    def evaluate(self,task,genome):
        state=self.bridge.sample()
        use_checkpoint=task.get('kind')=='checkpoint_navigation'
        if use_checkpoint:
            if self.checkpoint is None:
                raise TrialStopped('Checkpoint curriculum requires recovery configuration')
            self.checkpoint.verify_files()
            if distance(state['position'],task['start'])>task.get('start_tolerance',.3) or state['health']<task.get('start_health',task['min_health']):
                state=self.checkpoint.restore(task)
        if distance(state['position'],task['start'])>task.get('start_tolerance',.3):
            raise TrialStopped('Not at the declared episode start; refusing an uncontrolled reset')
        if not in_bounds(state,task) or state['health']<task.get('start_health',task['min_health']):
            raise TrialStopped('Episode preconditions failed')
        result=self.route(task,task['targets'],genome)
        result['score']=(100 if result['success'] else -100)+10*result['reached']-result['elapsed']-5*result['error']-25*result.get('damage',0)
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
