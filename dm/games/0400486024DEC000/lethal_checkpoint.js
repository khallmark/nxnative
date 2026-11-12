'use strict';

// Session-bound prototype. Loading installs no hooks. Neither callback calls guest
// functions: pre-CAS observes; post-CAS asks the host to validate, recover and stop.
// The runner must wait for `recovered` before arming the existing checkpoint warp.
const BUILD_ID = 'e0f3ccfaf50065ae4ea01eb5aeb13b1a';
const PRE = 0x49dcf8, POST = 0x49dd08;
const NATIVE_SOURCE = `
#include <gum/guminterceptor.h>
#include <stddef.h>
typedef struct {
  volatile gint state;
  guint32 error, tid, recovered;
  guint64 epoch, player, life, health, max_address, player_vtable, life_vtable;
  guint32 checkpoint, maximum, old_health, new_health;
  guint64 frame;
} Packet;
typedef struct {
  volatile gint tid;
  guint32 valid;
  guint64 epoch, health;
  guint32 old_health, new_health;
  guint64 life, sp;
} Candidate;
typedef char packet_size[sizeof(Packet) == 96 ? 1 : -1];
typedef char packet_frame[offsetof(Packet, frame) == 88 ? 1 : -1];
typedef char candidate_size[sizeof(Candidate) == 48 ? 1 : -1];
extern Packet packet;
extern Candidate candidates[128];
extern volatile gint callbacks_inflight;
extern unsigned char native_cas(gint old_value, gint new_value, volatile gint *address);
extern gint native_add(gint amount, volatile gint *address);
extern gint host_terminal_restore(guint64 epoch, guint64 address, guint32 old_health,
                                  guint32 new_health, guint32 checkpoint);
extern guint64 frame_now(void);
gint read_state(void) {
  gint state = packet.state;
  native_cas(state, state, &packet.state);
  return packet.state;
}
gint cancel_pending(void) {
  native_cas(0, 5, &packet.state); // Partially installed hooks are not armed yet.
  native_cas(1, 5, &packet.state);
  return read_state();
}
gint read_callbacks(void) { return native_add(0, &callbacks_inflight); }
static Candidate *slot(guint tid, gint create) {
  guint i;
  if (!tid) return NULL;
  for (i = 0; i != 128; ++i) {
    Candidate *s = &candidates[(tid + i) & 127];
    if ((guint)s->tid == tid) return s;
    if (create && !s->tid && native_cas(0, (gint)tid, &s->tid)) return s;
  }
  return NULL;
}
static gint identity(void) {
  guint64 components, record, maximum;
  if (*(guint64 *)(gsize)packet.player != packet.player_vtable ||
      *(guint64 *)(gsize)(packet.player + 0x168) != packet.player ||
      *(guint64 *)(gsize)packet.life != packet.life_vtable ||
      *(guint64 *)(gsize)(packet.life + 0x18) != packet.player) return 0;
  components = *(guint64 *)(gsize)(packet.player + 0x270);
  if (!components || *(guint64 *)(gsize)(components + 0x2b8) != packet.life) return 0;
  record = *(guint64 *)(gsize)(packet.life + 0x1c10);
  maximum = *(guint64 *)(gsize)(packet.life + 0x1c18);
  return record && record + 8 == packet.health && maximum && maximum + 8 == packet.max_address &&
      *(guint32 *)(gsize)packet.max_address == packet.maximum &&
      packet.checkpoint > 0 && packet.checkpoint <= packet.maximum;
}
static void pre_cas_impl(GumInvocationContext *ic) {
  Candidate *s;
  GumCpuContext *cpu = ic->cpu_context;
  guint tid = gum_invocation_context_get_thread_id(ic);
  if (read_state() != 1 || cpu->x[19] != packet.life || cpu->x[13] != packet.health) return;
  s = slot(tid, 1);
  if (!s) return; // No candidate means no recovery, including table exhaustion.
  s->valid = 0;   // Every retry replaces the previous attempt, even if now nonlethal.
  if ((guint32)cpu->x[10] == 0 || (guint32)cpu->x[12] != 0 ||
      (guint32)cpu->x[11] != (guint32)cpu->x[10] || !identity()) return;
  s->epoch = packet.epoch; s->health = cpu->x[13]; s->life = cpu->x[19]; s->sp = cpu->sp;
  s->old_health = (guint32)cpu->x[10]; s->new_health = (guint32)cpu->x[12];
  s->valid = 1;
}
static void post_cas_impl(GumInvocationContext *ic) {
  guint tid = gum_invocation_context_get_thread_id(ic);
  Candidate *s = slot(tid, 0);
  GumCpuContext *cpu = ic->cpu_context;
  if (!s || !s->valid) return;
  s->valid = 0;
  if (read_state() != 1 || s->epoch != packet.epoch || s->life != cpu->x[19] ||
      s->health != cpu->x[13] || s->sp != cpu->sp ||
      s->old_health != (guint32)cpu->x[10] || s->old_health != (guint32)cpu->x[11] ||
      s->new_health != (guint32)cpu->x[12] || !identity()) return;
  if (!native_cas(1, 2, &packet.state)) return;
  packet.tid = tid; packet.old_health = s->old_health; packet.new_health = s->new_health;
  packet.frame = frame_now();
  packet.recovered = host_terminal_restore(packet.epoch, s->health, s->old_health,
                                           s->new_health, packet.checkpoint);
  packet.error = packet.recovered ? 0 : 1;
  native_cas(2, packet.recovered ? 3 : 4, &packet.state);
}
void pre_cas(GumInvocationContext *ic) {
  native_add(1, &callbacks_inflight);
  pre_cas_impl(ic);
  native_add(-1, &callbacks_inflight);
}
void post_cas(GumInvocationContext *ic) {
  native_add(1, &callbacks_inflight);
  post_cas_impl(ic);
  native_add(-1, &callbacks_inflight);
}
`;

