"""Protocol-1 native frame replay and single-file batched memory observations."""
import json
import math
from collections import deque
import time
import uuid
from .live import Bridge,TrialStopped


class ReplayBridge(Bridge):
    def __init__(self,config):
        self.native_status={};self.episode_epoch=None;self.owns_replay=False
        self.fps=config.get('replay_fps',60)
        if type(self.fps) is not int or not 1<=self.fps<=240:raise ValueError('Invalid guest replay FPS')
        super().__init__(config)

    def rpc(self,verb,before=(),after=()):
        ack=self.root/(uuid.uuid4().hex+'.json')
        parts=(verb,*map(str,before),str(ack),*map(str,after))
        if any(any(c.isspace() for c in part) for part in parts):raise ValueError('Whitespace in replay argument')
        try:
            # Once a start is dispatched, a missing ACK leaves input ownership uncertain.
            if verb=='replay':self.owns_replay=True
            self.send(' '.join(parts));deadline=time.monotonic()+10
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

    def start_replay(self,timeline,health_address=None,threshold=0,episode=True):
        if self.episode_epoch is not None:raise TrialStopped('A native episode already owns input')
        path=self.root/(uuid.uuid4().hex+'.keys')
        try:
            path.write_text(timeline)
            guard=()
            if health_address is not None:
                if type(health_address) is not int or health_address<=0 or health_address%4 or health_address>2**64-5 or type(threshold) is not int or not 0<=threshold<2**32:
                    raise ValueError('Invalid native health guard')
                guard=(f'health={health_address:x}:{threshold}',)
            result=self.rpc('replay',(path,),guard)
            self.owns_replay=True
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
