"""Observe whole native attempts without interrupting them for Python decisions."""
import json
import math
import time
from .core import distance
from .live import Checkpoint,TrialStopped,ui_matches
from .timeline import EpisodeScore,compile_timeline,gene_space


def validate_episode(spec):
    if type(spec.get('stop_on_objective',False)) is not bool:raise ValueError('stop_on_objective must be a boolean')
    policy=spec.get('policy','timeline_v1')
    gene_space(spec.get('segments',128),policy)
    if policy=='target_feedback_v1':
        radius=spec.get('feedback_stop_radius',.1)
        if type(radius) not in (int,float) or not math.isfinite(radius) or not 0<=radius<=.2 or radius>=spec.get('tolerance',.5):
            raise ValueError('Feedback stop radius must be within 0..0.2 and below objective tolerance')
        target=spec['target']
        if not isinstance(target,(list,tuple)) or len(target)!=3 or any(type(v) not in (int,float) or not math.isfinite(v) or abs(v)>=1e6 for v in target):
            raise ValueError('Feedback needs a finite bounded XYZ target')
    elif 'feedback_stop_radius' in spec:raise ValueError('Feedback radius requires target_feedback_v1 policy')
    EpisodeScore(spec['start'],spec['target'],spec['horizon_frames'],spec.get('tolerance',.5))
    if spec['horizon_frames']>1000000:raise ValueError('Episode horizon too large')
    for name,default,lo,hi in [('poll_seconds',.05,.005,.5),('start_tolerance',.3,.01,2.),('stall_seconds',30.,1.,120.)]:
        value=spec.get(name,default)
        if type(value) not in (int,float) or not math.isfinite(value) or not lo<=value<=hi:raise ValueError('Invalid episode '+name)
    if type(spec.get('start_health')) is not int or not 1<=spec['start_health']<=80:raise ValueError('Invalid episode start health')


class NativeUIInterruption(TrialStopped):
    """Unscored living UI evidence, safe to hand off only after cleanup succeeds."""
    def __init__(self,evidence):
        super().__init__('Living UI interrupted native episode')
        self.evidence=evidence


