"""Optional, runner-owned Frida reset with bounded requests and explicit cleanup."""
from dataclasses import dataclass
import hashlib
from importlib import import_module
import json
import math
from pathlib import Path
import queue
import threading
import time
from typing import Any
import uuid
from .live import TrialStopped,ui_matches


@dataclass
class PreparedReset:
    config: dict[str,Any]
    source: bytes
    frida: Any
    version: str
    lethal_source: bytes | None = None

    def fingerprint(self):
        return self.source+b'\0frida='+self.version.encode()+b'\0lethal='+(self.lethal_source or b'')


def prepare_fast_reset(session):
    """Validate and freeze the script before experiment identity is computed."""
    if 'fast_reset' not in session:return None
    config=session['fast_reset']
    if not isinstance(config,dict):raise ValueError('fast_reset must be an object')
    script=config.get('script');digest=config.get('sha256');build=config.get('build_id')
    def hexadecimal(value,length):
        return isinstance(value,str) and len(value)==length and all(c in '0123456789abcdef' for c in value)
    if not isinstance(script,str) or not Path(script).is_absolute() or not hexadecimal(digest,64):
        raise ValueError('Fast reset needs an absolute script path and SHA256')
    if not hexadecimal(build,32):raise ValueError('Fast reset needs the full 32-hex build ID')
    if type(config.get('pid')) is not int or config['pid']<=0 or config['pid']!=session.get('pid'):
        raise ValueError('Fast reset PID does not match this session')
    base=config.get('mainbase')
    if not isinstance(base,str) or not base.startswith('0x'):
        raise ValueError('Fast reset mainbase must be a hex address')
    try:address=int(base,16)
    except ValueError as exc:raise ValueError('Invalid fast reset mainbase') from exc
    if not 0<address<2**64 or address%4:raise ValueError('Invalid fast reset mainbase')
    if not isinstance(config.get('address_binding'),dict) or not config['address_binding']:
        raise ValueError('Fast reset needs session address bindings')
    if config.get('mode') not in (None,'player_checkpoint'):
        raise ValueError('Fast reset mode must be player_checkpoint')
    if config.get('mode')=='player_checkpoint':
        rotation=config.get('target_rotation')
        if not isinstance(rotation,list) or len(rotation)!=9 or any(type(v) not in (int,float) or not math.isfinite(v) or abs(v)>1.01 for v in rotation):
            raise ValueError('Player checkpoint needs a finite target_rotation matrix')
        stable=config.get('stable_frames',3);tolerance=config.get('rotation_tolerance',.02)
        if type(stable) is not int or not 2<=stable<=30 or type(tolerance) not in (int,float) or not math.isfinite(tolerance) or not .001<=tolerance<=.1:
            raise ValueError('Player checkpoint stability bounds are invalid')
        if any(not isinstance(config['address_binding'].get(name),str) for name in ('hud_flags','plus_flags')):
            raise ValueError('Player checkpoint needs gameplay UI address bindings')
    timeout=config.get('timeout_seconds',30.)
    if type(timeout) not in (int,float) or not math.isfinite(timeout) or not .1<=timeout<=60:
        raise ValueError('Fast reset timeout must be between 0.1 and 60 seconds')
    try:encoded=json.dumps(config,sort_keys=True,allow_nan=False)
    except (ValueError,TypeError,RecursionError) as exc:raise ValueError('Invalid fast reset JSON configuration') from exc
    if len(encoded.encode())>65536:raise ValueError('Fast reset configuration is too large')
    with Path(script).open('rb') as source:data=source.read(1024*1024+1)
    if not data or len(data)>1024*1024 or hashlib.sha256(data).hexdigest()!=digest:
        raise ValueError('Fast reset script does not match its SHA256/size bound')
    try:data.decode('utf-8')
    except UnicodeDecodeError as exc:raise ValueError('Fast reset script must be UTF-8') from exc
    lethal_source=None
    if 'lethal_script' in config or 'lethal_sha256' in config:
        lethal_path=config.get('lethal_script');lethal_hash=config.get('lethal_sha256')
        if config.get('mode')!='player_checkpoint' or not isinstance(lethal_path,str) or not Path(lethal_path).is_absolute() or not hexadecimal(lethal_hash,64):
            raise ValueError('Lethal recovery needs player_checkpoint and an absolute script path plus SHA256')
        binding=config.get('lethal_address_binding')
        if not isinstance(binding,dict) or any(not isinstance(binding.get(key),str) for key in ('player','life','health','max_health_address')):
            raise ValueError('Lethal recovery needs authoritative address bindings')
        try:
            guard=int(session.get('native_guard_health_address',''),16)
            if guard!=int(binding['health'],16) or guard<=0 or guard%4 or binding['player']!=config['address_binding']['player']:raise ValueError
        except (TypeError,ValueError) as exc:raise ValueError('Lethal recovery must match the authoritative native health guard') from exc
        maximum=config.get('max_health');lethal_timeout=config.get('lethal_timeout_seconds',600.)
        if type(maximum) is not int or not 1<=maximum<=80 or type(lethal_timeout) not in (int,float) or not math.isfinite(lethal_timeout) or not .1<=lethal_timeout<=600:
            raise ValueError('Invalid lethal recovery maximum or timeout')
        with Path(lethal_path).open('rb') as source:lethal_source=source.read(1024*1024+1)
        if not lethal_source or len(lethal_source)>1024*1024 or hashlib.sha256(lethal_source).hexdigest()!=lethal_hash:
            raise ValueError('Lethal script does not match its SHA256/size bound')
        try:lethal_source.decode('utf-8')
        except UnicodeDecodeError as exc:raise ValueError('Lethal script must be UTF-8') from exc
    try:frida=import_module('frida')
    except ImportError as exc:raise ValueError('Fast reset requires the optional Python frida package in this interpreter') from exc
    version=getattr(frida,'__version__',None)
    if not isinstance(version,str) or not version:raise ValueError('Cannot identify the Frida version')
    return PreparedReset(json.loads(encoded),data,frida,version,lethal_source)


