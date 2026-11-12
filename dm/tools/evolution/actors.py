"""Read-only named actor discovery through a configured, validated handle pool."""
import math
import struct
from typing import Any


def integer(value):
    return int(value,0) if isinstance(value,str) else value


class ActorUnavailable(RuntimeError):
    pass


class ActorReader:
    def __init__(self,profile,read_batch):
        self.profile=profile;self.read_batch=read_batch
        self.pool=profile['pool'];self.obj=profile['object']
        self.ranges=[tuple(map(integer,r)) for r in profile['ranges']]
        self.bindings: dict[str,dict[str,Any]]={}
        self.cache: dict[int,bytes]={}
        pool=self.pool;obj=self.obj
        if not 1<=pool['count']<=4096 or not 8<=pool['stride']<=512 or not 16<=obj['size']<=4096:
            raise ValueError('Invalid actor pool/object size')
        if not self.contains(integer(pool['address']),pool['count']*pool['stride']):
            raise ValueError('Actor pool outside declared memory ranges')
        for offset in pool['flags']+[pool['pointer'],pool['serial']]:
            if not isinstance(offset,int) or not 0<=offset<=pool['stride']-8:
                raise ValueError('Invalid handle field offset')
        for key,width in [('self',8),('name',8),('position',12)]:
            if not isinstance(obj[key],int) or not 0<=obj[key]<=obj['size']-width:
                raise ValueError('Invalid actor field offset')
        if not pool['flags'] or not isinstance(profile['targets'],dict) or not profile['targets']:
            raise ValueError('Actor discovery needs handle flags and named targets')
        for spec in profile['targets'].values():
            name=spec.get('name')
            if not isinstance(name,str) or not name or len(name)>127 or not name.isascii():
                raise ValueError('Actor target needs an ASCII name')

    def contains(self,address,size):
        return size>0 and any(lo<=address and address+size<=hi for lo,hi in self.ranges)

    def invalidate(self):
        self.bindings={}

    def pages(self,addresses):
        needed=sorted({a//65536*65536 for a in addresses}-self.cache.keys())
        if len(self.cache)+len(needed)>1024 or any(not self.contains(a,65536) for a in needed):
            raise ActorUnavailable('Actor scan exceeded its declared memory range/budget')
        if needed:self.cache.update(self.read_batch({a:65536 for a in needed}))

    def read(self,address,size):
        if not self.contains(address,size):
            raise ActorUnavailable('Actor pointer outside declared memory ranges')
        self.pages(range(address//65536*65536,(address+size-1)//65536*65536+65536,65536))
        result=b''
        while size:
            base=address//65536*65536;chunk=self.cache[base][address-base:address-base+size]
            result+=chunk;address+=len(chunk);size-=len(chunk)
        return result

    def handle(self,data):
        q=lambda off:struct.unpack_from('<Q',data,off)[0]
        p=self.pool
        if any(q(off)&integer(p['mask'])!=integer(p['equals']) for off in p['flags']):return None
        address=q(p['pointer']);serial=q(p['serial'])
        if not self.contains(address,self.obj['size']) or serial>=0xffffffff:return None
        return address,serial

    def header(self,data,address):
        obj=self.obj;q=lambda off:struct.unpack_from('<Q',data,off)[0]
        if q(0)!=integer(obj['vtable']) or q(obj['self'])!=address:return None
        name=q(obj['name']);position=struct.unpack_from('<3f',data,obj['position'])
        if not self.contains(name,128) or not all(math.isfinite(v) and abs(v)<1e6 for v in position):return None
        return name,position

    def discover(self):
        self.cache={};self.bindings={};pool=self.pool
        start=integer(pool['address']);handles=self.read(start,pool['count']*pool['stride']);candidates=[]
        for i in range(pool['count']):
            offset=i*pool['stride'];slot=self.handle(handles[offset:offset+pool['stride']])
            if slot is not None:candidates.append((start+offset,*slot))
        self.pages([address+d for _,address,_ in candidates for d in [0,self.obj['size']-1]])
        valid=[]
        for handle,address,serial in candidates:
            header=self.header(self.read(address,self.obj['size']),address)
            if header is not None:valid.append((handle,address,serial,*header))
        self.pages([name+d for *_,name,position in valid for d in [0,127]])
        matches={key:[] for key in self.profile['targets']}
        for handle,address,serial,nameptr,position in valid:
            name=self.read(nameptr,128).split(b'\0',1)[0]
            for key,spec in self.profile['targets'].items():
                if name==spec['name'].encode('ascii'):
                    matches[key].append(dict(handle=handle,address=address,serial=serial,nameptr=nameptr,
                                             name=spec['name'],position=position))
        self.cache={}
        for key,found in matches.items():
            if len(found)>1 or (not found and self.profile['targets'][key].get('required',True)):
                raise ActorUnavailable('Expected one live actor: '+key)
            if found:self.bindings[key]=found[0]

    def current_reads(self):
        reads={}
        for actor in self.bindings.values():
            reads[actor['handle']]=self.pool['stride'];reads[actor['address']]=self.obj['size'];reads[actor['nameptr']]=128
        return reads

    def current(self,data=None):
        reads=self.current_reads()
        if data is None:data=self.read_batch(reads) if reads else {}
        result={}
        for key,actor in self.bindings.items():
            address=actor['address'];slot=self.handle(data[actor['handle']]);header=self.header(data[address],address)
            if slot!=(address,actor['serial']) or header is None or header[0]!=actor['nameptr'] or data[actor['nameptr']].split(b'\0',1)[0]!=actor['name'].encode('ascii'):
                return None
            result[key]={**actor,'position':header[1]}
        return result

    def observe(self,data=None):
        if not self.bindings:
            self.discover();data=None
        result=self.current(data)
        if result is None:
            self.discover();result=self.current()
        if result is None:raise ActorUnavailable('Actor identity changed during observation')
        return result