let active = null;
let compiled = null;
const rangeCache = [];
let rangeQueries = 0, rangeHits = 0;
function address(value, name) {
  if (typeof value !== 'string' || !/^0x[0-9a-f]+$/i.test(value) || ptr(value).isNull())
    throw new Error(name + ' must be a nonzero hexadecimal pointer');
  return ptr(value);
}
function range(pointer, size, permission, name) {
  const end = pointer.add(size);
  if (end.compare(pointer) < 0) throw new Error(name + ' range overflows');
  if (rangeCache.some(r => r.protection.includes(permission) && pointer.compare(r.base) >= 0 &&
      end.compare(r.base.add(r.size)) <= 0)) {++rangeHits; return;}
  ++rangeQueries;
  const r = Process.findRangeByAddress(pointer);
  if (!r || !r.protection.includes(permission) || pointer.add(size).compare(r.base.add(r.size)) > 0)
    throw new Error(name + ' lacks a suitable mapped range');
  if (rangeCache.length >= 64) throw new Error('session mapping cache budget exceeded');
  rangeCache.push(r);
}
function validate(config, nonce, requireEpoch = true) {
  if (!config || typeof nonce !== 'string' || !nonce || config.confirm_lethal_restore !== true)
    throw new Error('config, nonce and confirm_lethal_restore are required');
  if (Process.arch !== 'arm64' || Process.platform !== 'darwin' || Process.pointerSize !== 8 ||
      config.pid !== Process.id || config.build_id !== BUILD_ID)
    throw new Error('platform/PID/build identity mismatch');
  if (requireEpoch && (typeof config.native_epoch !== 'string' || !/^[1-9][0-9]*$/.test(config.native_epoch) ||
      BigInt(config.native_epoch) > 18446744073709551615n))
    throw new Error('native_epoch must be an exact positive uint64 decimal string');
  const base = address(config.mainbase, 'mainbase'), b = config.address_binding || {};
  const player = address(b.player, 'player'), life = address(b.life, 'life');
  const health = address(b.health, 'authoritative health'), maximum = address(b.max_health_address, 'max health');
  if (health.and(3).toInt32() || maximum.and(3).toInt32()) throw new Error('health alignment mismatch');
  range(player, 0x278, 'r', 'player'); range(life, 0x1c20, 'r', 'life');
  range(health, 4, 'w', 'health'); range(maximum, 4, 'r', 'maximum');
  const playerVtable = base.add(0x4b25e18), lifeVtable = base.add(0x4b4c460);
  if (!player.readPointer().equals(playerVtable) || !player.add(0x168).readPointer().equals(player) ||
      !life.readPointer().equals(lifeVtable) || !life.add(0x18).readPointer().equals(player))
    throw new Error('actor/life identity mismatch');
  const components = player.add(0x270).readPointer(); range(components, 0x2c0, 'r', 'components');
  if (!components.add(0x2b8).readPointer().equals(life) ||
      !life.add(0x1c10).readPointer().add(8).equals(health) ||
      !life.add(0x1c18).readPointer().add(8).equals(maximum)) throw new Error('health owner chain mismatch');
  if (!Number.isInteger(config.max_health) || config.max_health < 1 || config.max_health > 0xffffffff ||
      maximum.readU32() !== config.max_health || !Number.isInteger(config.checkpoint_health) ||
      config.checkpoint_health < 1 || config.checkpoint_health > config.max_health)
    throw new Error('checkpoint health/max mismatch');
  for (const offset of [PRE, POST]) {
    const key = '0x' + offset.toString(16), expected = (b.code_fingerprints || {})[key];
    if (typeof expected !== 'string' || !/^(?:[0-9a-f]{2}){16,64}$/i.test(expected))
      throw new Error('16-64 exact patched code bytes required for ' + key);
    range(base.add(offset), expected.length / 2, 'x', key);
    const actual = Array.from(new Uint8Array(base.add(offset).readByteArray(expected.length / 2)))
      .map(x => x.toString(16).padStart(2, '0')).join('');
    if (actual !== expected.toLowerCase()) throw new Error('code mismatch at ' + key);
  }
  const timeout = config.timeout_seconds === undefined ? 60 : config.timeout_seconds;
  if (!Number.isFinite(timeout) || timeout < 0.1 || timeout > 600) throw new Error('timeout must be 0.1..600 seconds');
  const identity = [Process.id, BUILD_ID, base, player, life, health, maximum,
    b.code_fingerprints['0x49dcf8'].toLowerCase(), b.code_fingerprints['0x49dd08'].toLowerCase()].join(':');
  return {base, player, life, health, maximum, playerVtable, lifeVtable, timeout, identity};
}
function prepareNative(checked) {
  if (compiled) {
    if (compiled.identity !== checked.identity) throw new Error('resident identity changed; unload before rebinding');
    if (compiled.readCallbacks()) throw new Error('prior native callback is still returning');
    return compiled;
  }
  const packet = Memory.alloc(96), candidates = Memory.alloc(128 * 48), callbacks = Memory.alloc(4);
  packet.writeByteArray(new Uint8Array(96)); candidates.writeByteArray(new Uint8Array(128 * 48)); callbacks.writeS32(0);
  const module = new CModule(NATIVE_SOURCE, {packet, candidates, callbacks_inflight: callbacks,
    native_cas: Module.getGlobalExportByName('OSAtomicCompareAndSwap32Barrier'),
    native_add: Module.getGlobalExportByName('OSAtomicAdd32Barrier'),
    host_terminal_restore: Module.getGlobalExportByName('dm_replay_terminal_restore'),
    frame_now: Module.getGlobalExportByName('dm_guest_frame')});
  compiled = {identity: checked.identity, packet, candidates, callbacks, module,
    readState: new NativeFunction(module.read_state, 'int', []),
    readCallbacks: new NativeFunction(module.read_callbacks, 'int', []),
    cancelPending: new NativeFunction(module.cancel_pending, 'int', [])};
  return compiled;
}
function finish(run) {
  if (run.inactive) return;
  const state = run.readState();
  if (![3, 4, 5].includes(state)) return;
  if (run.readCallbacks()) return;
  if (!run.detached) {
    for (const listener of run.listeners) listener.detach();
    Interceptor.flush(); run.detached = true;
  }
  if (run.readCallbacks()) return;
  clearInterval(run.timer); run.inactive = true;
  run.result = {event: state === 3 ? 'recovered' : state === 4 ? 'rejected' : 'cancelled',
    terminal: true, nonce: run.nonce, descriptor_restored: true, native_epoch: run.epoch,
    player: run.player.toString(), health_address: run.health.toString(),
    checkpoint_health: run.packet.add(72).readU32(), old_health: run.packet.add(80).readU32(),
    terminal_health: run.packet.add(84).readU32(), callback_frame: run.packet.add(88).readU64().toString(),
    thread_id: run.packet.add(8).readU32(), reset_requested: state === 3,
    reason: run.timedOut ? 'timeout' : state === 4 ? 'host rejected terminal recovery transaction' : undefined};
  send(run.result);
}
rpc.exports = {
  prepare(config) {
    if (active && !active.inactive) throw new Error('prior arm is active');
    const began = Date.now(), before = rangeQueries, hits = rangeHits;
    const checked = validate(config, 'prepare', false), validated = Date.now(), reused = compiled !== null;
    prepareNative(checked);
    return {prepared: true, hooks_installed: false, health_address: checked.health.toString(),
      timings: {total_ms: Date.now() - began, validation_ms: validated - began,
        compile_ms: Date.now() - validated, reused_module: reused,
        range_queries: rangeQueries - before, range_cache_hits: rangeHits - hits}};
  },
  arm(config, nonce) {
    if (active && !active.inactive) throw new Error('prior arm is active');
    const began = Date.now(), before = rangeQueries, hits = rangeHits;
    const checked = validate(config, nonce), validated = Date.now(), reused = compiled !== null;
    const native = prepareNative(checked), {packet, candidates, module} = native;
    packet.writeByteArray(new Uint8Array(96)); candidates.writeByteArray(new Uint8Array(128 * 48));
    packet.add(16).writeU64(uint64(config.native_epoch));
    [checked.player, checked.life, checked.health, checked.maximum, checked.playerVtable, checked.lifeVtable]
      .forEach((value, index) => packet.add(24 + index * 8).writePointer(value));
    packet.add(72).writeU32(config.checkpoint_health); packet.add(76).writeU32(config.max_health);
    const run = {...checked, ...native, nonce, epoch: config.native_epoch, listeners: [],
      inactive: false, detached: false, timer: null};
    active = run;
    try {
      run.listeners.push(Interceptor.attach(checked.base.add(PRE), {onEnter: module.pre_cas}));
      run.listeners.push(Interceptor.attach(checked.base.add(POST), {onEnter: module.post_cas}));
      Interceptor.flush(); packet.writeS32(1);
      const deadline = Date.now() + checked.timeout * 1000;
      run.timer = setInterval(() => {
        if (Date.now() >= deadline) {run.timedOut = true; run.cancelPending();}
        finish(run);
      }, 10);
      return {armed: true, nonce, native_epoch: run.epoch, health_address: checked.health.toString(),
        timings: {total_ms: Date.now() - began, validation_ms: validated - began,
          reused_module: reused, range_queries: rangeQueries - before, range_cache_hits: rangeHits - hits}};
    } catch (error) {
      // A failed second attach/flush can leave a callback from the first hook
      // returning. Cancellation and detachment do not prove it has quiesced.
      run.cancelPending();
      for (const listener of run.listeners) listener.detach();
      Interceptor.flush(); run.detached = true;
      finish(run); // Publishes cleanup only after the shared callback barrier.
      throw error;
    }
  },
  status() {
    if (!active) return {active: false, descriptor_restored: true};
    finish(active);
    return {active: !active.inactive, descriptor_restored: active.inactive, native_epoch: active.epoch,
      state: active.readState(), result: active.result || null};
  },
  cancel() {
    if (!active) return {active: false, descriptor_restored: true};
    active.cancelPending(); finish(active);
    return {active: !active.inactive, descriptor_restored: active.inactive, native_epoch: active.epoch,
      state: active.readState(), result: active.result || null};
  },
};