class FridaReset:
    """One owned attachment, ephemeral idle scripts, no target process ownership."""
    def __init__(self,prepared,bridge,log):
        self.prepared=prepared;self.bridge=bridge;self.log=log
        self.session: Any=None;self.script: Any=None;self.armed=False
        self.failed=False;self.closed=False;self.uncertain=False;self.verified_player_checkpoint=False
        self.timeout=prepared.config.get('timeout_seconds',30.)
        self.workers=[];self.message_sink=None;self.orphan_message=False
        self.lethal=LethalRecovery(self) if prepared.lethal_source is not None else None

    def native(self,operation,callback,timeout):
        # Native binding calls honor Cancellable; exports_sync RPC does not in
        # Frida 17.23.2, so rpc below waits on an owned, separately bounded worker.
        cancellable=self.prepared.frida.Cancellable()
        timer=threading.Timer(timeout,cancellable.cancel);timer.daemon=True;timer.start()
        try:
            with cancellable:return callback()
        except Exception as exc:raise TrialStopped('Fast reset '+operation+' failed: '+str(exc)) from exc
        finally:timer.cancel()

    def rpc(self,operation,callback,timeout):
        result=queue.Queue(maxsize=1)
        def run():
            try:result.put((True,callback()))
            except Exception as exc:result.put((False,exc))
        worker=threading.Thread(target=run,name='fast-reset-'+operation,daemon=True)
        self.workers=[thread for thread in self.workers if thread.is_alive()]
        self.workers.append(worker);worker.start()
        try:ok,value=result.get(timeout=max(.001,timeout))
        except queue.Empty as exc:raise TrialStopped('Fast reset '+operation+' timed out') from exc
        if not ok:raise TrialStopped('Fast reset '+operation+' failed: '+str(value)) from value
        return value

    def message_received(self,message,data):
        sink=self.message_sink
        if sink is None:self.orphan_message=True;return
        try:
            sink['count']+=1
            if sink['count']>256 or data or len(json.dumps(message,allow_nan=False).encode())>16384:
                sink['overflow'].set();return
            sink['messages'].put_nowait(message)
        except (TypeError,ValueError,RecursionError,queue.Full):sink['overflow'].set()

    def diagnostic(self,phase,**fields):
        """Best-effort evidence must never replace the exception being diagnosed."""
        try:
            record=dict(event='fast_reset_diagnostic',phase=phase,**fields)
            encoded=json.dumps(record,allow_nan=False)
            if len(encoded.encode())>32768:
                encoded=json.dumps(dict(event='fast_reset_diagnostic',phase=phase,error='diagnostic exceeds size bound'))
            self.log.write(encoded+'\n');self.log.flush()
        except Exception:
            pass

    def drain_diagnostics(self,phase):
        sink=self.message_sink
        if sink is None:return
        for _ in range(256):
            try:message=sink['messages'].get_nowait()
            except queue.Empty:break
            self.diagnostic(phase,message=message)

    def capture_failure(self,nonce,error):
        self.diagnostic('failure',nonce=nonce,error=str(error))
        self.drain_diagnostics('before_cleanup_message')
        if self.script is not None:
            try:
                snapshot=self.rpc('failure snapshot',self.script.exports_sync.snapshot,min(1.,self.timeout))
                self.diagnostic('before_cleanup_snapshot',nonce=nonce,snapshot=snapshot)
            except Exception as snapshot_error:
                self.diagnostic('snapshot_error',nonce=nonce,error=str(snapshot_error))
        self.drain_diagnostics('after_snapshot_message')

    def cleanup(self,unload=False):
        if self.script is None:return
        script=self.script;deadline=time.monotonic()+min(5.,self.timeout)
        if self.armed:
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TrialStopped('Fast reset cleanup was not acknowledged; instrumentation remains attached')
                acknowledgement=self.rpc('cancel',script.exports_sync.cancel,remaining)
                if isinstance(acknowledgement,dict) and acknowledgement.get('descriptor_restored') is True:break
                time.sleep(min(.05,max(0,deadline-time.monotonic())))
        self.armed=False
        # Successful player checkpoints keep their compiled, idle script. Failure
        # and explicit close unload only after the same cleanup acknowledgement.
        if unload or self.failed or self.prepared.config.get('mode')!='player_checkpoint':
            self.native('unload',script.unload,min(5.,self.timeout))
            self.script=None
        # Completed RPC calls must leave no worker holding the resident script.
        for worker in self.workers:worker.join(timeout=.05)
        self.workers=[worker for worker in self.workers if worker.is_alive()]
        if self.workers:raise TrialStopped('Fast reset RPC worker did not finish after unload')

    def validate_player_checkpoint(self,payload,task):
        evidence=payload['evidence'];config=self.prepared.config
        def integer(value):return type(value) is int
        def frame(value):
            if not isinstance(value,str) or not value.isascii() or not value.isdigit() or len(value)>20:return None
            result=int(value)
            return result if result<2**64 else None
        def vector(value,size):
            return isinstance(value,list) and len(value)==size and all(type(v) in (int,float) and math.isfinite(v) for v in value)
        first=frame(evidence.get('first_stable_frame'));last=frame(evidence.get('last_stable_frame'))
        stable=evidence.get('stable_frames');required=config.get('stable_frames',3)
        health=task.get('start_health',task['min_health']);position=evidence.get('position_after');rotation=evidence.get('rotation_after')
        dryrun=evidence.get('dryrun',{});delta=frame(dryrun.get('stack_delta')) if isinstance(dryrun,dict) else None
        valid=(payload.get('terminal') is True and payload.get('descriptor_restored') is True and
            payload.get('semantics')=='player_checkpoint' and evidence.get('semantics')=='player_checkpoint' and
            evidence.get('error_code')==0 and evidence.get('health_written') is True and evidence.get('warp_called') is True and
            integer(stable) and required<=stable<=30 and first is not None and last is not None and last-first>=required-1 and
            integer(evidence.get('authoritative_health')) and evidence['authoritative_health']==health and
            integer(evidence.get('mirror_health')) and evidence['mirror_health']==health and
            vector(position,3) and sum((a-b)**2 for a,b in zip(position,task['start']))<=task.get('start_tolerance',.3)**2 and
            vector(rotation,9) and all(abs(a-b)<=config.get('rotation_tolerance',.02) for a,b in zip(rotation,config['target_rotation'])) and
            delta is not None and delta<65536 and dryrun.get('error_code')==0 and
            evidence.get('main_instance')==dryrun.get('main_instance') and evidence.get('pid')==config['pid'] and
            evidence.get('player')==config['address_binding']['player'])
        if not valid:raise TrialStopped('Player checkpoint lacks validated distinct-frame completion evidence')

    def arm_episode(self,status,task,health_address):
        if self.lethal is None:return
        try:
            if self.failed or self.closed:raise TrialStopped('Fast reset backend is no longer available')
            self.lethal.arm(status,task,health_address)
        except BaseException:
            self.failed=True;self.uncertain=True
            raise

    def poll_episode(self):
        if self.lethal is None:return
        try:self.lethal.poll()
        except BaseException:
            self.failed=True;self.uncertain=True
            raise

    def finish_episode(self,status):
        if self.lethal is None:return
        try:self.lethal.finish(status)
        except BaseException:
            self.failed=True;self.uncertain=True
            raise

    def restore(self,task):
        self.verified_player_checkpoint=False
        if self.lethal is not None and self.lethal.armed:raise TrialStopped('Lethal recovery is still armed during checkpoint restore')
        if self.failed or self.closed:raise TrialStopped('Fast reset backend is no longer available')
        if self.prepared.config['pid']!=self.bridge.config.get('pid'):
            raise TrialStopped('Fast reset session PID changed')
        status=self.bridge.rpc('replay_status')
        if status['active'] or self.bridge.episode_epoch is not None:
            raise TrialStopped('Fast reset requires inactive native replay')
        state=self.bridge.sample(actors=False)
        if state['health']<=0 or not ui_matches(self.bridge.config,state,'gameplay'):
            raise TrialStopped('Fast reset requires living gameplay')
        messages=queue.Queue(maxsize=256);overflow=threading.Event();nonce=uuid.uuid4().hex
        self.message_sink={'messages':messages,'overflow':overflow,'count':0}
        deadline=time.monotonic()+self.timeout
        primary_error=None
        try:
            if self.orphan_message:raise TrialStopped('Fast reset received an unexpected message while idle')
            if self.session is None:
                self.session=self.native('attach',lambda:self.prepared.frida.attach(self.prepared.config['pid']),max(.001,deadline-time.monotonic()))
            if self.script is None:
                self.script=self.native('create script',lambda:self.session.create_script(self.prepared.source.decode('utf-8')),max(.001,deadline-time.monotonic()))
                self.script.on('message',self.message_received)
                self.native('load',self.script.load,max(.001,deadline-time.monotonic()))
            config=dict(self.prepared.config,start=list(task['start']),
                        start_health=task.get('start_health',task['min_health']),
                        start_tolerance=task.get('start_tolerance',.3))
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TrialStopped('Fast reset preparation timed out')
            self.armed=True;self.uncertain=True
            self.rpc('arm',lambda:self.script.exports_sync.arm(config,nonce),remaining)
            while True:
                if overflow.is_set():raise TrialStopped('Fast reset message budget exceeded')
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TrialStopped('Fast reset did not complete before timeout')
                try:message=messages.get(timeout=min(.05,remaining))
                except queue.Empty:continue
                if not isinstance(message,dict) or message.get('type')!='send':
                    raise TrialStopped('Fast reset script error: '+str(message)[:1000])
                payload=message.get('payload')
                if not isinstance(payload,dict) or payload.get('nonce')!=nonce or not isinstance(payload.get('event'),str):
                    raise TrialStopped('Fast reset message identity is invalid')
                self.log.write(json.dumps(dict(event='fast_reset',payload=payload))+'\n');self.log.flush()
                if payload['event']=='failed':raise TrialStopped('Fast reset failed: '+str(payload.get('error','unspecified failure')))
                if payload['event']=='restored':
                    if not isinstance(payload.get('evidence'),dict) or not payload['evidence']:
                        raise TrialStopped('Fast reset lacks completion evidence')
                    if self.prepared.config.get('mode')=='player_checkpoint':self.validate_player_checkpoint(payload,task)
                    break
            self.cleanup()
            if self.lethal is not None:self.lethal.prepare(task)
            self.uncertain=False
            self.verified_player_checkpoint=self.prepared.config.get('mode')=='player_checkpoint'
            return payload
        except BaseException as exc:
            primary_error=exc;self.failed=True
            self.capture_failure(nonce,exc)
            raise
        finally:
            try:
                if self.script is not None:self.cleanup()
            except BaseException as cleanup_error:
                self.failed=True;self.uncertain=True
                self.diagnostic('cleanup_error',nonce=nonce,error=str(cleanup_error))
                if primary_error is None:raise
                primary_error.add_note('Fast reset cleanup also failed: '+str(cleanup_error))
            finally:
                self.drain_diagnostics('cleanup_message')
                self.message_sink=None

    def close(self):
        try:
            if self.lethal is not None:self.lethal.close()
            self.cleanup(unload=True)
            if self.session is not None:
                self.native('detach',self.session.detach,min(5.,self.timeout));self.session=None
        finally:self.closed=True