class NativeEpisodes:
    def __init__(self,bridge,spec,log,reset_backend=None):
        validate_episode(spec)
        self.bridge=bridge;self.spec=spec;self.log=log;self.reset_backend=reset_backend
        self.feedback=spec.get('policy','timeline_v1')=='target_feedback_v1'
        if self.feedback and (getattr(bridge,'world_xz',None) is None or getattr(bridge,'position_xyz',None) is None):
            raise ValueError('Target feedback requires guarded position and world-camera bindings')
        # The UI mirror can lag the authoritative health store used by a native
        # lethal-damage callback. Keep scoring telemetry and guard binding separate.
        address=bridge.config.get('native_guard_health_address',bridge.config['reads']['health']['address'])
        try:
            if not isinstance(address,str) or not address.startswith('0x'):raise ValueError
            self.health_address=int(address,16)
            if not 0<self.health_address<=2**64-5 or self.health_address%4:raise ValueError
        except ValueError as exc:raise ValueError('Invalid native health guard address') from exc
        self.checkpoint=Checkpoint(bridge,log,reset_backend)
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
        compiled=time.perf_counter();timeline=compile_timeline(genome,s.get('segments',128),s['horizon_frames'],
            policy=s.get('policy','timeline_v1'),target=s['target'],stop_radius=s.get('feedback_stop_radius',.1));compile_seconds=time.perf_counter()-compiled
        started=time.perf_counter()
        try:status=b.start_replay(timeline,self.health_address,**({'target_feedback':True} if self.feedback else {}))
        except BaseException as error:
            # A missing/mismatched mode ACK can follow successful native dispatch.
            # Release owned input before the runner enters instrumentation cleanup.
            if getattr(b,'owns_replay',False):
                try:b.cancel_replay()
                except BaseException as cancel_error:error.add_note('Native start cancellation also failed: '+str(cancel_error))
            raise
        start_frame=status['start_frame'];end_frame=status['end_frame'];epoch=status['epoch']
        last_frame=start_frame;last_progress=started;max_skew=0;next_log=0;pending=None;interruption=None
        objective_elapsed=None;input_cancelled=False;terminal=None;elapsed=0.;primary=None

        def cancellation(row,minimum_frame):
            if (not isinstance(row,dict) or row.get('active') is not False or
                any(type(row.get(k)) is not int or not 0<=row[k]<2**64 for k in
                    ('epoch','start_frame','end_frame','current_frame','stop_frame','last_health')) or
                row['epoch']!=epoch or row['start_frame']!=start_frame or row['end_frame']!=end_frame or
                row['current_frame']<minimum_frame or not start_frame<=row['stop_frame']<=row['current_frame'] or
                row.get('reason') not in ('cancelled','death','completed') or row['last_health']>=2**32):
                raise TrialStopped('Native objective/input cancellation was not verified')
            reason=row['reason'];health=row['last_health']
            if (reason=='death' and health!=0) or (reason!='death' and health<=0):
                raise TrialStopped('Native cancellation health disagrees with terminal reason')
            if reason=='completed' and row['stop_frame']<end_frame:
                raise TrialStopped('Native cancellation horizon ended early')
            return 'death' if reason=='death' else ('horizon' if row['stop_frame']>=end_frame else 'objective')

        try:
            if self.reset_backend is not None:self.reset_backend.arm_episode(status,self.task,self.health_address)
            while True:
                if self.reset_backend is not None:self.reset_backend.poll_episode()
                state=b.sample(actors=False);observation=b.native_status;status=observation
                observed_end=observation.get('observed_frame_end')
                if type(observed_end) is not int or observed_end<observation['current_frame']:
                    raise TrialStopped('Invalid native observation frame bracket')
                # replay_observe snapshots status before reading memory. A lethal
                # callback can stop/heal the player inside that read bracket.
                status=b.rpc('replay_status')
                if any(row['epoch']!=epoch or row['start_frame']!=start_frame or row['end_frame']!=end_frame for row in (observation,status)):
                    raise TrialStopped('Native replay identity changed during observation')
                if status['current_frame']<observed_end:
                    raise TrialStopped('Native status precedes its observation bracket')
                max_skew=max(max_skew,observed_end-observation['current_frame'])
                now=time.perf_counter()
                if observed_end>last_frame:last_progress=now;last_frame=observed_end
                if now-last_progress>s.get('stall_seconds',30.):raise TrialStopped('Guest frame counter stopped advancing')
                terminal=None;health=state['health'];frame=observation['current_frame']
                if not status['active']:
                    if status['reason'] not in ('death','completed'):raise TrialStopped('Native replay stopped: '+status['reason'])
                    if type(status.get('stop_frame')) is not int or status['stop_frame']<start_frame or type(status.get('last_health')) is not int:
                        raise TrialStopped('Missing native terminal evidence')
                    terminal='death' if status['reason']=='death' else 'horizon'
                    health=status['last_health'];frame=status['stop_frame']
                    if terminal=='death' and health!=0:raise TrialStopped('Native death lacked zero health')
                    if terminal=='horizon' and frame<status['end_frame']:raise TrialStopped('Native horizon ended early')
                # Death evidence wins over a healed mirror/UI snapshot. A living
                # UI transition at the horizon is still unscored and needs review.
                if terminal!='death' and state['health']>0 and not ui_matches(b.config,state,'gameplay'):
                    interruption=NativeUIInterruption(dict(state=state,observation=dict(observation),
                        status=dict(status),timeline=timeline,native_epoch=epoch,
                        frame=max(0,observation['current_frame']-start_frame)))
                    self.emit('episode_ui_interrupted',**interruption.evidence,genome=dict(genome))
                    break
                relative=min(s['horizon_frames'],max(0,frame-start_frame))
                # Commit position only after a strictly later confirmed frame.
                # Equality stays pending: death can still land in that same frame
                # just after this status ACK. Keep at most the latest such sample.
                candidate=(observation['current_frame'],observed_end,state['position'],state['health']) if observation['active'] and state['health']>0 else None
                boundary=min(end_frame,status['stop_frame'] if terminal else status['current_frame'])
                previous=pending;pending=None
                for measured in (previous,candidate):
                    if measured is None:continue
                    measured_frame,measured_end,position,measured_health=measured
                    if measured_end<boundary:
                        scorer.observe(max(0,measured_frame-start_frame),position,measured_health)
                    elif not terminal and measured_end<end_frame:pending=measured
                if scorer.success and objective_elapsed is None:
                    objective_elapsed=time.perf_counter()-started
                    self.emit('objective_confirmed',frame=scorer.objective_frame,elapsed=objective_elapsed,
                        native_epoch=epoch,stop_on_objective=s.get('stop_on_objective',False))
                if not terminal and s.get('stop_on_objective',False) and scorer.success:
                    # Keep lethal recovery armed across cancellation. An already
                    # latched death/horizon must survive the cancel ACK unchanged.
                    before_cancel=status
                    cancelled=b.cancel_replay()
                    terminal=cancellation(cancelled,before_cancel['current_frame'])
                    status=cancelled;input_cancelled=True
                    state=b.sample(actors=False);after=b.native_status
                    cancellation(after,status['current_frame'])
                    if (not isinstance(after,dict) or after.get('active') is not False or
                        any(after.get(k)!=status[k] for k in ('epoch','start_frame','end_frame','stop_frame','reason','last_health')) or
                        type(after.get('current_frame')) is not int or after['current_frame']<status['current_frame'] or
                        type(after.get('observed_frame_end')) is not int or after['observed_frame_end']<after['current_frame']):
                        raise TrialStopped('Native replay changed after objective cancellation')
                    if terminal!='death':
                        if state['health']<=0:raise TrialStopped('Unlatched death after objective cancellation')
                        if not ui_matches(b.config,state,'gameplay'):
                            interruption=NativeUIInterruption(dict(state=state,observation=dict(after),
                                status=dict(status),timeline=timeline,native_epoch=epoch,
                                frame=max(0,after['current_frame']-start_frame)))
                            self.emit('episode_ui_interrupted',**interruption.evidence,genome=dict(genome))
                            break
                    health=status['last_health'];relative=min(s['horizon_frames'],status['stop_frame']-start_frame)
                if terminal:scorer.terminal(relative,health)
                if relative>=next_log or terminal:
                    self.emit('episode_sample',frame=relative,position=state['position'],health=health,active=status['active'],reason=status['reason'])
                    next_log=relative+60
                if terminal:
                    elapsed=time.perf_counter()-started
                    break
                time.sleep(s.get('poll_seconds',.05))
        except BaseException as error:
            primary=error
            raise
        finally:
            cleanup_errors=[]
            try:
                if self.reset_backend is not None:self.reset_backend.finish_episode(status)
            except BaseException as error:cleanup_errors.append(('lethal disarm',error,error.__traceback__))
            try:
                cancelled=status if input_cancelled else b.cancel_replay()
                if terminal and interruption is None and primary is None:
                    cancellation(cancelled,status['current_frame'])
                    if any(cancelled.get(k)!=status.get(k) for k in ('reason','stop_frame','last_health')):
                        raise TrialStopped('Native terminal changed during input cleanup')
                if interruption is not None:
                    if not isinstance(cancelled,dict) or cancelled.get('active') is not False or cancelled.get('epoch')!=epoch or cancelled.get('reason') not in ('cancelled','completed'):
                        raise TrialStopped('UI interruption input cancellation was not verified')
                    interruption.evidence['cancel_status']=dict(cancelled)
            except BaseException as error:cleanup_errors.append(('input cancellation',error,error.__traceback__))
            if cleanup_errors:
                failure=primary if primary is not None else cleanup_errors[0][1]
                for operation,error,_ in cleanup_errors:
                    if error is not failure:failure.add_note(operation+' also failed: '+str(error))
                if primary is None:raise failure.with_traceback(cleanup_errors[0][2])
        if interruption is not None:raise interruption
        # Fitness is created only after lethal disarm and verified input cleanup.
        return dict(scorer.finish(terminal,stop_on_objective=s.get('stop_on_objective',False)),elapsed=elapsed,compile_seconds=compile_seconds,
                    frames_per_wall_second=scorer.last_frame/max(elapsed,1e-9),
                    observation_max_frame_skew=max_skew,native_epoch=status['epoch'],
                    objective_elapsed=objective_elapsed,terminal_status=dict(cancelled))
