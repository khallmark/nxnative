"""Durable UI/objective holds and bounded commands on the existing attachment."""
import hashlib
import json
import math
import os
from pathlib import Path
import time
import uuid
from typing import Any

from .live import TrialStopped,ui_matches
from .timeline import compile_timeline

_BUTTONS={'','A','B','X','Y','Plus','Minus','Up','Down','Left','Right','L','R','ZL','ZR'}


def _atomic(path,value):
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('x') as stream:
            json.dump(value,stream,allow_nan=False,indent=2);stream.write('\n')
            stream.flush();os.fsync(stream.fileno())
        temporary.replace(path)
        directory=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:temporary.unlink(missing_ok=True)


def _control_marker(config):
    if not config or 'control' not in config:return None
    control=Path(config['control']).resolve()
    return control.with_name(control.name+'.handoff.json')


def assert_no_pending_handoff(output,config=None):
    # Shutdown does not resolve a milestone. Bind the barrier to input ownership
    # too: a new output directory must not let the same host load old saves.
    marker=_control_marker(config)
    if (Path(output)/'handoff.json').exists() or marker is not None and marker.exists():
        raise TrialStopped('Unresolved UI handoff: preserve and explicitly resolve it before restoring this host')


def _backend_idle(backend):
    if backend is None:return
    if backend.failed or backend.uncertain or backend.closed or backend.armed:
        raise TrialStopped('UI handoff requires a healthy idle reset backend')
    if backend.session is None or backend.session.is_detached:
        raise TrialStopped('UI handoff reset attachment is unavailable')
    if backend.lethal is not None and backend.lethal.armed:
        raise TrialStopped('UI handoff lethal recovery is still armed')
    # A completed RPC can publish just before its worker returns. Join only that
    # bounded epilogue; never treat a still-running native operation as idle.
    for worker in backend.workers:worker.join(timeout=.05)
    if any(worker.is_alive() for worker in backend.workers):
        raise TrialStopped('UI handoff has pending reset RPC work')
    if backend.orphan_message or backend.message_sink is not None:
        raise TrialStopped('UI handoff reset messages are not quiescent')


def _observe(bridge,backend,expected_epoch=None):
    _backend_idle(backend)
    if bridge.episode_epoch is not None or bridge.owns_replay:
        raise TrialStopped('UI handoff still owns native episode input')
    before=bridge.rpc('replay_status')
    if before['active']:raise TrialStopped('Another native timeline is active during UI handoff')
    if expected_epoch is not None and before['epoch']!=expected_epoch:
        raise TrialStopped('Native replay epoch changed before UI handoff')
    state=bridge.sample(actors=False)
    after=bridge.rpc('replay_status')
    if after['active'] or before['epoch']!=after['epoch']:
        raise TrialStopped('Native replay changed during UI handoff observation')
    return state,after


def _input_guard(command,bridge,state,status):
    if set(command)-{'handoff_id','sequence','action','expected_epoch','expected_ui','expected_reads','keys','hold_seconds'}:
        raise ValueError('Unknown UI handoff command field')
    if type(command.get('expected_epoch')) is not int or command['expected_epoch']!=status['epoch']:
        raise ValueError('UI handoff expected epoch does not match')
    if state['health']<=0:raise ValueError('UI handoff input requires living health')
    keys=command.get('keys');hold=command.get('hold_seconds')
    if not isinstance(keys,str) or keys not in _BUTTONS:
        raise ValueError('UI handoff permits one ordinary button or neutral')
    if type(hold) not in (int,float) or not math.isfinite(hold) or not .05<=hold<=.5:
        raise ValueError('UI handoff hold must be .05.. .5 seconds')
    expected=command.get('expected_reads')
    if not isinstance(expected,dict) or not expected:
        raise ValueError('UI handoff requires exact existing-read guards')
    ui_reads={rule['read'] for rules in bridge.config['ui'].values() for rule in rules}
    if not ui_reads.intersection(expected):raise ValueError('UI handoff requires an existing UI read guard')
    for name,values in expected.items():
        if name not in bridge.config['reads'] or name not in state['reads'] or not isinstance(values,list) or not values or any(type(v) is not int for v in values):
            raise ValueError('UI handoff guard must name existing integer telemetry')
        if list(state['reads'][name])!=values:raise ValueError('UI handoff read guard changed: '+name)
    screen=command.get('expected_ui')
    if screen is not None and (screen not in bridge.config['ui'] or not ui_matches(bridge.config,state,screen)):
        raise ValueError('UI handoff named UI guard did not match')
    return keys,hold