class LethalRecovery:
    """Resident, epoch-bound native terminal interception owned by FridaReset."""
    def __init__(self,owner):
        self.owner=owner;self.script: Any=None;self.armed=False;self.prepared=False
        self.nonce=None;self.epoch=None;self.result=None;self.config: dict[str,Any] | None=None
        self.messages=queue.Queue(maxsize=256);self.overflow=False;self.count=0

    def receive(self,message,data):
        try:
            self.count+=1
            if self.count>256 or data or len(json.dumps(message,allow_nan=False).encode())>16384:
                self.overflow=True;return
            self.messages.put_nowait(message)
        except (TypeError,ValueError,RecursionError,queue.Full):self.overflow=True

    def prepare(self,task):
        if self.armed:raise TrialStopped('Cannot prepare an armed lethal recovery')
        owner=self.owner;config=owner.prepared.config
        self.config=dict(pid=config['pid'],build_id=config['build_id'],mainbase=config['mainbase'],
            address_binding=config['lethal_address_binding'],max_health=config['max_health'],
            checkpoint_health=task['start_health'],confirm_lethal_restore=True,
            timeout_seconds=config.get('lethal_timeout_seconds',600.))
        if self.script is None:
            source=owner.prepared.lethal_source
            assert source is not None and owner.session is not None
            self.script=owner.native('create lethal script',lambda:owner.session.create_script(source.decode('utf-8')),owner.timeout)
            self.script.on('message',self.receive)
            owner.native('load lethal script',self.script.load,owner.timeout)
        acknowledgement=owner.rpc('prepare lethal',lambda:self.script.exports_sync.prepare(self.config),owner.timeout)
        if not isinstance(acknowledgement,dict) or acknowledgement.get('prepared') is not True or acknowledgement.get('hooks_installed') is not False or acknowledgement.get('health_address')!=config['lethal_address_binding']['health']:
            raise TrialStopped('Lethal recovery preparation was not acknowledged')
        self.prepared=True

    def arm(self,status,task,health_address):
        if not self.prepared or self.config is None or self.script is None:
            raise TrialStopped('Lethal recovery must be prepared before replay starts')
        if self.armed:raise TrialStopped('Previous lethal recovery is still armed')
        self.poll(allow_cancelled=True)
        epoch=status.get('epoch')
        if type(epoch) is not int or not 0<epoch<2**64 or health_address!=int(self.config['address_binding']['health'],16) or task['start_health']!=self.config['checkpoint_health']:
            raise TrialStopped('Lethal recovery epoch/address/checkpoint mismatch')
        self.epoch=str(epoch);self.nonce=uuid.uuid4().hex;self.result=None
        self.messages=queue.Queue(maxsize=256);self.overflow=False;self.count=0
        self.armed=True
        config=dict(self.config,native_epoch=self.epoch)
        acknowledgement=self.owner.rpc('arm lethal',lambda:self.script.exports_sync.arm(config,self.nonce),self.owner.timeout)
        if not isinstance(acknowledgement,dict) or acknowledgement.get('armed') is not True or acknowledgement.get('native_epoch')!=self.epoch or acknowledgement.get('nonce')!=self.nonce or acknowledgement.get('health_address')!=self.config['address_binding']['health']:
            raise TrialStopped('Lethal recovery arm was not acknowledged')
        self.owner.log.write(json.dumps(dict(event='lethal_armed',native_epoch=self.epoch,nonce=self.nonce))+'\n');self.owner.log.flush()

    def consume(self,payload,allow_cancelled):
        if not isinstance(payload,dict) or payload.get('nonce')!=self.nonce or payload.get('native_epoch')!=self.epoch or payload.get('event') not in ('recovered','rejected','cancelled'):
            raise TrialStopped('Lethal recovery message identity is invalid')
        self.owner.log.write(json.dumps(dict(event='lethal_recovery',payload=payload))+'\n');self.owner.log.flush()
        if payload['event']=='rejected':raise TrialStopped('Native lethal recovery transaction was rejected')
        if payload['event']=='cancelled':
            if not allow_cancelled or payload.get('reason')=='timeout':raise TrialStopped('Lethal recovery stopped before episode cleanup')
            return
        assert self.config is not None
        frame=payload.get('callback_frame')
        valid=(payload.get('terminal') is True and payload.get('descriptor_restored') is True and
            payload.get('reset_requested') is True and type(payload.get('terminal_health')) is int and payload['terminal_health']==0 and
            type(payload.get('old_health')) is int and 0<payload['old_health']<=self.config['max_health'] and
            payload.get('checkpoint_health')==self.config['checkpoint_health'] and
            payload.get('player')==self.config['address_binding']['player'] and
            payload.get('health_address')==self.config['address_binding']['health'] and
            isinstance(frame,str) and frame.isascii() and frame.isdigit() and len(frame)<=20 and int(frame)<2**64)
        if not valid:raise TrialStopped('Lethal recovery lacks verified terminal evidence')
        if self.result is not None and self.result!=payload:raise TrialStopped('Conflicting lethal recovery terminal evidence')
        self.result=payload

    def poll(self,allow_cancelled=False):
        if self.overflow:raise TrialStopped('Lethal recovery message budget exceeded')
        while True:
            try:message=self.messages.get_nowait()
            except queue.Empty:return
            if not isinstance(message,dict) or message.get('type')!='send':raise TrialStopped('Lethal recovery script error')
            self.consume(message.get('payload'),allow_cancelled)

    def finish(self,status=None):
        if self.script is None:return
        # HID can latch zero between the game's CAS and the post-CAS callback.
        # Preserve that callback until its transaction publishes a terminal result;
        # cancelling state 1 here would remove the only chance to recover health.
        if self.armed and status is not None and status.get('reason')=='death':
            if self.epoch is None or status.get('epoch')!=int(self.epoch):raise TrialStopped('Lethal terminal replay epoch changed')
            deadline=time.monotonic()+min(5.,self.owner.timeout)
            self.poll()
            while self.result is None:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TrialStopped('Native death lacks matching lethal recovery evidence before timeout')
                observed=self.owner.rpc('wait lethal terminal',self.script.exports_sync.status,remaining)
                if not isinstance(observed,dict) or observed.get('native_epoch')!=self.epoch:
                    raise TrialStopped('Lethal terminal status epoch mismatch')
                if observed.get('result') is not None:self.consume(observed['result'],False)
                self.poll()
                if self.result is not None:break
                if observed.get('active') is not True:raise TrialStopped('Native death lacks matching lethal recovery evidence')
                time.sleep(min(.005,max(0,deadline-time.monotonic())))
        if self.armed:
            deadline=time.monotonic()+min(5.,self.owner.timeout)
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TrialStopped('Lethal cleanup was not acknowledged; instrumentation remains attached')
                acknowledgement=self.owner.rpc('cancel lethal',self.script.exports_sync.cancel,remaining)
                if isinstance(acknowledgement,dict) and acknowledgement.get('descriptor_restored') is True:
                    if acknowledgement.get('native_epoch')!=self.epoch:raise TrialStopped('Lethal cleanup epoch mismatch')
                    self.armed=False
                    if acknowledgement.get('result') is not None:self.consume(acknowledgement['result'],True)
                    break
                time.sleep(min(.01,max(0,deadline-time.monotonic())))
        self.poll(allow_cancelled=True)
        if status is not None and self.epoch is not None:
            if self.epoch is None or status.get('epoch')!=int(self.epoch):raise TrialStopped('Lethal terminal replay epoch changed')
            if status.get('reason')=='death':
                if status.get('last_health')!=0 or self.result is None:
                    raise TrialStopped('Native death lacks matching lethal recovery evidence')
            elif self.result is not None:raise TrialStopped('Lethal recovery did not match a native death terminal')

    def close(self):
        primary_error=None
        try:self.finish()
        except BaseException as exc:
            primary_error=exc
            raise
        finally:
            if self.script is not None and not self.armed:
                try:
                    self.owner.native('unload lethal script',self.script.unload,min(5.,self.owner.timeout));self.script=None
                except BaseException as cleanup_error:
                    self.owner.diagnostic('lethal_cleanup_error',error=str(cleanup_error))
                    if primary_error is None:raise
                    primary_error.add_note('Lethal script unload also failed: '+str(cleanup_error))
