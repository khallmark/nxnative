"""Protocol-1 native frame replay and single-file batched memory observations."""
import json
import math
import re
from collections import deque
from contextlib import contextmanager
import time
import uuid
from .live import Bridge,TrialStopped


_WORLD_FIELDS=('rotation_address','guard1_address','guard1_value','guard2_address','guard2_value')
_POSITION_FIELDS=('position_address','guard1_address','guard1_value','guard2_address','guard2_value')


def _validate_binding(binding,fields,size,kind):
    """Freeze generic memory addresses/identities without guessing game offsets."""
    if binding is None:return None
    if not isinstance(binding,dict) or set(binding)!=set(fields):
        raise ValueError('Invalid native '+kind+' binding')
    result={}
    for name in fields:
        value=binding[name]
        if not isinstance(value,str) or not re.fullmatch(r'0x[0-9a-fA-F]{1,16}',value):
            raise ValueError('Invalid native '+kind+' '+name)
        number=int(value,16)
        if name.endswith('_address'):
            length,alignment=(size,4) if name==fields[0] else (8,8)
            if not 0<number<=2**64-1-length or number%alignment:
                raise ValueError('Invalid native '+kind+' '+name)
        result[name]=f'0x{number:x}'
    return result


def validate_world_xz(binding):
    return _validate_binding(binding,_WORLD_FIELDS,36,'world-X/Z camera')


def validate_position_xyz(binding):
    return _validate_binding(binding,_POSITION_FIELDS,12,'player-position')