def _inside(state,bounds):
    point=state.get('position')
    if (not isinstance(point,(list,tuple)) or len(point)!=3 or
        any(type(v) not in (int,float) or not math.isfinite(v) or abs(v)>=1e6 for v in point)):
        raise TrialStopped('Invalid position during handoff movement')
    return all(lo<=value<=hi for value,(lo,hi) in zip(point,bounds))


def _move_guard(command,bridge,state,status):
    fields={'handoff_id','sequence','action','expected_epoch','expected_ui','expected_reads',
            'world_xz','frames','bounds','timeout_seconds'}
    if set(command)!=fields:raise ValueError('Movement requires only its bounded structured fields')
    expected={name:command[name] for name in ('handoff_id','sequence','action','expected_epoch','expected_ui','expected_reads')}
    _input_guard(dict(expected,keys='',hold_seconds=.05),bridge,state,status)
    if command['expected_ui']!='gameplay' or getattr(bridge,'world_xz',None) is None:
        raise ValueError('Movement requires gameplay and the existing world-camera binding')
    vector=command['world_xz'];frames=command['frames'];bounds=command['bounds'];timeout=command['timeout_seconds']
    if (not isinstance(vector,list) or len(vector)!=2 or
        any(type(v) not in (int,float) or not math.isfinite(v) for v in vector) or math.hypot(*vector)>1):
        raise ValueError('Movement vector must be a finite world-X/Z unit disc')
    if type(frames) is not int or not 1<=frames<=30:raise ValueError('Movement requires 1..30 frames')
    if type(timeout) not in (int,float) or not math.isfinite(timeout) or not .1<=timeout<=5:
        raise ValueError('Movement timeout must be .1..5 seconds')
    if (not isinstance(bounds,list) or len(bounds)!=3 or any(not isinstance(pair,list) or len(pair)!=2 or
        any(type(v) not in (int,float) or not math.isfinite(v) or abs(v)>=1e6 for v in pair) or
        pair[0]>=pair[1] for pair in bounds)):
        raise ValueError('Movement requires finite ordered XYZ bounds')
    if not _inside(state,bounds):raise ValueError('Current position is outside movement bounds')
    address=bridge.config.get('native_guard_health_address',bridge.config['reads'].get('health',{}).get('address'))
    try:
        if not isinstance(address,str) or not address.startswith('0x'):raise ValueError
        health=int(address,16)
        if not 0<health<=2**64-5 or health%4:raise ValueError
    except ValueError as error:raise ValueError('Movement requires a valid existing health guard') from error
    if status['epoch']>=2**64-1:raise ValueError('Native epoch cannot advance')
    return health


def _move_status(row,epoch,frames,health_address,world_xz,started=None,*,terminal=False,minimum=0):
    if (not isinstance(row,dict) or type(row.get('active')) is not bool or
        any(type(row.get(k)) is not int or not 0<=row[k]<2**64 for k in
            ('epoch','start_frame','end_frame','current_frame','stop_frame')) or
        row.get('guard_address')!=f'0x{health_address:x}' or type(row.get('guard_threshold')) is not int or row['guard_threshold']!=0 or
        row.get('left_stick_space')!='world_xz' or row.get('world_xz')!=world_xz or
        row['epoch']!=epoch or row['start_frame']==0 or row['end_frame']-row['start_frame']!=frames or
        'last_health' not in row or
        row['current_frame']<max(minimum,row['start_frame']-1) or
        (row.get('last_health') is not None and (type(row['last_health']) is not int or not 0<=row['last_health']<2**32)) or
        (started is not None and any(row[k]!=started[k] for k in ('start_frame','end_frame'))) or
        (terminal and row['active'])):
        raise TrialStopped('Movement native identity or cancellation was not verified')
    reason=row.get('reason');health=row['last_health']
    if row['active']:
        if reason!='running' or health==0:raise TrialStopped('Invalid active movement state')
    elif (reason not in ('cancelled','completed','death') or
          not row['start_frame']-1<=row['stop_frame']<=row['current_frame'] or
          (reason=='death')!=(health==0) or
          (reason=='completed' and (health is None or row['stop_frame']<row['end_frame']))):
        raise TrialStopped('Invalid movement terminal evidence')


