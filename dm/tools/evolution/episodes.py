"""Observe whole native attempts without interrupting them for Python decisions."""
import json
import math
import time
from .core import distance
from .live import Checkpoint,TrialStopped,ui_matches
from .timeline import EpisodeScore,compile_timeline,gene_space


def validate_episode(spec):
    gene_space(spec.get('segments',128))
    EpisodeScore(spec['start'],spec['target'],spec['horizon_frames'],spec.get('tolerance',.5))
    if spec['horizon_frames']>1000000:raise ValueError('Episode horizon too large')
    for name,default,lo,hi in [('poll_seconds',.05,.005,.5),('start_tolerance',.3,.01,2.),('stall_seconds',30.,1.,120.)]:
        value=spec.get(name,default)
        if type(value) not in (int,float) or not math.isfinite(value) or not lo<=value<=hi:raise ValueError('Invalid episode '+name)
    if type(spec.get('start_health')) is not int or not 1<=spec['start_health']<=80:raise ValueError('Invalid episode start health')


class NativeEpisodes:
    def __init__(self,bridge,spec,log):
        validate_episode(spec)
        self.bridge=bridge;self.spec=spec;self.log=log
        self.checkpoint=Checkpoint(bridge,log)
        # Recovery verifies the exact anchor and health. These broad bounds only
        # reject impossible telemetry; episode damage and falling do not stop play.
        self.task=dict(start=spec['start'],start_tolerance=spec.get('start_tolerance',.3),
                       min_health=1,start_health=spec['start_health'],bounds=[[-999999,999999]]*3)

    def emit(self,event,**fields):
        self.log.write(json.dumps(dict(event=event,**fields))+'\n');self.log.flush()

    def restore(self):
        started=time.perf_counter();self.bridge.cancel_replay()
        state=self.checkpoint.restore(self.task)
        return state,time.perf_counter()-started

    def evaluate(self,genome):
        b=self.bridge;s=self.spec
        initial=b.sample(actors=False)
        if not ui_matches(b.config,initial,'gameplay') or initial['health']<s['start_health'] or distance(initial['position'],s['start'])>s.get('start_tolerance',.3):
            raise TrialStopped('Native episode is outside its verified start')
        scorer=EpisodeScore(initial['position'],s['target'],s['horizon_frames'],s.get('tolerance',.5))
        scorer.observe(0,initial['position'],initial['health'])
        compiled=time.perf_counter();timeline=compile_timeline(genome,s.get('segments',128),s['horizon_frames']);compile_seconds=time.perf_counter()-compiled
        started=time.perf_counter();status=b.start_replay(timeline,int(b.config['reads']['health']['address'],0))
        start_frame=status['start_frame'];last_frame=start_frame;last_progress=started;max_skew=0;next_log=0
        try:
            while True:
                state=b.sample(actors=False);status=b.native_status
                observed_end=status.get('observed_frame_end')
                if type(observed_end) is not int or observed_end<status['current_frame']:
                    raise TrialStopped('Invalid native observation frame bracket')
                max_skew=max(max_skew,observed_end-status['current_frame'])
                now=time.perf_counter()
                if observed_end>last_frame:last_progress=now;last_frame=observed_end
                if now-last_progress>s.get('stall_seconds',30.):raise TrialStopped('Guest frame counter stopped advancing')
                terminal=None;health=state['health'];frame=status['current_frame']
                if not status['active']:
                    if status['reason'] not in ('death','completed'):raise TrialStopped('Native replay stopped: '+status['reason'])
                    if type(status.get('stop_frame')) is not int or status['stop_frame']<start_frame or type(status.get('last_health')) is not int:
                        raise TrialStopped('Missing native terminal evidence')
                    terminal='death' if status['reason']=='death' else 'horizon'
                    health=status['last_health'];frame=status['stop_frame']
                    if terminal=='death' and health!=0:raise TrialStopped('Native death lacked zero health')
                    if terminal=='horizon' and frame<status['end_frame']:raise TrialStopped('Native horizon ended early')
                elif health>0 and not ui_matches(b.config,state,'gameplay'):
                    raise TrialStopped('Unexpected UI interrupted native episode')
                relative=min(s['horizon_frames'],max(0,frame-start_frame))
                # The guard may fire while the asynchronous reads are in flight.
                # Wait for its terminal snapshot before counting death/survival.
                if terminal:
                    scorer.terminal(relative,health)
                elif health>0 and observed_end<status['end_frame']:
                    scorer.observe(relative,state['position'],health)
                if relative>=next_log or terminal:
                    self.emit('episode_sample',frame=relative,position=state['position'],health=health,active=status['active'],reason=status['reason'])
                    next_log=relative+60
                if terminal:
                    elapsed=time.perf_counter()-started
                    return dict(scorer.finish(terminal),elapsed=elapsed,compile_seconds=compile_seconds,
                                frames_per_wall_second=scorer.last_frame/max(elapsed,1e-9),
                                observation_max_frame_skew=max_skew,native_epoch=status['epoch'])
                time.sleep(s.get('poll_seconds',.05))
        finally:
            b.cancel_replay()