class ReplayBridge(Bridge):
    def __init__(self,config):
        self.native_status={};self.episode_epoch=None;self.owns_replay=False
        self.world_xz=validate_world_xz(config.get('native_world_xz'))
        self.position_xyz=validate_position_xyz(config.get('native_position_xyz'))
        if self.position_xyz is not None and self.world_xz is None:
            raise ValueError('Native player-position binding requires a world-camera binding')
        self.fps=config.get('replay_fps',60)
        if type(self.fps) is not int or not 1<=self.fps<=240:raise ValueError('Invalid guest replay FPS')
        super().__init__(config)

    @contextmanager
    def request_deadline(self,seconds):
        """Bound all RPCs in one operation; callers budget cleanup separately."""
        if type(seconds) not in (int,float) or not math.isfinite(seconds) or not 0<seconds<=10:
            raise ValueError('Invalid native request deadline')
        previous=getattr(self,'rpc_deadline',None)
        deadline=time.monotonic()+seconds
        self.rpc_deadline=min(previous,deadline) if previous is not None else deadline
        try:yield
        finally:self.rpc_deadline=previous

    def rpc(self,verb,before=(),after=()):
        ack=self.root/(uuid.uuid4().hex+'.json')
        parts=(verb,*map(str,before),str(ack),*map(str,after))
        if any(any(c.isspace() for c in part) for part in parts):raise ValueError('Whitespace in replay argument')
        deadline=min(time.monotonic()+10,getattr(self,'rpc_deadline',None) or float('inf'))
        try:
            if time.monotonic()>=deadline:raise TrialStopped('Native replay request deadline expired')
            # Once a start is dispatched, a missing ACK leaves input ownership uncertain.
            if verb=='replay':self.owns_replay=True
            self.send(' '.join(parts))
            while not ack.is_file():
                if time.monotonic()>deadline:raise TrialStopped('Native replay ACK timed out')
                time.sleep(.001)
            result=json.loads(ack.read_text())
            if result.get('protocol')!=1 or result.get('ok') is not True:
                raise TrialStopped('Native replay rejected request: '+str(result.get('error')))
            if any(type(result.get(k)) is not int or not 0<=result[k]<2**64 for k in ('epoch','current_frame','start_frame','end_frame')) or type(result.get('active')) is not bool:
                raise TrialStopped('Malformed native replay status')
            self.native_status={k:v for k,v in result.items() if k!='reads'}
            if self.episode_epoch is not None and result['epoch']!=self.episode_epoch:
                raise TrialStopped('Native replay epoch changed during evaluation')
            return result
        finally:
            ack.unlink(missing_ok=True)

    def start_replay(self,timeline,health_address=None,threshold=0,episode=True,target_feedback=False):
        if self.episode_epoch is not None:raise TrialStopped('A native episode already owns input')
        if type(target_feedback) is not bool:raise ValueError('Invalid target-feedback mode')
        if target_feedback and (not episode or self.world_xz is None or self.position_xyz is None):
            raise ValueError('Target feedback requires an episode with world and position bindings')
        path=self.root/(uuid.uuid4().hex+'.keys')
        try:
            path.write_text(timeline)
            guard=()
            if health_address is not None:
                if type(health_address) is not int or health_address<=0 or health_address%4 or health_address>2**64-5 or type(threshold) is not int or not 0<=threshold<2**32:
                    raise ValueError('Invalid native health guard')
                guard=(f'health={health_address:x}:{threshold}',)
            binding=self.world_xz if episode else None
            if binding is not None:
                guard+=('world_xz='+':'.join(binding[name][2:] for name in _WORLD_FIELDS),)
            position=self.position_xyz if target_feedback else None
            if position is not None:
                guard+=('position_xyz='+':'.join(position[name][2:] for name in _POSITION_FIELDS),)
            result=self.rpc('replay',(path,),guard)
            self.owns_replay=True
            if binding is not None:
                if result.get('left_stick_space')!='world_xz' or result.get('world_xz')!=binding or type(result.get('last_camera_frame')) is not int or not 0<=result['last_camera_frame']<=result['current_frame']:
                    raise TrialStopped('Native replay did not acknowledge world-X/Z camera binding')
            elif result.get('left_stick_space','camera')!='camera' or result.get('world_xz') is not None:
                raise TrialStopped('Native replay unexpectedly changed left-stick coordinates')
            if target_feedback:
                frame=result.get('last_position_frame');observed=result.get('last_position')
                if (result.get('movement_mode')!='target_feedback' or result.get('position_xyz')!=position or
                    type(frame) is not int or not 0<=frame<=result['current_frame'] or
                    (observed is None and frame!=0) or
                    (observed is not None and (not isinstance(observed,list) or len(observed)!=3 or
                     any(type(v) not in (int,float) or not math.isfinite(v) or abs(v)>=1e6 for v in observed)))):
                    raise TrialStopped('Native replay did not acknowledge target-feedback position binding')
            elif result.get('movement_mode','timeline')!='timeline' or result.get('position_xyz') is not None:
                raise TrialStopped('Native replay unexpectedly enabled target feedback')
            if episode:self.episode_epoch=result['epoch']
            return result
        finally:
            path.unlink(missing_ok=True)

    def cancel_replay(self):
        # Cancellation remains possible after an epoch mismatch or malformed read.
        self.episode_epoch=None
        result=self.rpc('replay_cancel');self.owns_replay=False
        return result

    def wait_replay(self,timeout=10):
        deadline=time.monotonic()+timeout
        while True:
            status=self.rpc('replay_status')
            if not status['active']:return status
            if time.monotonic()>deadline:
                self.cancel_replay();raise TrialStopped('Native frame action did not finish')
            time.sleep(.001)

    def read_memory(self,ranges,commands=()):
        if len(ranges)>4096 or sum(ranges.values())>128*1024*1024 or any(type(a) is not int or type(n) is not int or not 0<a<2**64-n or not 0<n<=128*1024*1024 for a,n in ranges.items()):
            raise ValueError('Invalid native observation ranges')
        if commands:
            if self.episode_epoch is not None or self.native_status.get('active'):
                raise TrialStopped('Cannot mix UI controls with an active episode')
            for command in commands:
                keys,hold=command.rsplit('~',1);seconds=float(hold)
                if not math.isfinite(seconds) or not .05<=seconds<=.5:raise ValueError('Invalid bounded UI hold')
                self.start_replay(f'f0:{keys}~f{max(1,round(seconds*self.fps))}\n',episode=False)
                status=self.wait_replay()
                if status['reason']!='completed':raise TrialStopped('Native UI action failed: '+status['reason'])
        # Split larger actor reads to the host's 4KiB/range and64KiB/batch limits.
        chunks=deque((address+offset,min(4096,size-offset),address,offset) for address,size in ranges.items() for offset in range(0,size,4096))
        output={address:bytearray(size) for address,size in ranges.items()}
        while chunks:
            batch=[];total=0
            while chunks and len(batch)<128 and total+chunks[0][1]<=65536:
                part=chunks.popleft();batch.append(part);total+=part[1]
            request=self.root/(uuid.uuid4().hex+'.reads')
            try:
                request.write_text(''.join(f'{a:x} {n}\n' for a,n,_,_ in batch))
                response=self.rpc('replay_observe',(request,))
                reads=response.get('reads')
                if not isinstance(reads,list) or len(reads)!=len(batch):raise TrialStopped('Missing native observations')
                for row,(address,size,original,offset) in zip(reads,batch):
                    if row.get('error') is not None or row.get('size')!=size or row.get('address')!=f'0x{address:x}' or not isinstance(row.get('hex'),str) or len(row['hex'])!=size*2:
                        raise TrialStopped('Invalid or unreadable native observation')
                    try:data=bytes.fromhex(row['hex'])
                    except ValueError as exc:raise TrialStopped('Malformed native observation bytes') from exc
                    output[original][offset:offset+size]=data
            finally:
                request.unlink(missing_ok=True)
        return {address:bytes(value) for address,value in output.items()}

    def settle(self,seconds):
        while seconds>0:
            hold=min(.5,seconds)
            if hold<.05:hold=.05
            self.sample('',hold,actors=False);seconds-=hold

    def close(self):
        try:
            if self.owns_replay:self.cancel_replay()
        finally:
            super().close()
