"""Unscored UI evidence and explicit commands on the existing runner attachment."""
import hashlib
import json
import math
import os
from pathlib import Path
import time
import uuid

from .live import TrialStopped,ui_matches

_BUTTONS={'','A','B','X','Y','Plus','Minus','Up','Down','Left','Right','L','R','ZL','ZR'}


def _atomic(path,value):
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('x') as stream:
            json.dump(value,stream,allow_nan=False,indent=2);stream.write('\n')
            stream.flush();os.fsync(stream.fileno())
        temporary.replace(path)
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


def wait_for_handoff(bridge,backend,output,interruption,*,key,genome,objective,implementation,log,resident=True):
    """Stay attached until explicit shutdown; no scoring, resets or task resume.

    The supervisor atomically replaces handoff-command.json. Commands require the
    handoff_id and next sequence shown in handoff-ack.json. Accepted sequence
    numbers are durably consumed before input, so a lost ACK cannot repeat a press.
    """
    output=Path(output);assert_no_pending_handoff(output,bridge.config)
    handoff_id=uuid.uuid4().hex;archive=output/'handoffs'/handoff_id
    archive.mkdir(parents=True,exist_ok=False)
    event=dict(schema_version=1,event='native_ui_handoff',handoff_id=handoff_id,key=list(key),
        genome=dict(genome),genome_sha256=hashlib.sha256(json.dumps(genome,sort_keys=True).encode()).hexdigest(),
        objective=objective,implementation=implementation,pid=bridge.config.get('pid'),
        identity=bridge.config.get('identity'),session_config=bridge.config,
        session_config_sha256=hashlib.sha256(json.dumps(bridge.config,sort_keys=True).encode()).hexdigest(),
        evidence=interruption.evidence,scored=False)
    _atomic(archive/'event.json',event)
    pending=dict(handoff_id=handoff_id,event_path=str(archive/'event.json'),resolved=False,phase='preparing')
    _atomic(output/'handoff.json',pending)
    marker=_control_marker(bridge.config)
    if marker is not None:_atomic(marker,dict(pending,pid=bridge.config.get('pid')))
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
        cancelled=interruption.evidence.get('cancel_status',{})
        if cancelled.get('active') is not False or cancelled.get('epoch')!=interruption.evidence.get('native_epoch'):
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
            if action!='input':
                acknowledge('rejected',sequence=sequence,error='Only input or shutdown is supported');continue
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