def _move(bridge,backend,command,state,status,health_address) -> dict[str,Any]:
    """One native world segment, always cancelled before returning to the hold."""
    epoch=status['epoch']+1;frames=command['frames'];started=None;primary=None
    observed=state;last_frame=status['current_frame'];reason='timeout';attempted=False
    timeline=f"f0:L{command['world_xz'][0]:.9f}/{command['world_xz'][1]:.9f}~f{frames}\n"
    try:
        _backend_idle(backend)
        with bridge.request_deadline(command['timeout_seconds']):
            deadline=time.monotonic()+command['timeout_seconds']
            attempted=True
            installed=bridge.start_replay(timeline,health_address,episode=True)
            _move_status(installed,epoch,frames,health_address,bridge.world_xz,minimum=last_frame)
            started=installed
            while True:
                _backend_idle(backend)
                if time.monotonic()>=deadline-.01:reason='timeout';break
                observed=bridge.sample(actors=False)
                current=bridge.rpc('replay_status')
                _move_status(current,epoch,frames,health_address,bridge.world_xz,started,minimum=last_frame)
                last_frame=current['current_frame']
                if current['reason']=='death':reason='death';break
                if observed['health']<state['health'] or (current['last_health'] is not None and current['last_health']<state['health']):reason='damage';break
                if not ui_matches(bridge.config,observed,'gameplay'):reason='ui';break
                if not _inside(observed,command['bounds']):reason='bounds';break
                if not current['active']:
                    if current['reason']!='completed':raise TrialStopped('Movement stopped unexpectedly')
                    reason='completed';break
                time.sleep(.005)
    except BaseException as error:
        primary=error;raise
    finally:
        if attempted:
            try:
                # Cleanup has its own budget even when the movement budget expired.
                with bridge.request_deadline(2.):
                    cancelled=bridge.cancel_replay()
                    _move_status(cancelled,epoch,frames,health_address,bridge.world_xz,started,terminal=True,minimum=last_frame)
                    observed,final=_observe(bridge,backend,epoch)
                    _move_status(final,epoch,frames,health_address,bridge.world_xz,started,terminal=True,minimum=cancelled['current_frame'])
                    if any(final[k]!=cancelled[k] for k in ('reason','stop_frame','last_health')):
                        raise TrialStopped('Movement terminal changed after cancellation')
            except BaseException as error:
                if primary is None:raise
                primary.add_note('Movement cancellation also failed: '+str(error))
    if final['reason']=='death':reason='death'
    elif observed['health']<state['health'] or (final['last_health'] is not None and final['last_health']<state['health']):reason='damage'
    elif not ui_matches(bridge.config,observed,'gameplay'):reason='ui'
    elif not _inside(observed,command['bounds']):reason='bounds'
    return dict(reason=reason,timeline=timeline,state=observed,status=final)


def _verified_objective(result):
    if result.get('success') is not True or result.get('reason')!='objective':return False
    status=result.get('terminal_status')
    fields=('epoch','current_frame','start_frame','end_frame','stop_frame','last_health')
    if (not isinstance(status,dict) or status.get('active') is not False or status.get('reason')!='cancelled' or
        any(type(status.get(k)) is not int or not 0<=status[k]<2**64 for k in fields) or
        status['epoch']==0 or not status['start_frame']<=status['stop_frame']<status['end_frame'] or
        status['current_frame']<status['stop_frame'] or not 0<status['last_health']<2**32 or
        type(result.get('native_epoch')) is not int or result['native_epoch']!=status['epoch'] or
        type(result.get('objective_frame')) is not int or
        not 0<result['objective_frame']<=status['stop_frame']-status['start_frame']):
        raise TrialStopped('Objective qualification lacks verified terminal evidence')
    return True


def _qualification_rows(evolution,generation,candidate):
    return [dict(key=[generation,candidate,task,repeat],result=json.loads(value))
            for task,repeat,value in evolution.db.execute(
                'SELECT task,repeat,result FROM trials WHERE generation=? AND candidate=? ORDER BY task,repeat',
                (generation,candidate))]


