"""Many-gene native frame timelines and objective-aware whole-episode fitness.

The hot input loop runs in the host. Python compiles once per candidate and only
samples outcomes. Finite horizons are explicitly censored survival observations.
"""
import math
from typing import Any

FIELDS={'lx':(-1.,1.),'ly':(-1.,1.),'dash':(-1.,1.),'jump':(-1.,1.),
        'attack':(-1.,1.),'frames':(1.,30.)}


def gene_space(segments=128):
    if type(segments) is not int or not 1<=segments<=2048:
        raise ValueError('Timeline needs 1..2048 action segments')
    return {f'{i:04d}.{name}':limits for i in range(segments) for name,limits in FIELDS.items()}


def seed_genome(segments=128,direction=(0.,1.)):
    space=gene_space(segments)
    if len(direction)!=2 or not all(math.isfinite(x) for x in direction):
        raise ValueError('Invalid initial stick direction')
    length=max(1.,math.hypot(*direction));x,y=(v/length for v in direction)
    values=dict(lx=x,ly=y,dash=1.,jump=0.,attack=0.,frames=12.)
    return {name:values[name.split('.')[1]] for name in space}


def compile_timeline(genome,segments=128,horizon_frames=3600):
    """Repeat the chromosome to a fixed horizon; consecutive holds have no gaps."""
    space=gene_space(segments)
    if set(genome)!=set(space) or any(type(genome[k]) not in (int,float) or not math.isfinite(genome[k]) or not lo<=genome[k]<=hi for k,(lo,hi) in space.items()):
        raise ValueError('Invalid timeline genes')
    if type(horizon_frames) is not int or not 1<=horizon_frames<=1000000:
        raise ValueError('Invalid episode frame horizon')
    frame=0;index=0;lines=[]
    while frame<horizon_frames:
        prefix=f'{index%segments:04d}.';x=genome[prefix+'lx'];y=genome[prefix+'ly']
        norm=max(1.,math.hypot(x,y));keys=[f'L{x/norm:.4f}/{y/norm:.4f}']
        keys += [button for name,button in [('dash','A'),('jump','X'),('attack','Y')] if genome[prefix+name]>0]
        hold=min(horizon_frames-frame,max(1,round(genome[prefix+'frames'])))
        lines.append(f'f{frame}:'+ '+'.join(keys)+f'~f{hold}')
        frame+=hold;index+=1
    return '\n'.join(lines)+'\n'


class EpisodeScore:
    """Progress bins outrank passive survival; equal progress rewards longer life.

    Frames are relative to native replay start. A death count is distinct from a
    time-horizon censoring event. Reaching an objective need not end an attempt.
    """
    def __init__(self,start,target,horizon_frames,tolerance=.5):
        if any(len(p)!=3 or not all(math.isfinite(v) for v in p) for p in (start,target)) or type(horizon_frames) is not int or horizon_frames<1 or not 0<tolerance<=2:
            raise ValueError('Invalid episode objective')
        self.start=tuple(start);self.target=tuple(target);self.horizon=horizon_frames
        self.initial_distance=math.dist(start,target)
        if self.initial_distance<=tolerance:raise ValueError('Episode must start outside the objective tolerance')
        self.best_distance=self.initial_distance;self.last_position=self.start
        self.last_frame=0;self.health=None;self.damage=0;self.samples=0
        self.stable=0;self.success=False;self.tolerance=tolerance

    def observe(self,frame,position,health):
        if type(frame) is not int or frame<self.last_frame or len(position)!=3 or not all(math.isfinite(v) and abs(v)<1e6 for v in position) or type(health) is not int or not 0<=health<=2**32-1:
            raise ValueError('Invalid/reversed episode observation')
        self.best_distance=min(self.best_distance,math.dist(position,self.target))
        if self.health is not None:self.damage+=max(0,self.health-health)
        if frame>self.last_frame:
            self.stable=self.stable+1 if health>0 and math.dist(position,self.target)<=self.tolerance and math.dist(position,self.last_position)<=.05 else 0
            self.success=self.success or self.stable>=3
        self.last_frame=frame;self.last_position=tuple(position);self.health=health;self.samples+=1

    def terminal(self,frame,health):
        # A terminal ACK freezes time/health, not the asynchronously read position.
        if type(frame) is not int or frame<self.last_frame or type(health) is not int or not 0<=health<=2**32-1:
            raise ValueError('Invalid terminal episode evidence')
        if self.health is not None:self.damage+=max(0,self.health-health)
        self.last_frame=frame;self.health=health;self.samples+=1

    def finish(self,reason) -> dict[str,Any]:
        if reason not in ('death','horizon','objective') or not self.samples:
            raise ValueError('Only measured terminal episodes receive fitness')
        if reason=='death' and self.health!=0:raise ValueError('Death requires observed zero health')
        if reason=='objective' and not self.success:raise ValueError('Objective was not stably observed')
        progress=max(0.,min(1.,1-self.best_distance/self.initial_distance))
        progress_bin=100 if self.success else min(99,int(progress*100))
        alive=min(self.last_frame,self.horizon)
        return dict(success=self.success,reason=reason,score=progress_bin*(self.horizon+1)+alive,
                    progress=progress,progress_bin=progress_bin,best_distance=self.best_distance,
                    alive_frames=alive,damage=self.damage,health=self.health,samples=self.samples,
                    censored=reason=='horizon',position=self.last_position)
