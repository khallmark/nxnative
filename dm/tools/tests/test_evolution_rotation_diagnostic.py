"""Exact shipped rotation diagnostic callbacks, without a live game or Frida."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from test_evolution_reset import HEADER,HARNESS

SCRIPT=Path(__file__).resolve().parents[2]/'games'/'0400486024DEC000'/'player_checkpoint.js'

TRACE_HARNESS=r'''
_Alignas(8) static guint8 physics[0x40], model[0x198], controller_set[16], controller[0x30];
_Alignas(8) static guint8 controller_state[0x314], controller_vtable[0x58], body[8];
_Alignas(8) static guint32 setter_code[4]={0x12345678,0x87654321,0x11223344,0x44332211};
static void pointer_at(void *base, size_t offset, guint64 value) { memcpy((char *)base+offset,&value,8); }
static void trace_setup(void) {
  initialize(); test_tid=321; trace_block=0;
  memset(physics,0,sizeof physics);memset(model,0,sizeof model);memset(controller_set,0,sizeof controller_set);
  memset(controller,0,sizeof controller);memset(controller_state,0,sizeof controller_state);
  memset(controller_vtable,0,sizeof controller_vtable);
  packet.action_enabled=1;packet.trace_enabled=1;
  packet.trace_physics=(gsize)physics;packet.trace_model=(gsize)model;
  packet.trace_controller_set=(gsize)controller_set;packet.trace_controller=(gsize)controller;
  packet.trace_controller_state=(gsize)controller_state;packet.trace_body=(gsize)body;
  packet.trace_controller_vtable=(gsize)controller_vtable;packet.trace_setter=(gsize)setter_code;
  packet.trace_physics_vtable=0x111000;packet.trace_model_vtable=0x222000;
  memcpy(packet.trace_code,setter_code,16);
  pointer_at(table_data,0x50,packet.trace_physics);
  pointer_at(physics,0,packet.trace_physics_vtable);pointer_at(physics,0x38,packet.player);
  pointer_at(physics,0x20,packet.trace_model);pointer_at(model,0,packet.trace_model_vtable);
  pointer_at(model,0x10,packet.trace_controller_set);pointer_at(model,0x190,packet.trace_body);
  pointer_at(controller_set,8,packet.trace_controller);pointer_at(controller,0,packet.trace_controller_vtable);
  pointer_at(controller,0x20,packet.trace_body);pointer_at(controller,0x28,packet.trace_controller_state);
  pointer_at(controller_vtable,0x50,packet.trace_setter);
  ((float *)(controller_state+0x1c0))[2]=1;
  *(guint32 *)(controller_state+0x310)=1;
}
static void flip_rotation(void) {
  ((float *)(player_data+0x328))[0]=-1;
  ((float *)(player_data+0x328))[8]=-1;
  ((float *)(controller_state+0x1c0))[2]=-1;
  *(guint32 *)(action_data+0x4c)=41;
  *(guint8 *)(action_data+0x7c)=3;
}
static void actor_only(void) {
  GumCpuContext cpu={0};GumInvocationContext ic;
  cpu.sp=(guint64)(gsize)&cpu & ~15ULL;cpu.x[20]=packet.player;cpu.x[24]=packet.action_bsa;
  ic.function=(void *)0x778899;ic.cpu_context=&cpu;ic.claimed=0;on_action_enter(&ic);
}
int main(void) {
  assert(baseline_main()==0); // unchanged default, cancellation and one-shot regressions
  assert(sizeof(BridgePacket)==1352 && offsetof(BridgePacket,trace_samples)==872);
  initialize();tick();assert(!packet.trace_enabled && !packet.trace_samples[0].captured);
  trace_setup();tick();
  assert(packet.state==7 && packet.trace_samples[0].captured && packet.trace_samples[0].validity==7);
  assert(packet.trace_samples[0].phase==1 && packet.trace_samples[0].frame==100 && packet.trace_samples[0].thread_id==321);
  ++current_frame;test_tid=322;tick();
  assert(warp_count==1 && action_count==1 && packet.health_written && packet.state==5);
  assert(packet.trace_samples[1].phase==2 && packet.trace_samples[1].thread_id==322);
  assert(packet.trace_samples[2].phase==3 && packet.trace_samples[2].frame==101 && packet.trace_samples[2].validity==7);
  assert(packet.trace_saw_target && packet.trace_target_frame==101 && !packet.trace_samples[3].captured);
  ++current_frame;test_tid=323;flip_rotation();tick();
  RotationSample first=packet.trace_samples[3];
  assert(first.captured && first.validity==7 && first.phase==4 && first.frame==102 && first.thread_id==323);
  assert(first.rotation_error==2 && first.direction[2]==-1 && first.action_requested==41 && first.action_selected==41 && first.action_flags==3);
  assert(!packet.error && !packet.stable_count && packet.trace_samples[0].thread_id==321);
  ++current_frame;test_tid=999;((float *)(controller_state+0x1c0))[0]=.5;tick();
  assert(!memcmp(&first,&packet.trace_samples[3],sizeof first));
  current_frame=161;tick();assert(packet.error==13 && packet.state==3 && warp_count==1 && action_count==1);
  assert(hp_data[1]==10 && !memcmp(&first,&packet.trace_samples[3],sizeof first));

  trace_setup();tick();++current_frame;tick();
  pointer_at(controller,0x28,(gsize)controller_state+8);++current_frame;flip_rotation();tick();
  assert(packet.trace_samples[3].captured && packet.trace_samples[3].error==3 && !packet.trace_samples[3].validity);
  assert(!packet.error && warp_count==1);current_frame=161;tick();assert(packet.error==13);
  trace_setup();trace_block=(gsize)controller_state;tick();
  assert(packet.trace_samples[0].captured && packet.trace_samples[0].error==1 && !packet.error && packet.state==7);
  trace_block=0;++current_frame;tick();assert(packet.trace_samples[1].validity==7 && warp_count==1);
  trace_setup();setter_code[0]^=1;tick();assert(packet.trace_samples[0].error==3 && !packet.error);setter_code[0]^=1;
  trace_setup();*(guint32 *)(controller_state+0x1c0)=0x7fc00000;tick();
  assert(packet.trace_samples[0].error==4 && packet.trace_samples[0].validity==3 && !packet.error);
  trace_setup();tick();++current_frame;actor_only();flip_rotation();main_tick();
  assert(packet.trace_samples[2].captured && !packet.trace_saw_target && !packet.trace_samples[3].captured);
  ++current_frame;main_tick();assert(!packet.trace_samples[3].captured);
  ((float *)(player_data+0x328))[0]=1;((float *)(player_data+0x328))[8]=1;
  ++current_frame;main_tick();assert(packet.trace_saw_target && packet.trace_target_frame==103);
  ++current_frame;flip_rotation();main_tick();assert(packet.trace_samples[3].frame==104);
  puts("first-divergence native guards and retention passed");return 0;
}
'''


class RotationDiagnosticTests(unittest.TestCase):
    def test_exact_native_default_and_diagnostic_paths_under_sanitizers(self):
        compiler=shutil.which('clang')
        if compiler is None:self.skipTest('clang unavailable')
        script=SCRIPT.read_text();source=script.split('const NATIVE_SOURCE = `',1)[1].split('`;',1)[0]
        harness=HARNESS.replace('int main(void) {','int baseline_main(void) {')
        harness=harness.replace('static guint8 player_data', '_Alignas(8) static guint8 player_data')
        harness=harness.replace('static guint8 bsa_data', '_Alignas(8) static guint8 bsa_data')
        harness=harness.replace('static guint64 current_frame;', 'static guint64 current_frame, trace_block; static guint test_tid=321;')
        harness=harness.replace('(void)ic; return 321;','(void)ic; return test_tid;')
        harness=harness.replace('*address &= ~4095ULL; *size=4096; *count=9;',
            'if (*address==trace_block) { *object=0; return -1; } *size=65536; *count=9;')
        harness=harness.replace('info[0]=readonly_health ? 1 : 3;', 'info[0]=readonly_health ? 1 : 7;')
        with tempfile.TemporaryDirectory(prefix='rotation-diagnostic-') as directory:
            root=Path(directory);(root/'gum').mkdir();(root/'gum'/'guminterceptor.h').write_text(HEADER)
            code=root/'trace.c';code.write_text('#define pthread_getspecific test_getspecific\n#define pthread_setspecific test_setspecific\n'+source+harness+TRACE_HARNESS)
            for sanitizer in [[],['-fsanitize=address,undefined']]:
                binary=root/'trace'
                compiled=subprocess.run([compiler,'-std=c11','-O1','-Wall','-Wextra','-Werror',*sanitizer,
                    '-I',str(root),str(code),'-o',str(binary)],capture_output=True,text=True,timeout=30)
                self.assertEqual(compiled.returncode,0,compiled.stderr)
                tested=subprocess.run([str(binary)],capture_output=True,text=True,timeout=10)
                self.assertEqual(tested.returncode,0,tested.stderr)
                self.assertIn('first-divergence',tested.stdout)

    def run_node(self,harness):
        node=shutil.which('node')
        if node is None:self.skipTest('node unavailable')
        tested=subprocess.run([node,'-e',harness,str(SCRIPT)],capture_output=True,text=True,timeout=10)
        self.assertEqual(tested.returncode,0,tested.stdout+tested.stderr)
        return tested.stdout

    def test_opt_in_chain_and_setter_fingerprint_validation(self):
        self.run_node(r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const pointers=new Map(),reads=[],ranges=[];
class P {
  constructor(n){this.n=BigInt(n);}
  add(n){return new P(this.n+BigInt(n));} and(n){return new P(this.n&BigInt(n));}
  isNull(){return this.n===0n;} equals(p){return this.n===p.n;}
  toString(){return '0x'+this.n.toString(16);}
  readPointer(){reads.push(Number(this.n));return new P(pointers.get(Number(this.n))||0);}
  readByteArray(n){return new Uint8Array(n).buffer;}
}
const context={rpc:{},ptr:n=>new P(n),Process:{arch:'arm64',platform:'darwin',pointerSize:8,id:9}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
context.requireRange=(address,size,protection,label)=>{ranges.push([Number(address.n),size,protection,label]);};
const base=new P(0x10000000),player=new P(0x1000),components=new P(0x2000),action={};
const config={rotation_diagnostics:true,address_binding:{code_fingerprints:{'0x539e20':'00'.repeat(16)}}};
assert.equal(context.rotationDiagnosticConfig({},base,player,components,null),null);
assert.equal(reads.length,0);assert.equal(ranges.length,0);
assert.throws(()=>context.rotationDiagnosticConfig(config,base,player,components,null),/requires reset_action/);
const entries=[[0x1270,0x2000],[0x2050,0x3000],[0x3000,0x31000],[0x3038,0x1000],[0x3020,0x4000],
 [0x4000,0x41000],[0x4010,0x5000],[0x4190,0x7000],[0x5008,0x6000],
 [0x6000,0x14c34f10],[0x6020,0x7000],[0x6028,0x8000],[0x14c34f60,0x10539e20]];
for(const row of entries)pointers.set(...row);
let checked=context.rotationDiagnosticConfig(config,base,player,components,action);
assert(checked.controllerState.equals(new P(0x8000)));assert.equal(checked.code,'00'.repeat(16));
assert(ranges.some(row=>row[0]===0x8000&&row[1]===0x314));
for(const [address,bad]of [[0x3038,0xbad],[0x6000,0xbad],[0x6020,0xbad],[0x14c34f60,0xbad]]) {
 const old=pointers.get(address);pointers.set(address,bad);
 assert.throws(()=>context.rotationDiagnosticConfig(config,base,player,components,action),/mismatch/);
 pointers.set(address,old);
}
pointers.set(0x6028,0x8001);
assert.throws(()=>context.rotationDiagnosticConfig(config,base,player,components,action),/aligned/);
pointers.set(0x6028,0x8000);
context.requireRange=(address)=>{if(address.equals(new P(0x8000)))throw Error('missing mapped state');};
assert.throws(()=>context.rotationDiagnosticConfig(config,base,player,components,action),/mapped state/);
context.requireRange=()=>{};
const full={pid:9,build_id:'e0f3ccfaf50065ae4ea01eb5aeb13b1a',mainbase:'0x10000000',
 mode:'player_checkpoint',reset_action:true,rotation_diagnostics:true,address_binding:{player:'0x1000',code_fingerprints:{}}};
for(const offset of ['0x3303ca0','0xc6fb30','0xc08f0','0xcba80'])full.address_binding.code_fingerprints[offset]='00'.repeat(16);
assert.throws(()=>context.validate(full,'nonce'),/0x539e20/);
full.address_binding.code_fingerprints['0x539e20']='01'.repeat(16);
assert.throws(()=>context.validate(full,'nonce'),/code mismatch at 0x539e20/);
assert.throws(()=>context.validate({...full,rotation_diagnostics:1},'nonce'),/must be boolean/);
assert.throws(()=>context.validate({...full,reset_action:false},'nonce'),/requires player_checkpoint and reset_action/);
""")

    def test_packet_offsets_immutable_records_and_full_message_bounds(self):
        output=self.run_node(r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
let bytes;
class P {
 constructor(offset=0){this.offset=offset;}
 add(n){return new P(this.offset+n);} isNull(){return this.offset===0;}
 toString(){return '0xffffffffffffffff';}
 readS32(){return bytes.readInt32LE(this.offset);} readU32(){return bytes.readUInt32LE(this.offset);}
 readU64(){return bytes.readBigUInt64LE(this.offset);} readFloat(){return bytes.readFloatLE(this.offset);}
 readPointer(){return {isNull:()=>true,toString:()=> '0xffffffffffffffff'};}
}
const fail=()=>{throw Error('unexpected native call or mapping query');};
const actor={readPointer:()=>({equals:()=>true}),toString:()=> '0xffffffffffffffff',
 add:()=>({readPointer:()=>({equals:()=>true}),readFloat:()=>-1.2345678586414594e-30})};
let sent;
const context={rpc:{},Process:{id:4294967295,findRangeByAddress:fail},NativeFunction:fail,
 Interceptor:{flush:()=>{}},send:value=>{sent=value;},clearInterval:fail};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const size=vm.runInContext('PACKET_SIZE',context),off=vm.runInContext('OFF',context);
assert.equal(size,1352);assert.equal(off.traceSamples,872);assert.equal(off.traceCode,856);
bytes=Buffer.alloc(size);
for(const name of ['postWarpPosition','postWarpRotation','firstReadbackPosition','firstReadbackRotation',
 'positionBefore','positionAfter','rotationAfter','previousPosition','previousRotation']) {
 const n=/Rotation|rotationAfter/.test(name)?9:3;
 for(let i=0;i<n;++i)bytes.writeFloatLE(-1.2345678586414594e-30,off[name]+i*4);
}
for(const name of ['state','mode','diagnosticPhase','targetHealth','maximum','healthBefore','healthAfter','mirrorAfter',
 'warpCalled','warpReturned','healthWritten','requiredFrames','stableCount','actionEnabled','postWarpCaptured','firstReadbackCaptured'])bytes.writeUInt32LE(1,off[name]);
bytes.writeUInt32LE(3,off.state);bytes.writeUInt32LE(3,off.mode);bytes.writeUInt32LE(15,off.diagnosticPhase);
for(const name of ['frameBefore','frameAfter','warpFrame','firstReadbackFrame','firstStableFrame','lastSampleFrame',
 'actionFrame','actionGuestSp','actionCallbackSp','actionX18After','actionTlsAfter','actionTlrAfter'])
 bytes.writeBigUInt64LE(0xffffffffffffffffn,off[name]);
for(const name of ['stackDelta','actionStackDelta'])bytes.writeBigUInt64LE(65535n,off[name]);
for(const name of ['gateSlot','actionGateSlot'])bytes.writeBigUInt64LE(1023n,off[name]);
for(const name of ['actualTid','actionTid','readbackCount'])bytes.writeUInt32LE(4294967295,off[name]);
for(const name of ['healthBefore','healthAfter','mirrorAfter','firstReadbackHealth','firstReadbackMirror'])bytes.writeUInt32LE(80,off[name]);
bytes.writeUInt32LE(30,off.requiredFrames);bytes.writeUInt32LE(30,off.stableCount);
bytes.writeFloatLE(1.2345678586414594e-30,off.positionDeltaSquared);bytes.writeFloatLE(1.2345678586414594e-30,off.rotationDelta);
const worstVector=Array(12).fill(-1.2345678586414594e-30),addr='0xffffffffffffffff';
context.physicsObservation=()=>({available:true,atomic:false,physics:addr,physics_vtable:addr,model:addr,model_vtable:addr,
 model_flags:4294967295,model_pending_matrix:worstVector,primary_body:addr,controller_set:addr,controller:addr,
 controller_vtable:addr,controller_transform_method:addr,controller_body:addr,body_layout_verified:true,
 body_vtable:addr,body_flags:'18446744073709551615',body_uses_global_scratch:false,body_cache:addr,
 body_pending_matrix:worstVector,body_pending_flags:'18446744073709551615'});
const run={packet:new P(),player:actor,base:{toString:()=>addr},expectedVtable:{},mode:'player_checkpoint',
 rotationDiagnostics:true,nativePhase:'restore',inactive:false,nonce:'f'.repeat(32),timer:null,listener:null,actionListener:null,
 readState:()=>3,armTimings:{reused_module:true,validation_ms:9999,compile_ms:9999,range_queries:64,
 range_cache_hits:9999,range_cache_entries:64,attach_ms:9999,flush_ms:9999,total_ms:99999}};
context.run=run;vm.runInContext('active=run',context);
run.dryrunEvidence=context.metrics(run);
for(let n=0;n<4;++n) {
 const b=off.traceSamples+n*120;
 bytes.writeBigUInt64LE(0xffffffffffffffffn,b);bytes.writeUInt32LE(4294967295,b+8);
 bytes.writeUInt32LE(n+1,b+12);bytes.writeInt32LE(1,b+16);bytes.writeUInt32LE(7,b+20);
 for(const k of [24,28,32,104,108])bytes.writeUInt32LE(4294967295,b+k);
 for(let k=36;k<104;k+=4)bytes.writeFloatLE(-1.2345678586414594e-30,b+k);
 bytes.writeFloatLE(1.9999998807907104,b+116);
}
bytes.writeUInt32LE(1,off.traceSawTarget);bytes.writeBigUInt64LE(0xffffffffffffffffn,off.traceTargetFrame);
const before=Buffer.from(bytes),trace=context.rotationTrace(run);
assert.equal(trace.records[3].frame,'18446744073709551615');assert.equal(trace.records[3].phase,'first_rotation_divergence');
assert.equal(trace.records[3].action[0],4294967295);assert.equal(trace.records[0].validity,7);
assert.equal(trace.bindings.controllerState,addr);assert.equal(trace.bindings.setter,addr);
assert(bytes.equals(before));
let lengths=[],messages=[],snapshots=[];
for(const code of [0,13]) {
 bytes.writeUInt32LE(code,off.error);run.inactive=false;run.result=null;run.timedOut=code!==0;context.finish(run);
 assert.equal(sent.event,code?'failed':'restored');lengths.push(Buffer.byteLength(JSON.stringify(sent)));
 const snap=context.rpc.exports.snapshot();lengths.push(Buffer.byteLength(JSON.stringify(snap)));
 messages.push(sent);snapshots.push(snap);
 assert(lengths[lengths.length-2]<=16384,'message too large: '+lengths[lengths.length-2]);
 assert(lengths[lengths.length-1]<=32768,'snapshot too large: '+lengths[lengths.length-1]);
}
// Corrupt diagnostic data is unavailable; it cannot alter the reset error or result.
bytes.writeUInt32LE(3,off.traceSamples+3*120+20);
assert.equal(context.rotationTrace(run).records[3].direction,null);
const bad={packet:{add(){throw Error('diagnostic read unavailable');}}};
assert.equal(context.rotationTrace(bad).available,false);assert.equal(bytes.readUInt32LE(off.error),13);
console.log(JSON.stringify({lengths,messages,snapshots,phase:{event:'phase',nonce:run.nonce,phase:'dryrun_verified',evidence:run.dryrunEvidence}}));
""")
        payload=json.loads(output)
        self.assertLessEqual(len(json.dumps(dict(type='send',payload=payload['phase']),allow_nan=False).encode()),16384)
        for message in payload['messages']:
            encoded=json.dumps(dict(type='send',payload=message),allow_nan=False).encode()
            self.assertLessEqual(len(encoded),16384)
            self.assertIn('rotation_trace',message['evidence']['diagnostics'])
            self.assertEqual(message['diagnostics']['details'],'evidence.diagnostics')
        for snapshot in payload['snapshots']:
            encoded=json.dumps(dict(event='fast_reset_diagnostic',phase='before_cleanup_snapshot',
                nonce='f'*32,snapshot=snapshot),allow_nan=False).encode()
            self.assertLessEqual(len(encoded),32768)
