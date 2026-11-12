"""Compile the shipped lethal callback source and exercise its resident JS lifecycle.

No Frida attachment or game process is needed. The C test extracts the real host
terminal transaction, with host input/VM queries stubbed at the platform boundary.
Living checkpoint callback coverage remains in test_evolution_reset.py.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "dm/games/0400486024DEC000/lethal_checkpoint.js"
HOST = REPO / "dm/host/hle_hid.c"

HEADER = r"""#include <stddef.h>
#include <stdint.h>
typedef int gint; typedef unsigned int guint; typedef uint32_t guint32; typedef uint64_t guint64; typedef uintptr_t gsize;
typedef struct { guint64 x[29], fp, lr, sp, pc; } GumCpuContext;
typedef struct { GumCpuContext *cpu_context; guint tid; } GumInvocationContext;
static guint gum_invocation_context_get_thread_id(GumInvocationContext *ic) { return ic->tid; }
"""

HOST_STUBS = r"""
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
typedef uint64_t mach_vm_address_t, mach_vm_size_t;
typedef unsigned int mach_msg_type_number_t, mach_port_t;
typedef int kern_return_t;
typedef int *vm_region_info_t;
typedef struct { int protection; } vm_region_basic_info_data_64_t;
#define VM_REGION_BASIC_INFO_COUNT_64 1
#define VM_REGION_BASIC_INFO_64 9
#define MACH_PORT_NULL 0
#define VM_PROT_WRITE 2
#define KERN_SUCCESS 0
static struct {
    uint64_t epoch, guard_address, stop_frame;
    uint32_t guard_threshold, last_health;
    int health_valid;
    const char *reason;
} g_replay;
static _Atomic int g_replay_on, g_sc_btn;
static pthread_mutex_t g_sync_mu = PTHREAD_MUTEX_INITIALIZER;
static uint64_t test_frame;
static int checks, fails, writable = 1;
#define CHECK(condition, message) do { ++checks; if (!(condition)) { ++fails; fprintf(stderr,"FAIL: %s\n",message); } } while (0)
static unsigned mach_task_self(void) { return 1; }
static int mach_port_deallocate(unsigned task, unsigned object) { (void)task; (void)object; return 0; }
static int mach_vm_region(unsigned task, uint64_t *address, uint64_t *size, int flavor,
                          int *info, unsigned *count, unsigned *object) {
    (void)task; (void)flavor; (void)count; *object = 0;
    if (*address != g_replay.guard_address) return -1;
    *size = sizeof(uint32_t); *info = writable ? VM_PROT_WRITE : 0; return 0;
}
static void replay_stop_locked(const char *reason) {
    g_replay.reason = reason; g_replay.stop_frame = test_frame;
    atomic_store(&g_replay_on,0); atomic_store(&g_sc_btn,0);
}
static void replay_test_stop(void) { if (atomic_load(&g_replay_on)) replay_stop_locked("cancelled"); }
static void test_install(const char *unused, uint64_t address, uint32_t threshold) {
    (void)unused; ++g_replay.epoch; g_replay.guard_address = address;
    g_replay.guard_threshold = threshold; g_replay.reason = "running";
    g_replay.last_health = 0; g_replay.health_valid = 0; g_replay.stop_frame = 0;
    atomic_store(&g_replay_on,1); atomic_store(&g_sc_btn,1);
}
static void test_input(uint64_t frame) {
    test_frame = frame;
    if (atomic_load(&g_replay_on) && *(uint32_t *)(uintptr_t)g_replay.guard_address <= g_replay.guard_threshold) {
        g_replay.last_health = *(uint32_t *)(uintptr_t)g_replay.guard_address;
        g_replay.health_valid = 1; replay_stop_locked("death");
    }
}
"""

HARNESS = r"""Packet packet; Candidate candidates[128]; volatile gint callbacks_inflight;
gint native_add(gint amount, volatile gint *address) { return atomic_fetch_add((_Atomic gint *)address,amount)+amount; }
unsigned char native_cas(gint old_value, gint new_value, volatile gint *address) {
    return atomic_compare_exchange_strong((_Atomic gint *)address, &old_value, new_value);
}
gint host_terminal_restore(guint64 epoch, guint64 address, guint32 old_health, guint32 new_health, guint32 checkpoint) {
    return dm_replay_terminal_restore(epoch, address, old_health, new_health, checkpoint);
}
guint64 frame_now(void) { return test_frame; }
_Alignas(8) static unsigned char player[0x278], life[0x1c20], components[0x2c0];
static uint64_t hp[2], maximum[2];
static void pointer_at(void *base, size_t offset, uintptr_t value) { memcpy((char *)base + offset, &value, sizeof value); }
static void setup(void) {
    replay_test_stop(); memset(&packet, 0, sizeof packet); memset(candidates, 0, sizeof candidates);
    memset(player,0,sizeof player); memset(life,0,sizeof life); memset(components,0,sizeof components);
    packet.epoch = g_replay.epoch + 1; packet.player = (uintptr_t)player; packet.life = (uintptr_t)life;
    packet.health = (uintptr_t)&hp[1]; packet.max_address = (uintptr_t)&maximum[1];
    packet.player_vtable = 0x1234; packet.life_vtable = 0x5678; packet.maximum = 12; packet.checkpoint = 10;
    pointer_at(player,0,packet.player_vtable); pointer_at(player,0x168,(uintptr_t)player); pointer_at(player,0x270,(uintptr_t)components);
    pointer_at(components,0x2b8,(uintptr_t)life); pointer_at(life,0,packet.life_vtable); pointer_at(life,0x18,(uintptr_t)player);
    pointer_at(life,0x1c10,(uintptr_t)hp); pointer_at(life,0x1c18,(uintptr_t)maximum); maximum[1] = 12; hp[1] = 6;
    test_frame = 2000; test_install("f0:A+L0/1~f20",packet.health,0); test_input(2001); packet.state = 1;
}
static GumCpuContext context(uint32_t old, uint32_t newhp) {
    GumCpuContext c = {0}; c.x[10]=old; c.x[11]=old; c.x[12]=newhp; c.x[13]=packet.health; c.x[19]=packet.life; c.sp=0x100000;
    return c;
}
int main(void) {
    setup(); GumCpuContext c=context(6,0); GumInvocationContext ic={&c,123};
    pre_cas(&ic); CHECK(c.x[10]==6 && c.x[11]==6 && c.x[12]==0 && hp[1]==6,"pre-CAS observes without register/HP mutation");
    hp[1]=0; post_cas(&ic);
    CHECK(packet.state==3 && packet.recovered && hp[1]==10 && g_replay.last_health==0 && !atomic_load(&g_sc_btn),"native callback integrates committed death and recovery");
    post_cas(&ic); CHECK(hp[1]==10 && packet.state==3,"duplicate post callback is inert");
    setup(); c=context(6,0); pre_cas(&ic); hp[1]=9; c=context(10,9); pre_cas(&ic); post_cas(&ic);
    CHECK(packet.state==1 && hp[1]==9 && atomic_load(&g_replay_on),"failed lethal CAS followed by nonlethal retry does not count death");
    setup(); c=context(6,0); pre_cas(&ic); test_install("f0:X~f20",packet.health,0); hp[1]=0; post_cas(&ic);
    CHECK(packet.state==4 && hp[1]==0 && atomic_load(&g_replay_on),"replacement epoch rejects old recovery without healing");
    setup(); c=context(6,0); hp[1]=0; post_cas(&ic);
    CHECK(packet.state==1 && hp[1]==0,"post without candidate does not recover");
    setup(); c=context(6,0); pre_cas(&ic); hp[1]=0; ic.tid=124; post_cas(&ic);
    CHECK(packet.state==1 && hp[1]==0,"candidate is isolated by native thread"); ic.tid=123;
    setup(); c=context(6,0); pre_cas(&ic); hp[1]=0; c.sp+=16; post_cas(&ic);
    CHECK(packet.state==1 && hp[1]==0,"mismatched stack rejects stale candidate");
    setup(); c=context(6,0); pre_cas(&ic); hp[1]=0; maximum[1]=8; post_cas(&ic);
    CHECK(packet.state==1 && hp[1]==0,"changed maximum prevents over-healing");
    setup(); c=context(6,0); pre_cas(&ic); hp[1]=0; cancel_pending(); post_cas(&ic);
    CHECK(packet.state==5 && hp[1]==0 && atomic_load(&g_replay_on),"bridge cancel disarms pending recovery");
    setup(); c=context(6,0); pre_cas(&ic); hp[1]=0; test_input(2002); test_frame=2003; post_cas(&ic);
    CHECK(packet.state==3 && hp[1]==10 && g_replay.stop_frame==2002,"earlier HID death recovers without moving stop frame");
    CHECK(read_callbacks()==0,"native callback accounting returns to zero");
    setup(); packet.state=0; CHECK(cancel_pending()==5,"partial installation state can be cancelled");
    replay_test_stop(); printf("lethal native bridge: %d checks, %d failed\n", checks, fails); return fails != 0;
}
"""

RESIDENT_JS = r"""const fs=require('fs'),vm=require('vm'),assert=require('assert');
const cells=new Map(); let next=0x9000000000n,queries=0,compiles=0,attached=0,detached=0; const sent=[]; let failAttachAt=0,failFlushOnce=false;
class P {
 constructor(x){this.n=x instanceof P?x.n:BigInt(x);}
 add(v){return new P(this.n+BigInt(v));} sub(v){return new P(this.n-BigInt(v));} and(v){return new P(this.n&BigInt(v));}
 compare(v){const n=new P(v).n;return this.n<n?-1:this.n>n?1:0;} equals(v){return this.compare(v)===0;}
 isNull(){return this.n===0n;} toString(){return '0x'+this.n.toString(16);} toInt32(){return Number(this.n);}
 readPointer(){return new P(cells.get(this.n)||0n);} writePointer(v){cells.set(this.n,new P(v).n);}
 readU32(){return Number(cells.get(this.n)||0n);} readS32(){return this.readU32();}
 writeU32(v){cells.set(this.n,BigInt(v));} writeS32(v){this.writeU32(v);} writeU64(v){cells.set(this.n,BigInt(v));}
 readU64(){return cells.get(this.n)||0n;}
 readByteArray(n){return new Uint8Array(n).buffer;}
 writeByteArray(b){for(const key of cells.keys())if(key>=this.n&&key<this.n+BigInt(b.length))cells.delete(key);}
}
const base=new P('0x109c30000'),player=new P('0x7ba713d7c0'),life=new P('0x7ba75452d8'),components=new P('0x7ba5000000'),health=new P('0x7ba79f3d20'),max=new P('0x7ba79f4d20');
player.writePointer(base.add(0x4b25e18));player.add(0x168).writePointer(player);player.add(0x270).writePointer(components);components.add(0x2b8).writePointer(life);
life.writePointer(base.add(0x4b4c460));life.add(0x18).writePointer(player);life.add(0x1c10).writePointer(health.sub(8));life.add(0x1c18).writePointer(max.sub(8));max.writeU32(12);health.writeU32(10);
const config={pid:55166,build_id:'e0f3ccfaf50065ae4ea01eb5aeb13b1a',mainbase:base.toString(),confirm_lethal_restore:true,checkpoint_health:10,max_health:12,address_binding:{player:player.toString(),life:life.toString(),health:health.toString(),max_health_address:max.toString(),code_fingerprints:{'0x49dcf8':'00'.repeat(16),'0x49dd08':'00'.repeat(16)}}};
const sandbox={rpc:{exports:{}},ptr:x=>new P(x),uint64:x=>BigInt(x),send:x=>sent.push(x),setInterval:()=>1,clearInterval:()=>{},
 Process:{id:55166,arch:'arm64',platform:'darwin',pointerSize:8,findRangeByAddress(p){queries++;return p.compare(base.add(0x10000000))<0?{base,size:0x10000000,protection:'r-x'}:{base:new P('0x7b00000000'),size:0x100000000,protection:'rw-'};}},
 Memory:{alloc(n){const p=new P(next);next+=BigInt(n+16);return p;}},Module:{getGlobalExportByName:n=>n},
 Interceptor:{attach(){attached++;
   if(attached===failAttachAt){vm.runInContext('compiled.callbacks.writeS32(1)',sandbox);throw Error('second attach failed');}
   return{detach(){detached++;}};},flush(){if(failFlushOnce){failFlushOnce=false;
     vm.runInContext('compiled.callbacks.writeS32(1)',sandbox);throw Error('flush failed');}}},
 NativeFunction:function(f){return f;},
 CModule:class {constructor(source,b){compiles++;this.read_state=()=>b.packet.readS32();this.read_callbacks=()=>b.callbacks_inflight.readS32();this.cancel_pending=()=>{if([0,1].includes(b.packet.readS32()))b.packet.writeS32(5);return b.packet.readS32();};this.pre_cas=()=>{};this.post_cas=()=>{};}}
};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);
assert.equal(attached,0);const api=sandbox.rpc.exports;
const warm=api.prepare(config);assert(warm.prepared&&warm.hooks_installed===false);assert.equal(attached,0);assert.equal(compiles,1);const initialQueries=queries;
assert(api.prepare(config).timings.reused_module);assert.equal(queries,initialQueries);
assert.throws(()=>api.arm(config,'missing_epoch'),/native_epoch/);assert.equal(attached,0);
const arm1=api.arm({...config,native_epoch:'42'},'first');assert(arm1.armed&&arm1.timings.reused_module);assert.equal(attached,2);assert.equal(queries,initialQueries);
assert.throws(()=>api.prepare(config),/prior arm/);vm.runInContext('compiled.callbacks.writeS32(1)',sandbox);assert.equal(api.cancel().descriptor_restored,false);assert.equal(detached,0);vm.runInContext('compiled.callbacks.writeS32(0)',sandbox);assert(api.status().descriptor_restored);assert.equal(detached,2);assert.equal(api.status().result.native_epoch,'42');
const arm2=api.arm({...config,native_epoch:'43'},'second');assert(arm2.armed&&arm2.timings.reused_module);assert.equal(compiles,1);assert.equal(queries,initialQueries);
assert(api.cancel().descriptor_restored);assert.equal(detached,4);assert.equal(api.status().result.native_epoch,'43');
assert.equal(sent.length,2);
// A failed second attach leaves the first listener's callback returning.
failAttachAt=attached+2;
assert.throws(()=>api.arm({...config,native_epoch:'44'},'attach_failure'),/second attach failed/);
assert.equal(api.status().descriptor_restored,false);
assert.equal(api.cancel().descriptor_restored,false);
assert.throws(()=>api.prepare(config),/prior arm/);
assert.throws(()=>api.arm({...config,native_epoch:'45'},'too_soon'),/prior arm/);
vm.runInContext('compiled.callbacks.writeS32(0)',sandbox);
assert(api.status().descriptor_restored);assert.equal(api.status().result.event,'cancelled');
// Flush failure follows the same barrier even though both listeners installed.
failAttachAt=0;failFlushOnce=true;
assert.throws(()=>api.arm({...config,native_epoch:'45'},'flush_failure'),/flush failed/);
assert.equal(api.cancel().descriptor_restored,false);
vm.runInContext('compiled.callbacks.writeS32(0)',sandbox);
assert(api.status().descriptor_restored);assert.equal(api.status().result.event,'cancelled');
assert.equal(compiles,1);assert.equal(queries,initialQueries);
player.writePointer(new P('0xdead'));
assert.throws(()=>api.arm({...config,native_epoch:'46'},'bad_identity'),/identity mismatch/);
console.log('resident JS: inert prepare, exact epoch requirement, cache/module reuse and two arm/cancel cycles passed');
"""

def c_function(source, signature):
    """Extract one whole source function, including its nested blocks."""
    begin = source.index(signature)
    opening = source.index("{", begin)
    depth = 1
    for end in range(opening + 1, len(source)):
        depth += (source[end] == "{") - (source[end] == "}")
        if depth == 0:
            return source[begin:end + 1]
    raise ValueError("Unterminated native source function")


class LethalCheckpointNativeTests(unittest.TestCase):
    def test_actual_lethal_callbacks_and_host_transaction(self):
        compiler = shutil.which("clang")
        if compiler is None:
            self.skipTest("clang unavailable for native callback test")
        source = SCRIPT.read_text().split("const NATIVE_SOURCE = `", 1)[1].split("`;", 1)[0]
        transaction = c_function(HOST.read_text(), "int dm_replay_terminal_restore(")
        with tempfile.TemporaryDirectory(prefix="lethal-checkpoint-test-") as directory:
            root = Path(directory)
            (root / "gum").mkdir()
            (root / "gum/guminterceptor.h").write_text(HEADER)
            source_path = root / "lethal.c"
            source_path.write_text(HOST_STUBS + transaction + "\n" + source + HARNESS)
            binary = root / "lethal"
            built = subprocess.run([compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread",
                "-I", str(root), str(source_path), "-o", str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertIn("13 checks, 0 failed", ran.stdout)

    def test_resident_prepare_cache_epochs_and_partial_attach_cleanup(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node unavailable for resident callback lifecycle test")
        result = subprocess.run([node, "-e", RESIDENT_JS, str(SCRIPT)],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