def prepare_objective_handoff(evolution,key,result,output,config,*,genome,implementation) -> dict[str,Any] | None:
    """Publish a control-scoped barrier BEFORE the final qualifying DB record."""
    if not _verified_objective(result):return None
    if (len(key)!=4 or any(type(k) is not int or k<0 for k in key) or
        key[0]!=evolution.state['generation'] or key[1]>=len(evolution.state['candidates']) or
        genome!=evolution.state['candidates'][key[1]]):
        raise TrialStopped('Objective qualification candidate identity changed')
    generation,candidate,task,repeat=key
    expected={(t,r) for t in range(len(evolution.tasks)) for r in range(evolution.repeats)}
    rows=_qualification_rows(evolution,generation,candidate)
    seen={(row['key'][2],row['key'][3]) for row in rows}
    if not seen<=expected or (task,repeat) not in expected or (task,repeat) in seen:
        raise TrialStopped('Objective qualification rows do not match the expected repeats')
    if seen|{(task,repeat)}!=expected:return None
    if any(row['result'].get('genome')!=genome for row in rows):
        raise TrialStopped('Objective qualification genome changed across repeats')
    if not all(_verified_objective(row['result']) for row in rows):return None
    rows.append(dict(key=list(key),result=dict(result,genome=dict(genome))))
    rows.sort(key=lambda row:row['key'])
    output=Path(output);assert_no_pending_handoff(output,config)
    marker=_control_marker(config)
    if marker is None:raise TrialStopped('Progression requires a control-scoped handoff marker')
    handoff_id=uuid.uuid4().hex;archive=output/'handoffs'/handoff_id
    pending=dict(handoff_id=handoff_id,event_path=str(archive/'event.json'),resolved=False,
                 phase='preparing',kind='objective',generation=generation,candidate=candidate)
    spec=evolution.tasks[task]
    evidence=dict(native_epoch=result['native_epoch'],cancel_status=dict(result['terminal_status']),
        timeline=compile_timeline(genome,spec.get('segments',128),spec['horizon_frames'],
            policy=spec.get('policy','timeline_v1'),target=spec['target'],stop_radius=spec.get('feedback_stop_radius',.1)))
    event=dict(schema_version=1,event='native_objective_handoff',kind='objective',handoff_id=handoff_id,
        key=list(key),genome=dict(genome),genome_sha256=hashlib.sha256(json.dumps(genome,sort_keys=True).encode()).hexdigest(),
        objective=spec,curriculum=evolution.tasks,implementation=implementation,pid=config.get('pid'),
        identity=config.get('identity'),session_config=config,
        session_config_sha256=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest(),
        evidence=evidence,scored=True,qualification=dict(generation=generation,candidate=candidate,
            signature=evolution.state['signature'],trials=rows))
    event=json.loads(json.dumps(event,allow_nan=False))
    # The first durable write already blocks every output sharing this control.
    # A failure making the output marker or recording the final trial is a hold.
    _atomic(marker,dict(pending,pid=config.get('pid')))
    _atomic(output/'handoff.json',pending)
    return dict(pending=pending,event=event)


def wait_for_handoff(bridge,backend,output,interruption=None,*,key,genome,objective,implementation,log,resident=True,intent=None,evolution=None):
    """Stay attached until explicit shutdown; no scoring, resets or task resume.

    The supervisor atomically replaces handoff-command.json. Commands require the
    handoff_id and next sequence shown in handoff-ack.json. Accepted sequence
    numbers are durably consumed before input, so a lost ACK cannot repeat a press.
    """
    bridge.running=False
    output=Path(output);marker=_control_marker(bridge.config)
    event: dict[str,Any]
    if intent is None:
        assert_no_pending_handoff(output,bridge.config)
        if interruption is None:raise ValueError('UI handoff needs interruption evidence')
        handoff_id=uuid.uuid4().hex;archive=output/'handoffs'/handoff_id
        event=dict(schema_version=1,event='native_ui_handoff',kind='ui',handoff_id=handoff_id,key=list(key),
            genome=dict(genome),genome_sha256=hashlib.sha256(json.dumps(genome,sort_keys=True).encode()).hexdigest(),
            objective=objective,implementation=implementation,pid=bridge.config.get('pid'),
            identity=bridge.config.get('identity'),session_config=bridge.config,
            session_config_sha256=hashlib.sha256(json.dumps(bridge.config,sort_keys=True).encode()).hexdigest(),
            evidence=interruption.evidence,scored=False)
        pending=dict(handoff_id=handoff_id,event_path=str(archive/'event.json'),resolved=False,phase='preparing',kind='ui')
        if marker is not None:_atomic(marker,dict(pending,pid=bridge.config.get('pid')))
        _atomic(output/'handoff.json',pending)
    else:
        if evolution is None or interruption is not None:raise ValueError('Objective handoff needs its recorded experiment')
        pending=intent['pending'];event=intent['event'];handoff_id=pending['handoff_id']
        archive=Path(pending['event_path']).parent
        qualification=event['qualification']
        if (marker is None or json.loads(marker.read_text())!=dict(pending,pid=bridge.config.get('pid')) or
            json.loads((output/'handoff.json').read_text())!=pending or
            evolution.state['signature']!=qualification['signature'] or
            _qualification_rows(evolution,qualification['generation'],qualification['candidate'])!=qualification['trials']):
            raise TrialStopped('Objective handoff intent or recorded qualification changed')
    archive.mkdir(parents=True,exist_ok=False)
    _atomic(archive/'event.json',event)
    evidence=event['evidence']
    # Closing this bridge later must not inject Plus as a generic cleanup action.
    bridge.running=False
    sequence=0;last_bytes=None
    def outcome(phase,**fields):
        row=dict(event='handoff_command',handoff_id=handoff_id,phase=phase,**fields)
        with (archive/'commands.jsonl').open('a') as stream:
            stream.write(json.dumps(row,allow_nan=False)+'\n');stream.flush();os.fsync(stream.fileno())
        log.write(json.dumps(row)+'\n');log.flush()
        return row
    def acknowledge(phase,**fields):
        row=outcome(phase,next_sequence=sequence+1,**fields)
        _atomic(output/'handoff-ack.json',row)
    try:
        cancelled=evidence.get('cancel_status',{})
        if cancelled.get('active') is not False or cancelled.get('epoch')!=evidence.get('native_epoch'):
            raise TrialStopped('UI handoff lacks verified episode cancellation')
        state,status=_observe(bridge,backend,cancelled['epoch'])
        pending['phase']='waiting';_atomic(output/'handoff.json',pending)
        acknowledge('ready',state=state,status=status,resident=resident)
        if not resident:raise TrialStopped('UI handoff archived; --resident-handoff is required to keep its attachment')
        while True:
            path=output/'handoff-command.json'
            try:
                with path.open('rb') as stream:raw=stream.read(65537)
            except FileNotFoundError:
                time.sleep(.1);continue
            if raw==last_bytes:
                time.sleep(.1);continue
            last_bytes=raw
            try:
                if len(raw)>65536:raise ValueError('UI handoff command is too large')
                command=json.loads(raw)
                if not isinstance(command,dict) or command.get('handoff_id')!=handoff_id:
                    raise ValueError('Stale or invalid UI handoff identity')
                if type(command.get('sequence')) is not int or command['sequence']!=sequence+1:
                    raise ValueError('Stale, duplicate or out-of-order UI handoff sequence')
            except (ValueError,UnicodeError) as error:
                acknowledge('rejected',error=str(error));continue
            sequence=command['sequence']
            # Persist the attempt before validation/dispatch. Even a rejected
            # expected-state guard consumes this sequence; ACK supplies the next.
            outcome('accepted',sequence=sequence,command=command)
            action=command.get('action')
            if action=='shutdown' and set(command)=={'handoff_id','sequence','action'}:
                pending['phase']='shutdown_requested';_atomic(output/'handoff.json',pending)
                acknowledge('shutdown_requested',sequence=sequence)
                return 'shutdown'
            if action=='observe':
                if set(command)!={'handoff_id','sequence','action'}:
                    acknowledge('rejected',sequence=sequence,error='Unknown observe field');continue
                state,status=_observe(bridge,backend)
                acknowledge('observed',sequence=sequence,state=state,status=status);continue
            if action=='move':
                state,status=_observe(bridge,backend)
                try:health=_move_guard(command,bridge,state,status)
                except (ValueError,TypeError) as error:
                    acknowledge('rejected',sequence=sequence,error=str(error),state=state,status=status);continue
                result=_move(bridge,backend,command,state,status,health)
                acknowledge('moved' if result['reason']=='completed' else 'stopped',sequence=sequence,before=state,**result)
                continue
            if action!='input':
                acknowledge('rejected',sequence=sequence,error='Only input, move, observe or shutdown is supported');continue
            state,status=_observe(bridge,backend)
            try:keys,hold=_input_guard(command,bridge,state,status)
            except (ValueError,TypeError) as error:
                acknowledge('rejected',sequence=sequence,error=str(error),state=state,status=status);continue
            primary=None
            try:after=bridge.sample(keys,hold,actors=False)
            except BaseException as error:
                primary=error;raise
            finally:
                try:
                    cancelled=bridge.cancel_replay()
                    if cancelled['active']:raise TrialStopped('UI handoff input cancellation was not acknowledged')
                except BaseException as error:
                    if primary is None:raise
                    primary.add_note('UI handoff input cleanup also failed: '+str(error))
            after,status=_observe(bridge,backend)
            acknowledge('applied',sequence=sequence,before=state,state=after,status=status)
    except BaseException as error:
        pending['phase']='failed'
        try:
            _atomic(output/'handoff.json',pending)
            acknowledge('failed',sequence=sequence,error=str(error))
        except BaseException as logging_error:error.add_note('UI handoff diagnostic failed: '+str(logging_error))
        raise
