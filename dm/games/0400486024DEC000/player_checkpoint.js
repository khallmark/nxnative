'use strict';

// Experimental, session-bound bridge. Loading this file installs no hooks.
// A dryrun must finish in THIS script before a separate warp arm is accepted.
// player_checkpoint performs its own dryrun, then restores player pose and HP.
// This does not restore world objects, camera state, timers, or RNG.
const BUILD_ID = 'e0f3ccfaf50065ae4ea01eb5aeb13b1a';
const SELECTOR_OFFSET = 0x3303ca0;
const WARP_OFFSET = 0xc6fb30;
const ACTION_OFFSET = 0xc08f0;
const ACTION_PHASE_OFFSET = 0xcba80;
const PLAYER_VTABLE_OFFSET = 0x4b25e18;
const PACKET_SIZE = 760;
const OFF = {
  state: 0, mode: 4, expectedTid: 8, actualTid: 12, error: 16,
  restoreResult: 20, expectedInstance: 24, instance: 32, player: 40,
  playerVtable: 48, guestSp: 56, callbackSp: 64, gateValue: 72,
  gateTop: 80, gateSlot: 88, x18Before: 96, x18After: 104,
  stackDelta: 112, returnAddress: 120, frameBefore: 128, frameAfter: 136,
  matrix: 144, positionBefore: 192, positionAfter: 204,
  tlsBefore: 216, tlsAfter: 224, tlrBefore: 232, tlrAfter: 240,
  components: 248, life: 256, hpRecord: 264, maxRecord: 272, lifeVtable: 280,
  hudFlags: 288, plusFlags: 296, hudVtable: 304, plusVtable: 312,
  targetHealth: 320, maximum: 324, healthBefore: 328, healthAfter: 332,
  mirrorAfter: 336, stableCount: 340, requiredFrames: 344, cancelRequested: 348,
  firstStableFrame: 352, lastSampleFrame: 360, tolerance: 368,
  rotationTolerance: 372, rotationAfter: 376, healthWritten: 412,
  warpCalled: 416, warpFrame: 424,
  diagnosticPhase: 432, warpReturned: 436, readbackCount: 440, lastReadbackStable: 444,
  postWarpPosition: 448, postWarpRotation: 460,
  firstReadbackPosition: 496, firstReadbackRotation: 508, firstReadbackFrame: 544,
  firstReadbackHealth: 552, firstReadbackMirror: 556, firstReadbackStable: 560,
  firstReadbackError: 564, postWarpCaptured: 568, firstReadbackCaptured: 572,
  actionEnabled: 576, actionCalled: 580, actionResult: 584, actionSelected: 588,
  actionBsa: 592, actionBsaVtable: 600, actionSlot0: 608,
  actionRequested: 616, actionSlotFlags: 620, actionReturned: 624, actionContextRestored: 628,
  actionX18After: 632, actionTlsAfter: 640, actionTlrAfter: 648,
  actionFrame: 656, actionGuestSp: 664, actionCallbackSp: 672, actionStackDelta: 680,
  actionGateSlot: 688, actionTid: 696, previousPoseValid: 700,
  previousPosition: 704, previousRotation: 716, positionDeltaSquared: 752, rotationDelta: 756,
};

const NATIVE_SOURCE = `
#include <gum/guminterceptor.h>
#include <stddef.h>

typedef struct {
  volatile gint state;
  guint32 mode, expected_tid, actual_tid, error;
  gint restore_result;
  guint64 expected_instance, instance, player, player_vtable;
  guint64 guest_sp, callback_sp, gate_value, gate_top, gate_slot;
  guint64 x18_before, x18_after, stack_delta, return_address;
  guint64 frame_before, frame_after;
  float matrix[12], position_before[3], position_after[3];
  guint64 tls_before, tls_after, tlr_before, tlr_after;
  guint64 components, life, hp_record, max_record, life_vtable;
  guint64 hud_flags, plus_flags, hud_vtable, plus_vtable;
  guint32 target_health, maximum, health_before, health_after, mirror_after;
  guint32 stable_count, required_frames;
  volatile gint cancel_requested;
  guint64 first_stable_frame, last_sample_frame;
  float tolerance, rotation_tolerance, rotation_after[9];
  guint32 health_written, warp_called;
  guint64 warp_frame;
  volatile guint32 diagnostic_phase;
  guint32 warp_returned, readback_count, last_readback_stable;
  float post_warp_position[3], post_warp_rotation[9];
  float first_readback_position[3], first_readback_rotation[9];
  guint64 first_readback_frame;
  guint32 first_readback_health, first_readback_mirror, first_readback_stable;
  guint32 first_readback_error, post_warp_captured, first_readback_captured;
  guint32 action_enabled, action_called;
  gint action_result;
  guint32 action_selected;
  guint64 action_bsa, action_bsa_vtable, action_slot0;
  guint32 action_requested, action_slot_flags, action_returned, action_context_restored;
  guint64 action_x18_after, action_tls_after, action_tlr_after;
  guint64 action_frame, action_guest_sp, action_callback_sp, action_stack_delta, action_gate_slot;
  guint32 action_tid, previous_pose_valid;
  float previous_position[3], previous_rotation[9];
  float position_delta_squared, rotation_delta;
} BridgePacket;
typedef char packet_size_check[sizeof(BridgePacket) == 760 ? 1 : -1];
typedef char packet_matrix_check[offsetof(BridgePacket, matrix) == 144 ? 1 : -1];
typedef char packet_tls_check[offsetof(BridgePacket, tls_before) == 216 ? 1 : -1];
typedef char packet_life_check[offsetof(BridgePacket, life) == 256 ? 1 : -1];
typedef char packet_warp_frame_check[offsetof(BridgePacket, warp_frame) == 424 ? 1 : -1];
typedef char packet_phase_check[offsetof(BridgePacket, diagnostic_phase) == 432 ? 1 : -1];
typedef char packet_post_warp_check[offsetof(BridgePacket, post_warp_position) == 448 ? 1 : -1];
typedef char packet_first_readback_check[offsetof(BridgePacket, first_readback_frame) == 544 ? 1 : -1];
typedef char packet_captured_check[offsetof(BridgePacket, first_readback_captured) == 572 ? 1 : -1];
typedef char packet_action_check[offsetof(BridgePacket, action_bsa) == 592 ? 1 : -1];
typedef char packet_action_context_check[offsetof(BridgePacket, action_x18_after) == 632 ? 1 : -1];
typedef char packet_previous_pose_check[offsetof(BridgePacket, previous_position) == 704 ? 1 : -1];
typedef char packet_motion_delta_check[offsetof(BridgePacket, position_delta_squared) == 752 ? 1 : -1];
extern BridgePacket packet;
extern gsize g_key18, g_keygate, g_keytls, g_keytlr;
extern guint8 dm_gate_stubs[];
extern guint64 dm_gate_top[];
extern guint64 frame_now(void);
extern gpointer pthread_getspecific(gsize key);
extern gint pthread_setspecific(gsize key, gconstpointer value);
extern unsigned char native_cas(gint old_value, gint new_value,
                               volatile gint *address);
extern void warp_guest(gpointer actor, const float *matrix, guint32 flags);
extern gint action_guest(gpointer bsa, const void *request, guint32 flags);
extern guint32 task_self_port;
extern gint region_info(guint32 task, guint64 *address, guint64 *size,
                        gint flavor, gint *info, guint32 *count, guint32 *object);
extern gint port_deallocate(guint32 task, guint32 object);

// States: 1 armed, 2 claimed/awaiting original Main return, 3 finished,
// 4 cancelled, 5 awaiting another read-only validation frame, 6 actor phase claimed,
// 7 Wait accepted; awaiting a strictly later actor update before the one-shot warp.
gint cancel_pending(void) {
  native_cas(0, 1, &packet.cancel_requested);
  if (native_cas(1, 4, &packet.state) || native_cas(5, 4, &packet.state) ||
      native_cas(7, 4, &packet.state))
    packet.diagnostic_phase = 16;
  return packet.state;
}
gint read_state(void) {
  gint state = packet.state;
  native_cas(state, state, &packet.state);
  return packet.state;
}
static void copy_position(float *out) {
  const float *position = (const float *)(gsize)(packet.player + 0x31c);
  guint i;
  for (i = 0; i != 3; ++i) out[i] = position[i];
}
static void copy_rotation(float *out) {
  const float *rotation = (const float *)(gsize)(packet.player + 0x328);
  guint i;
  for (i = 0; i != 9; ++i) out[i] = rotation[i];
}
static guint checkpoint_identity(void) {
  guint hp, maximum;
  if (*(guint64 *)(gsize)(packet.player + 0x270) != packet.components ||
      *(guint64 *)(gsize)(packet.components + 87 * 8) != packet.life ||
      *(guint64 *)(gsize)packet.life != packet.life_vtable ||
      *(guint64 *)(gsize)(packet.life + 0x18) != packet.player ||
      *(guint64 *)(gsize)(packet.life + 0x1c10) != packet.hp_record ||
      *(guint64 *)(gsize)(packet.life + 0x1c18) != packet.max_record) return 8;
  hp = *(volatile guint32 *)(gsize)(packet.hp_record + 8);
  packet.health_after = hp;
  packet.mirror_after = *(volatile guint32 *)(gsize)(packet.life + 0x1ca8);
  maximum = *(volatile guint32 *)(gsize)(packet.max_record + 8);
  if (!hp || hp > maximum || maximum != packet.maximum ||
      *(guint32 *)(gsize)(packet.life + 0x1cac) != maximum) return 8;
  if (*(guint64 *)(gsize)(packet.hud_flags - 0x1a4) != packet.hud_vtable ||
      *(guint64 *)(gsize)(packet.plus_flags - 0x1a4) != packet.plus_vtable ||
      !(*(guint32 *)(gsize)packet.hud_flags & 0x20000) ||
      (*(guint32 *)(gsize)packet.plus_flags & 0x20000)) return 10;
  return 0;
}
static guint writable_health(guint64 address) {
  guint64 region = address, size = 0;
  gint info[16], result;
  guint32 count = 16, object = 0;
  result = region_info(task_self_port, &region, &size, 9, info, &count, &object);
  if (object) port_deallocate(task_self_port, object);
  return result == 0 && count > 0 && (info[0] & 3) == 3 &&
      !(address & 3) && address >= region && size >= 4 && address - region <= size - 4;
}
static guint action_identity(void) {
  if (*(guint64 *)(gsize)(packet.player + 0x270) != packet.components ||
      *(guint64 *)(gsize)(packet.components + 0x188) != packet.action_bsa ||
      *(guint64 *)(gsize)packet.action_bsa != packet.action_bsa_vtable ||
      *(guint64 *)(gsize)(packet.action_bsa + 0x18) != packet.player ||
      *(guint64 *)(gsize)(packet.action_bsa + 0x1c0) != packet.action_slot0) return 14;
  return 0;
}
static void reset_action(gpointer saved_x18) {
  union { guint64 aligned[12]; guint32 words[24]; } request;
  guint32 *words = request.words;
  guint i, selected;
  packet.error = action_identity();
  if (packet.error) return;
  packet.action_slot_flags = *(guint8 *)(gsize)(packet.action_slot0 + 0x7c);
  if (packet.action_slot_flags & 2) { packet.error = 15; return; }
  // Exact normal Wait request shape; null origin is an opt-in experiment.
  for (i = 0; i != 12; ++i) request.aligned[i] = 0;
  words[0] = words[3] = words[6] = words[11] = words[12] = 0x7fc00000;
  words[9] = 4; words[10] = 0x11a; words[13] = 0x10;
  words[18] = 0; words[19] = 40;
  packet.action_called = 1;
  packet.diagnostic_phase = 17;
  packet.action_result = action_guest((gpointer)(gsize)packet.action_bsa, &request, 1);
  packet.action_returned = 1;
  packet.diagnostic_phase = 18;
  packet.action_x18_after = (guint64)(gsize)pthread_getspecific(g_key18);
  packet.restore_result = pthread_setspecific(g_key18, saved_x18);
  packet.action_tls_after = (guint64)(gsize)pthread_getspecific(g_keytls);
  packet.action_tlr_after = (guint64)(gsize)pthread_getspecific(g_keytlr);
  if (packet.restore_result || pthread_getspecific(g_key18) != saved_x18 ||
      packet.action_tls_after != packet.tls_before || packet.action_tlr_after != packet.tlr_before) {
    packet.error = 17; return;
  }
  packet.action_context_restored = 1;
  packet.diagnostic_phase = 19;
  if (packet.action_result) { packet.error = 16; return; }
  packet.error = action_identity();
  if (packet.error) return;
  packet.action_requested = *(guint32 *)(gsize)(packet.action_slot0 + 0x4c);
  selected = *(guint32 *)(gsize)(packet.action_slot0 + 0x28);
  packet.action_selected = selected == 0x11a ? packet.action_requested : selected;
  if (packet.action_requested != 40 || packet.action_selected != 40) packet.error = 18;
}
static guint checkpoint_readback(void) {
  guint i, stable = 1;
  float distance_squared = 0;
  const float *rotation = (const float *)(gsize)(packet.player + 0x328);
  packet.error = checkpoint_identity();
  if (packet.error) return 0;
  copy_position(packet.position_after);
  packet.health_after = *(volatile guint32 *)(gsize)(packet.hp_record + 8);
  packet.mirror_after = *(volatile guint32 *)(gsize)(packet.life + 0x1ca8);
  if (packet.health_after != packet.target_health ||
      packet.mirror_after != packet.target_health) stable = 0;
  for (i = 0; i != 3; ++i) {
    float delta = packet.position_after[i] - packet.matrix[i * 4 + 3];
    distance_squared += delta * delta;
  }
  if (distance_squared != distance_squared || distance_squared > packet.tolerance * packet.tolerance) stable = 0;
  for (i = 0; i != 9; ++i) {
    float delta;
    packet.rotation_after[i] = rotation[i];
    delta = rotation[i] - packet.matrix[(i / 3) * 4 + i % 3];
    if (delta != delta || delta > packet.rotation_tolerance || delta < -packet.rotation_tolerance) stable = 0;
  }
  return stable;
}
// Only distinct guest frames advance this baseline. Duplicate Main callbacks
// must not hide movement by replacing it with a sample from the same frame.
static guint checkpoint_motion_stable(void) {
  guint i, stable = 1;
  packet.position_delta_squared = 0;
  packet.rotation_delta = 0;
  if (packet.previous_pose_valid) {
    if (packet.frame_after != packet.last_sample_frame + 1) stable = 0;
    for (i = 0; i != 3; ++i) {
      float delta = packet.position_after[i] - packet.previous_position[i];
      packet.position_delta_squared += delta * delta;
    }
    for (i = 0; i != 9; ++i) {
      float delta = packet.rotation_after[i] - packet.previous_rotation[i];
      if (delta != delta) stable = 0;
      if (delta < 0) delta = -delta;
      if (delta > packet.rotation_delta) packet.rotation_delta = delta;
    }
    // Settled pilot drift was about .00056/frame; the carried-motion case was
    // about .0225/frame. These per-game limits retain the anchor bounds too.
    if (packet.position_delta_squared != packet.position_delta_squared ||
        packet.position_delta_squared > .005f * .005f ||
        packet.rotation_delta > .005f) stable = 0;
  }
  for (i = 0; i != 3; ++i) packet.previous_position[i] = packet.position_after[i];
  for (i = 0; i != 9; ++i) packet.previous_rotation[i] = packet.rotation_after[i];
  packet.previous_pose_valid = 1;
  return stable;
}
static void enter_bridge(GumInvocationContext *ic, guint actor_phase) {
  guint *claimed = (guint *)gum_invocation_context_get_listener_invocation_data(
      ic, sizeof(guint));
  guint tid = gum_invocation_context_get_thread_id(ic);
  guint64 instance = actor_phase ? packet.expected_instance : (guint64)(gsize)
      gum_invocation_context_get_nth_argument(ic, 0);
  volatile guint64 local_marker = 0;
  guint64 gate_delta, gate_bottom;
  gpointer saved_x18;
  *claimed = 0;
  if (actor_phase) {
    if (!packet.action_enabled || packet.mode != 2 || packet.warp_called ||
        ic->cpu_context->x[20] != packet.player || ic->cpu_context->x[24] != packet.action_bsa) return;
    if (packet.action_called) {
      if (frame_now() <= packet.action_frame || !native_cas(7, 6, &packet.state)) return;
    } else if (!native_cas(1, 6, &packet.state)) return;
  } else {
    if (packet.expected_tid && packet.expected_tid != tid) return;
    if (packet.expected_instance && packet.expected_instance != instance) return;
    // Main cannot execute either action stage. It still bounds a missing actor
    // update after Wait, using a separate guest-frame deadline before warp.
    if (packet.action_enabled && packet.mode == 2) {
      guint64 now = frame_now();
      if (packet.state != 7 || now < packet.action_frame ||
          now - packet.action_frame < 60 || !native_cas(7, 2, &packet.state)) return;
      *claimed = 1;
      packet.frame_before = now;
      packet.error = 19;
      packet.diagnostic_phase = 21;
      return;
    }
    if (!native_cas(1, 2, &packet.state) && !native_cas(5, 2, &packet.state)) return;
  }
  *claimed = 1;
  packet.diagnostic_phase = 1;
  packet.actual_tid = tid;
  packet.instance = instance;
  packet.guest_sp = ic->cpu_context->sp;
  packet.callback_sp = (guint64)(gsize)&local_marker;
  packet.return_address = (guint64)(gsize)
      gum_invocation_context_get_return_address(ic);
  packet.frame_before = frame_now();
  packet.stack_delta = packet.guest_sp > packet.callback_sp
      ? packet.guest_sp - packet.callback_sp
      : packet.callback_sp - packet.guest_sp;
  saved_x18 = pthread_getspecific(g_key18);
  packet.x18_before = (guint64)(gsize)saved_x18;
  packet.x18_after = packet.x18_before;
  packet.tls_before = (guint64)(gsize)pthread_getspecific(g_keytls);
  packet.tlr_before = (guint64)(gsize)pthread_getspecific(g_keytlr);
  packet.tls_after = packet.tls_before;
  packet.tlr_after = packet.tlr_before;
  packet.gate_value = (guint64)(gsize)pthread_getspecific(g_keygate);
  if (packet.gate_value < (guint64)(gsize)dm_gate_stubs) {
    packet.error = 1; return;
  }
  gate_delta = packet.gate_value - (guint64)(gsize)dm_gate_stubs;
  if (gate_delta >= 1024 * 16 || (gate_delta & 15)) {
    packet.error = 1; return;
  }
  packet.gate_slot = gate_delta / 16;
  packet.gate_top = dm_gate_top[packet.gate_slot];
  if (actor_phase && !packet.action_called) {
    packet.action_frame = packet.frame_before;
    packet.action_guest_sp = packet.guest_sp;
    packet.action_callback_sp = packet.callback_sp;
    packet.action_stack_delta = packet.stack_delta;
    packet.action_gate_slot = packet.gate_slot;
    packet.action_tid = tid;
  }
  if (packet.gate_top < (1 << 20)) { packet.error = 2; return; }
  gate_bottom = packet.gate_top - (1 << 20);
  if ((packet.guest_sp >= gate_bottom && packet.guest_sp < packet.gate_top) ||
      (packet.callback_sp >= gate_bottom && packet.callback_sp < packet.gate_top)) {
    packet.error = 3; return;
  }
  if (packet.stack_delta >= 65536 || (packet.guest_sp & 15)) {
    packet.error = 4; return;
  }
  if (*(guint64 *)(gsize)packet.player != packet.player_vtable ||
      *(guint64 *)(gsize)(packet.player + 0x168) != packet.player) {
    packet.error = 5; return;
  }
  if (packet.life) {
    packet.error = checkpoint_identity();
    if (packet.error) return;
  }
  packet.diagnostic_phase = 2;
  if (packet.mode != 3) copy_position(packet.position_before);
  if (packet.mode == 1 || packet.mode == 2) {
    if (packet.mode == 2) {
      packet.diagnostic_phase = 3;
      if (!writable_health(packet.hp_record + 8) || !writable_health(packet.life + 0x1ca8)) {
        packet.error = 12; return;
      }
      packet.health_before = *(volatile guint32 *)(gsize)(packet.hp_record + 8);
      if (*(volatile guint32 *)(gsize)(packet.life + 0x1ca8) != packet.health_before) {
        packet.error = 9; return;
      }
    }
    if (packet.mode == 2 && packet.action_enabled) {
      guint selected;
      if (packet.cancel_requested) return;
      if (!packet.action_called) {
        reset_action(saved_x18);
        // Resume the ordinary scheduler/update before attempting placement.
        // on_action_enter publishes state7 only after context restoration.
        return;
      }
      if (packet.frame_before <= packet.action_frame ||
          packet.frame_before - packet.action_frame >= 60) { packet.error = 19; return; }
      packet.error = action_identity();
      if (packet.error) return;
      if (*(guint8 *)(gsize)(packet.action_slot0 + 0x7c) & 2) { packet.error = 15; return; }
      packet.action_requested = *(guint32 *)(gsize)(packet.action_slot0 + 0x4c);
      selected = *(guint32 *)(gsize)(packet.action_slot0 + 0x28);
      packet.action_selected = selected == 0x11a ? packet.action_requested : selected;
      if (!packet.action_returned || !packet.action_context_restored || packet.action_result ||
          packet.action_requested != 40 || packet.action_selected != 40) { packet.error = 18; return; }
      if (packet.cancel_requested) return;
    }
    packet.diagnostic_phase = 4;
    // Ordinary native ABI call from this C callback's measured stack. No JS FFI.
    packet.warp_frame = packet.frame_before;
    packet.warp_called = 1;
    packet.diagnostic_phase = 5;
    warp_guest((gpointer)(gsize)packet.player, packet.matrix, 0);
    packet.warp_returned = 1;
    packet.diagnostic_phase = 6;
    copy_position(packet.post_warp_position);
    copy_rotation(packet.post_warp_rotation);
    packet.post_warp_captured = 1;
    packet.x18_after = (guint64)(gsize)pthread_getspecific(g_key18);
    packet.restore_result = pthread_setspecific(g_key18, saved_x18);
    if (packet.restore_result || pthread_getspecific(g_key18) != saved_x18)
      packet.error = 6;
    packet.tls_after = (guint64)(gsize)pthread_getspecific(g_keytls);
    packet.tlr_after = (guint64)(gsize)pthread_getspecific(g_keytlr);
    if (packet.tls_after != packet.tls_before || packet.tlr_after != packet.tlr_before)
      packet.error = 7;
    packet.diagnostic_phase = 7;
    if (packet.mode == 2 && !packet.error) {
      packet.diagnostic_phase = 8;
      packet.error = checkpoint_identity();
      if (!packet.error && (!writable_health(packet.hp_record + 8) ||
          !writable_health(packet.life + 0x1ca8))) packet.error = 12;
      if (!packet.error &&
          (!native_cas(packet.health_before, packet.target_health,
             (volatile gint *)(gsize)(packet.hp_record + 8)) ||
           !native_cas(packet.health_before, packet.target_health,
             (volatile gint *)(gsize)(packet.life + 0x1ca8)))) packet.error = 9;
      if (!packet.error) packet.health_written = 1;
      packet.diagnostic_phase = 9;
    }
  }
  copy_position(packet.position_after);
  packet.diagnostic_phase = 10;
}
void on_enter(GumInvocationContext *ic) { enter_bridge(ic, 0); }
void on_action_enter(GumInvocationContext *ic) {
  guint *claimed;
  enter_bridge(ic, 1);
  claimed = (guint *)gum_invocation_context_get_listener_invocation_data(ic, sizeof(guint));
  if (!*claimed) return;
  if (packet.cancel_requested) {
    packet.diagnostic_phase = 16; native_cas(6, 4, &packet.state);
  } else if (packet.error) {
    packet.diagnostic_phase = 15; native_cas(6, 3, &packet.state);
  } else if (!packet.warp_called) {
    packet.diagnostic_phase = 20;
    native_cas(6, 7, &packet.state);
  } else {
    // Only the later actor callback places the player. Main now reads the
    // resulting pose; neither action nor warp is repeated on instability.
    packet.mode = 3;
    packet.diagnostic_phase = 14;
    native_cas(6, 5, &packet.state);
  }
}
void on_leave(GumInvocationContext *ic) {
  guint *claimed = (guint *)gum_invocation_context_get_listener_invocation_data(
      ic, sizeof(guint));
  if (!*claimed) return;
  packet.diagnostic_phase = 11;
  packet.frame_after = frame_now();
  if (packet.cancel_requested) { packet.diagnostic_phase = 16; native_cas(2, 4, &packet.state); return; }
  if (packet.mode >= 2 && !packet.error) {
    guint stable;
    packet.diagnostic_phase = 12;
    ++packet.readback_count;
    stable = checkpoint_readback();
    if (!packet.error && packet.frame_after != packet.last_sample_frame)
      stable = checkpoint_motion_stable() && stable;
    packet.last_readback_stable = stable;
    if (!packet.first_readback_captured) {
      guint i;
      packet.first_readback_frame = packet.frame_after;
      for (i = 0; i != 3; ++i)
        packet.first_readback_position[i] = packet.position_after[i];
      for (i = 0; i != 9; ++i)
        packet.first_readback_rotation[i] = packet.rotation_after[i];
      packet.first_readback_health = packet.health_after;
      packet.first_readback_mirror = packet.mirror_after;
      packet.first_readback_stable = stable;
      packet.first_readback_error = packet.error;
      packet.first_readback_captured = 1;
    }
    packet.diagnostic_phase = 13;
    if (!packet.error && packet.frame_after != packet.last_sample_frame) {
      packet.last_sample_frame = packet.frame_after;
      if (stable) {
        if (!packet.stable_count) packet.first_stable_frame = packet.frame_after;
        ++packet.stable_count;
      } else packet.stable_count = 0;
    }
    if (!packet.error && (packet.stable_count < packet.required_frames ||
        packet.frame_after - packet.first_stable_frame < packet.required_frames - 1)) {
      // A completed warp must settle promptly; cold validation has its own
      // wall-clock budget. Never repeat the warp or health write on failure.
      if (packet.frame_after >= packet.warp_frame &&
          packet.frame_after - packet.warp_frame >= 60) {
        packet.error = 13;
      } else {
        packet.mode = 3;
        packet.diagnostic_phase = 14;
        native_cas(2, 5, &packet.state);
        return;
      }
    }
  }
  packet.diagnostic_phase = 15;
  native_cas(2, 3, &packet.state);
}
`;

let active = null;
let proof = null;
// Compile once per owned script session. Hooks still detach after every result;
// the idle module/packet remain resident until explicit script unload.
let compiled = null;
const rangeCache = [];
let rangeQueries = 0;
let rangeHits = 0;

function hexPointer(value, name) {
  if (typeof value !== 'string' || !/^0x[0-9a-f]+$/i.test(value))
    throw new Error(name + ' must be a hexadecimal address');
  const result = ptr(value);
  if (result.isNull()) throw new Error(name + ' cannot be null');
  return result;
}
function exportAddress(name) {
  return Module.getGlobalExportByName(name);
}
function requireRange(address, size, protection, label) {
  // Mach's range lookup is expensive in this guest's large mapping inventory.
  // Cache only spans verified in this owned process session. All actual code,
  // owner, vtable, component pointers and health values are reread on every arm;
  // a stale unmapped span therefore throws during those reads before native use.
  const end = address.add(size);
  if (end.compare(address) < 0) throw new Error(label + ' range overflows');
  if (rangeCache.some(range => range.protection.includes(protection) &&
      address.compare(range.base) >= 0 && end.compare(range.base.add(range.size)) <= 0)) {
    ++rangeHits;
    return;
  }
  ++rangeQueries;
  const range = Process.findRangeByAddress(address);
  if (!range || !range.protection.includes(protection) ||
      end.compare(range.base.add(range.size)) > 0)
    throw new Error(label + ' has no suitable mapped range');
  if (rangeCache.length >= 64) throw new Error('session mapping cache budget exceeded');
  rangeCache.push(range);
}
function checkpointConfig(config, base, player) {
  const binding = config.address_binding;
  const components = player.add(0x270).readPointer();
  requireRange(components, 88 * 8, 'r', 'components');
  const life = components.add(87 * 8).readPointer();
  requireRange(life, 0x1cb0, 'r', 'life component');
  const lifeVtable = base.add(0x4b4c460);
  if (!life.readPointer().equals(lifeVtable) || !life.add(0x18).readPointer().equals(player))
    throw new Error('life component identity mismatch');
  const hpRecord = life.add(0x1c10).readPointer();
  const maxRecord = life.add(0x1c18).readPointer();
  requireRange(hpRecord, 12, 'w', 'authoritative health record');
  requireRange(maxRecord, 12, 'r', 'maximum health record');
  requireRange(life.add(0x1ca8), 8, 'w', 'health mirror');
  if (binding.health && !hexPointer(binding.health, 'health mirror').equals(life.add(0x1ca8)))
    throw new Error('health mirror binding mismatch');
  const maximum = maxRecord.add(8).readU32();
  const health = hpRecord.add(8).readU32();
  if (maximum < 1 || maximum > 80 || health < 1 || health > maximum ||
      life.add(0x1ca8).readU32() !== health || life.add(0x1cac).readU32() !== maximum)
    throw new Error('living authoritative health/mirror mismatch');
  const hudFlags = hexPointer(binding.hud_flags, 'hud_flags');
  const plusFlags = hexPointer(binding.plus_flags, 'plus_flags');
  const hudVtable = base.add(0x4beb710);
  const plusVtable = base.add(0x4bebaa8);
  for (const [flags, vtable] of [[hudFlags, hudVtable], [plusFlags, plusVtable]]) {
    requireRange(flags.sub(0x1a4), 0x1a8, 'r', 'UI object');
    if (!flags.sub(0x1a4).readPointer().equals(vtable)) throw new Error('UI identity mismatch');
  }
  if (!(hudFlags.readU32() & 0x20000) || (plusFlags.readU32() & 0x20000))
    throw new Error('player checkpoint requires gameplay');
  const targetHealth = config.start_health;
  const start = config.start;
  const rotation = config.target_rotation;
  if (!Number.isInteger(targetHealth) || targetHealth < 1 || targetHealth > maximum)
    throw new Error('start_health is outside current maximum');
  if (!Array.isArray(start) || start.length !== 3 ||
      !start.every(value => Number.isFinite(value) && Math.abs(value) < 1e6))
    throw new Error('finite three-coordinate start required');
  if (!Array.isArray(rotation) || rotation.length !== 9 ||
      !rotation.every(value => Number.isFinite(value) && Math.abs(value) <= 1.01))
    throw new Error('target_rotation requires a finite orthonormal 3x3 matrix');
  for (let i = 0; i < 3; ++i) {
    for (let j = i; j < 3; ++j) {
      const dot = [0, 1, 2].reduce((sum, k) => sum + rotation[i * 3 + k] * rotation[j * 3 + k], 0);
      if (Math.abs(dot - (i === j ? 1 : 0)) > 0.01) throw new Error('target_rotation is not orthonormal');
    }
  }
  const determinant = rotation[0] * (rotation[4] * rotation[8] - rotation[5] * rotation[7]) -
    rotation[1] * (rotation[3] * rotation[8] - rotation[5] * rotation[6]) +
    rotation[2] * (rotation[3] * rotation[7] - rotation[4] * rotation[6]);
  if (Math.abs(determinant - 1) > 0.01) throw new Error('target_rotation must preserve orientation');
  const tolerance = config.start_tolerance === undefined ? 0.3 : config.start_tolerance;
  const rotationTolerance = config.rotation_tolerance === undefined ? 0.02 : config.rotation_tolerance;
  const requiredFrames = config.stable_frames === undefined ? 3 : config.stable_frames;
  if (!Number.isFinite(tolerance) || tolerance < 0.001 || tolerance > 1 ||
      !Number.isFinite(rotationTolerance) || rotationTolerance < 0.001 || rotationTolerance > 0.1 ||
      !Number.isInteger(requiredFrames) || requiredFrames < 2 || requiredFrames > 30)
    throw new Error('invalid checkpoint stability bounds');
  const matrix = rotation.flatMap((_, i) => i % 3 === 0
    ? [rotation[i], rotation[i + 1], rotation[i + 2], start[i / 3]] : []);
  let action = null;
  if (config.reset_action === true) {
    const bsa = hexPointer(binding.action_bsa, 'action_bsa');
    const bsaVtable = hexPointer(binding.action_bsa_vtable, 'action_bsa_vtable');
    const slot0 = hexPointer(binding.action_slot0, 'action_slot0');
    requireRange(bsa, 0x1c8, 'r', 'BSA component');
    requireRange(slot0, 0x80, 'r', 'BSA action slot0');
    if (!components.add(0x188).readPointer().equals(bsa) || !bsa.readPointer().equals(bsaVtable) ||
        !bsa.add(0x18).readPointer().equals(player) || !bsa.add(0x1c0).readPointer().equals(slot0))
      throw new Error('BSA action binding mismatch');
    action = {bsa, bsaVtable, slot0};
  }
  return { components, life, hpRecord, maxRecord, lifeVtable, hudFlags, plusFlags,
    hudVtable, plusVtable, maximum, targetHealth, matrix, tolerance, rotationTolerance, requiredFrames, action };
}
function validate(config, nonce) {
  if (!config || typeof config !== 'object' || typeof nonce !== 'string' || !nonce)
    throw new Error('config and nonempty nonce required');
  if (Process.arch !== 'arm64' || Process.platform !== 'darwin' || Process.pointerSize !== 8)
    throw new Error('this prototype requires Darwin arm64');
  if (config.pid !== Process.id || config.build_id !== BUILD_ID)
    throw new Error('PID/build identity mismatch');
  const base = hexPointer(config.mainbase, 'mainbase');
  const binding = config.address_binding || {};
  const player = hexPointer(binding.player, 'address_binding.player');
  const fingerprints = binding.code_fingerprints;
  if (!fingerprints || typeof fingerprints !== 'object')
    throw new Error('code_fingerprints required');
  if (config.reset_action !== undefined && typeof config.reset_action !== 'boolean')
    throw new Error('reset_action must be boolean');
  if (config.reset_action === true && config.mode !== 'player_checkpoint')
    throw new Error('reset_action requires player_checkpoint mode');
  for (const offset of [SELECTOR_OFFSET, WARP_OFFSET, ...(config.reset_action === true ? [ACTION_OFFSET, ACTION_PHASE_OFFSET] : [])]) {
    const key = '0x' + offset.toString(16);
    const expected = fingerprints[key];
    if (typeof expected !== 'string' || !/^(?:[0-9a-f]{2}){16,64}$/i.test(expected))
      throw new Error('16-64 exact patched code bytes required for ' + key);
    const address = base.add(offset);
    requireRange(address, expected.length / 2, 'x', key);
    const actual = Array.from(new Uint8Array(address.readByteArray(expected.length / 2)))
      .map(value => value.toString(16).padStart(2, '0')).join('');
    if (actual !== expected.toLowerCase()) throw new Error('code mismatch at ' + key);
  }
  requireRange(player, 0x34c, 'r', 'player');
  const expectedVtable = base.add(PLAYER_VTABLE_OFFSET);
  if (!player.readPointer().equals(expectedVtable) ||
      !player.add(0x168).readPointer().equals(player))
    throw new Error('player identity mismatch');
  const mode = config.mode || 'dryrun';
  if (!['dryrun', 'warp', 'player_checkpoint'].includes(mode)) throw new Error('unknown mode');
  const timeout = config.timeout_seconds === undefined ? 10 : config.timeout_seconds;
  if (!Number.isFinite(timeout) || timeout < 0.1 || timeout > 60)
    throw new Error('timeout_seconds must be between 0.1 and 60');
  const identity = [Process.id, BUILD_ID, base.toString(), player.toString(),
    fingerprints['0x3303ca0'].toLowerCase(), fingerprints['0xc6fb30'].toLowerCase(),
    config.reset_action === true ? fingerprints['0xc08f0'].toLowerCase() + ':' +
      fingerprints['0xcba80'].toLowerCase() : 'no-action-reset'].join(':');
  if (mode === 'warp') {
    if (config.confirm_warp !== true || !proof || proof.identity !== identity ||
        config.dryrun_nonce !== proof.nonce)
      throw new Error('warp requires confirm_warp and matching completed dryrun_nonce');
    if (!Array.isArray(config.target_matrix) || config.target_matrix.length !== 12 ||
        !config.target_matrix.every(value => Number.isFinite(value) && Math.abs(value) < 1e7))
      throw new Error('finite row-major Matrix34 target_matrix required');
  }
  const checkpoint = mode === 'player_checkpoint' ? checkpointConfig(config, base, player) : null;
  return { base, player, expectedVtable, mode, timeout, identity, checkpoint };
}
const DIAGNOSTIC_PHASES = ['idle', 'callback_entered', 'identity_checked',
  'permission_check_before_warp', 'ready_for_warp', 'warp_entered', 'warp_returned',
  'thread_context_restored', 'permission_check_before_health', 'health_commit_finished',
  'enter_callback_returning', 'leave_entered', 'readback_entered', 'readback_returned',
  'waiting_stability', 'completed', 'cancelled', 'action_entered', 'action_returned', 'action_context_restored',
  'waiting_later_actor_update', 'pre_warp_deadline'];
function physicsObservation(run) {
  // Bounded, read-only and deliberately non-atomic. Typed body-cache fields
  // require the exact controller transform method used by the inspected build.
  try {
    const components = run.player.add(0x270).readPointer();
    const expected = run.packet.add(OFF.components).readPointer();
    if (expected.isNull() || !components.equals(expected)) throw new Error('components identity unavailable');
    const physics = components.add(0x50).readPointer();
    if (physics.isNull() || !physics.add(0x38).readPointer().equals(run.player))
      throw new Error('physics owner mismatch');
    const model = physics.add(0x20).readPointer();
    if (model.isNull()) throw new Error('physics model unavailable');
    const matrix = address => Array.from({length: 12}, (_, i) => address.add(i * 4).readFloat());
    const result = {available: true, atomic: false, physics: physics.toString(),
      physics_vtable: physics.readPointer().toString(), model: model.toString(),
      model_vtable: model.readPointer().toString(), model_flags: model.add(0x228).readU32(),
      model_pending_matrix: matrix(model.add(0x1d0))};
    const body = model.add(0x190).readPointer();
    const controllerSet = model.add(0x10).readPointer();
    result.primary_body = body.toString();
    result.controller_set = controllerSet.toString();
    if (!controllerSet.isNull()) {
      const controller = controllerSet.add(8).readPointer();
      result.controller = controller.toString();
      if (!controller.isNull()) {
        const vtable = controller.readPointer();
        result.controller_vtable = vtable.toString();
        const method = vtable.add(0xc8).readPointer();
        result.controller_transform_method = method.toString();
        const controllerBody = controller.add(0x20).readPointer();
        result.controller_body = controllerBody.toString();
        const verified = method.equals(run.base.add(0x6b2330)) && !body.isNull() && controllerBody.equals(body);
        result.body_layout_verified = verified;
        if (verified) {
          const flags = body.add(0x70).readU64();
          const flagsLow = body.add(0x70).readU32();
          result.body_vtable = body.readPointer().toString();
          result.body_flags = flags.toString();
          result.body_uses_global_scratch = (flagsLow & 0x100) !== 0;
          const cache = body.add(0x60).readPointer();
          result.body_cache = cache.toString();
          if (!cache.isNull() && !result.body_uses_global_scratch) {
            result.body_pending_matrix = matrix(cache);
            result.body_pending_flags = cache.add(0x100).readU64().toString();
          }
          if (!controller.add(0x20).readPointer().equals(body) ||
              !model.add(0x190).readPointer().equals(body)) throw new Error('physics body changed during snapshot');
        }
        if (!controllerSet.add(8).readPointer().equals(controller))
          throw new Error('physics controller changed during snapshot');
      }
    }
    if (!run.player.add(0x270).readPointer().equals(components) ||
        !components.add(0x50).readPointer().equals(physics) ||
        !physics.add(0x38).readPointer().equals(run.player) ||
        !physics.add(0x20).readPointer().equals(model) ||
        !model.add(0x10).readPointer().equals(controllerSet)) throw new Error('physics chain changed during snapshot');
    return result;
  } catch (error) { return {available: false, atomic: false, error: String(error).slice(0, 160)}; }
}
function diagnostics(run) {
  // Only bounded reads: no NativeFunction, guest call, mapping lookup or cleanup.
  const p = run.packet, stateBefore = p.add(OFF.state).readS32();
  const phaseBefore = p.add(OFF.diagnosticPhase).readU32();
  const readsBefore = p.add(OFF.readbackCount).readU32();
  const u32 = offset => p.add(offset).readU32();
  const frame = offset => p.add(offset).readU64().toString();
  const vector = (offset, count) => Array.from({length: count}, (_, i) => p.add(offset + i * 4).readFloat());
  const result = {version: 1, phase: phaseBefore, phase_name: DIAGNOSTIC_PHASES[phaseBefore] || 'unknown',
    native_mode: u32(OFF.mode), state_before: stateBefore, error_code: u32(OFF.error),
    error_name: ({13: 'post_warp_stability_timeout', 14: 'action_identity_mismatch',
      15: 'action_slot_busy', 16: 'action_request_rejected', 17: 'action_context_mismatch',
      18: 'action_selection_mismatch', 19: 'pre_warp_actor_timeout'})[u32(OFF.error)] || null,
    pre_warp_frame_budget: 60, post_warp_frame_budget: 60,
    motion_delta: {position: Math.sqrt(p.add(OFF.positionDeltaSquared).readFloat()),
      rotation_element: p.add(OFF.rotationDelta).readFloat(), limit: 0.005},
    action_reset: {enabled: u32(OFF.actionEnabled) === 1, called: u32(OFF.actionCalled) === 1,
      returned: u32(OFF.actionReturned) === 1, result: p.add(OFF.actionResult).readS32(),
      context_restored: u32(OFF.actionContextRestored) === 1,
      requested: u32(OFF.actionRequested), selected: u32(OFF.actionSelected),
      slot_flags_before: u32(OFF.actionSlotFlags), frame: frame(OFF.actionFrame),
      thread_id: u32(OFF.actionTid), guest_sp: frame(OFF.actionGuestSp),
      callback_sp: frame(OFF.actionCallbackSp), stack_delta: frame(OFF.actionStackDelta),
      gate_slot: frame(OFF.actionGateSlot),
      x18_after: frame(OFF.actionX18After), tls_after: frame(OFF.actionTlsAfter), tlr_after: frame(OFF.actionTlrAfter)},
    warp_called: u32(OFF.warpCalled) === 1, warp_returned: u32(OFF.warpReturned) === 1,
    warp_frame: frame(OFF.warpFrame), health_written: u32(OFF.healthWritten) === 1,
    health_before: u32(OFF.healthBefore), authoritative_health: u32(OFF.healthAfter),
    mirror_health: u32(OFF.mirrorAfter), readback_count: readsBefore,
    last_readback_stable: u32(OFF.lastReadbackStable) === 1, stable_frames: u32(OFF.stableCount),
    required_frames: u32(OFF.requiredFrames), frame_before: frame(OFF.frameBefore),
    frame_after: frame(OFF.frameAfter), position_before: vector(OFF.positionBefore, 3),
    position_after: vector(OFF.positionAfter, 3), rotation_after: vector(OFF.rotationAfter, 9),
    cancel_requested: u32(OFF.cancelRequested) !== 0,
    post_warp: {captured: u32(OFF.postWarpCaptured) === 1,
      position: vector(OFF.postWarpPosition, 3), rotation: vector(OFF.postWarpRotation, 9)},
    first_readback: {captured: u32(OFF.firstReadbackCaptured) === 1,
      frame: frame(OFF.firstReadbackFrame), position: vector(OFF.firstReadbackPosition, 3),
      rotation: vector(OFF.firstReadbackRotation, 9),
      authoritative_health: u32(OFF.firstReadbackHealth), mirror_health: u32(OFF.firstReadbackMirror),
      stable: u32(OFF.firstReadbackStable) === 1, error_code: u32(OFF.firstReadbackError)}};
  // These direct live observations are explicitly non-atomic and may lag/lead
  // packet samples. Identity failures produce an unavailable record, never writes.
  try {
    if (!run.player.readPointer().equals(run.expectedVtable) ||
        !run.player.add(0x168).readPointer().equals(run.player)) throw new Error('player identity changed');
    const live = {available: true, atomic: false,
      position: [0, 1, 2].map(i => run.player.add(0x31c + i * 4).readFloat()),
      rotation: Array.from({length: 9}, (_, i) => run.player.add(0x328 + i * 4).readFloat())};
    const life = p.add(OFF.life).readPointer(), hpRecord = p.add(OFF.hpRecord).readPointer();
    if (!life.isNull()) {
      if (!life.add(0x18).readPointer().equals(run.player) ||
          !life.add(0x1c10).readPointer().equals(hpRecord)) throw new Error('health identity changed');
      live.authoritative_health = hpRecord.add(8).readU32();
      live.mirror_health = life.add(0x1ca8).readU32();
    }
    result.live_observation = live;
  } catch (error) { result.live_observation = {available: false, error: String(error).slice(0, 160)}; }
  result.physics_observation = physicsObservation(run);
  result.state_after = p.add(OFF.state).readS32();
  result.snapshot_consistent = stateBefore !== 2 && stateBefore !== 6 && stateBefore === result.state_after &&
    phaseBefore === u32(OFF.diagnosticPhase) && readsBefore === u32(OFF.readbackCount);
  return result;
}
function snapshot() {
  if (!active) return {active: false, descriptor_restored: true, diagnostics: null};
  const observed = diagnostics(active);
  return {active: !active.inactive, nonce: active.nonce, state: observed.state_after,
    descriptor_restored: active.inactive, result: active.result, diagnostics: observed};
}
function metrics(run) {
  const p = run.packet;
  const address = offset => p.add(offset).readPointer().toString();
  const vector = offset => [0, 1, 2].map(i => p.add(offset + i * 4).readFloat());
  return {
    mode: run.mode, pid: Process.id, mainbase: run.base.toString(),
    player: run.player.toString(), thread_id: p.add(OFF.actualTid).readU32(),
    main_instance: address(OFF.instance), guest_sp: address(OFF.guestSp),
    callback_local: address(OFF.callbackSp), stack_delta: p.add(OFF.stackDelta).readU64().toString(),
    gate_value: address(OFF.gateValue), gate_top: address(OFF.gateTop),
    gate_slot: p.add(OFF.gateSlot).readU64().toString(),
    virtual_x18_before: address(OFF.x18Before), virtual_x18_after_call: address(OFF.x18After),
    virtual_x18_restore_result: p.add(OFF.restoreResult).readS32(),
    tls_before: address(OFF.tlsBefore), tls_after_call: address(OFF.tlsAfter),
    tlr_before: address(OFF.tlrBefore), tlr_after_call: address(OFF.tlrAfter),
    return_address: address(OFF.returnAddress),
    frame_before: p.add(OFF.frameBefore).readU64().toString(),
    frame_after: p.add(OFF.frameAfter).readU64().toString(),
    frame_available: true,
    position_before: vector(OFF.positionBefore), position_after: vector(OFF.positionAfter),
    error_code: p.add(OFF.error).readU32(),
    semantics: run.mode === 'player_checkpoint' ? 'player_checkpoint' : run.mode,
    authoritative_health: p.add(OFF.healthAfter).readU32(),
    mirror_health: p.add(OFF.mirrorAfter).readU32(),
    health_before: p.add(OFF.healthBefore).readU32(),
    health_written: p.add(OFF.healthWritten).readU32() === 1,
    warp_called: p.add(OFF.warpCalled).readU32() === 1,
    warp_frame: p.add(OFF.warpFrame).readU64().toString(),
    stable_frames: p.add(OFF.stableCount).readU32(),
    first_stable_frame: p.add(OFF.firstStableFrame).readU64().toString(),
    last_stable_frame: p.add(OFF.lastSampleFrame).readU64().toString(),
    rotation_after: Array.from({ length: 9 }, (_, i) => p.add(OFF.rotationAfter + i * 4).readFloat()),
    arm_timings: run.armTimings,
    diagnostics: diagnostics(run),
  };
}
function detach(run) {
  if (run.listener) { run.listener.detach(); run.listener = null; }
  if (run.actionListener) { run.actionListener.detach(); run.actionListener = null; }
  Interceptor.flush();
  if (run.timer) { clearInterval(run.timer); run.timer = null; }
  run.inactive = true;
}
function finish(run) {
  if (active !== run || run.inactive) return;
  const state = run.readState();
  if (state !== 3 && state !== 4) return;
  const evidence = metrics(run);
  const failed = state === 4 || evidence.error_code !== 0 || run.timedOut || run.cancelled;
  if (!failed && run.mode === 'player_checkpoint' && run.nativePhase === 'dryrun') {
    run.dryrunEvidence = evidence;
    run.nativePhase = 'restore';
    run.packet.add(OFF.expectedInstance).writePointer(ptr(evidence.main_instance));
    run.packet.add(OFF.mode).writeU32(2);
    run.packet.add(OFF.state).writeS32(1);
    send({ event: 'phase', nonce: run.nonce, phase: 'dryrun_verified', evidence });
    return;
  }
  detach(run);
  if (!failed && run.mode === 'dryrun') {
    proof = { identity: run.identity, nonce: run.nonce,
      threadId: evidence.thread_id, instance: evidence.main_instance };
  }
  if (failed) proof = null;
  if (run.dryrunEvidence) evidence.dryrun = run.dryrunEvidence;
  run.result = { event: failed ? 'failed' : (run.mode === 'player_checkpoint' ? 'restored' :
      (run.mode === 'dryrun' ? 'dryrun' : 'warped')),
    terminal: true, nonce: run.nonce, descriptor_restored: true,
    diagnostics: evidence.diagnostics,
    semantics: run.mode === 'player_checkpoint' ? 'player_checkpoint' : run.mode,
    error: failed ? 'native checkpoint failed (code ' + evidence.error_code + ')' : undefined,
    reason: run.timedOut ? 'timeout; native callback was allowed to finish' :
      (state === 4 ? 'cancelled before native claim' : undefined), evidence };
  send(run.result);
}

rpc.exports = {
  arm(config, nonce) {
    if (active && !active.inactive) throw new Error('previous arm remains active');
    const began = Date.now();
    const queriesBefore = rangeQueries, hitsBefore = rangeHits;
    const checked = validate(config, nonce);
    const validated = Date.now();
    if (compiled && compiled.identity !== checked.identity)
      throw new Error('resident bridge identity changed; unload before rebinding');
    const reused = compiled !== null;
    const packet = compiled ? compiled.packet : Memory.alloc(PACKET_SIZE);
    packet.writeByteArray(new Uint8Array(PACKET_SIZE));
    packet.add(OFF.mode).writeU32(checked.mode === 'warp' ? 1 : 0);
    packet.add(OFF.player).writePointer(checked.player);
    packet.add(OFF.playerVtable).writePointer(checked.expectedVtable);
    if (checked.checkpoint) {
      const cp = checked.checkpoint;
      for (const name of ['components', 'life', 'hpRecord', 'maxRecord', 'lifeVtable',
        'hudFlags', 'plusFlags', 'hudVtable', 'plusVtable']) packet.add(OFF[name]).writePointer(cp[name]);
      for (const name of ['targetHealth', 'maximum', 'requiredFrames']) packet.add(OFF[name]).writeU32(cp[name]);
      packet.add(OFF.tolerance).writeFloat(cp.tolerance);
      packet.add(OFF.rotationTolerance).writeFloat(cp.rotationTolerance);
      cp.matrix.forEach((value, i) => packet.add(OFF.matrix + i * 4).writeFloat(value));
      if (cp.action) {
        packet.add(OFF.actionEnabled).writeU32(1);
        packet.add(OFF.actionBsa).writePointer(cp.action.bsa);
        packet.add(OFF.actionBsaVtable).writePointer(cp.action.bsaVtable);
        packet.add(OFF.actionSlot0).writePointer(cp.action.slot0);
      }
    }
    if (checked.mode === 'warp') {
      // Main may rotate worker threads. Every claimed invocation repeats the
      // stack/gate checks using that invocation's own pthread TSD and gate slot.
      packet.add(OFF.expectedInstance).writePointer(ptr(proof.instance));
      config.target_matrix.forEach((value, i) => packet.add(OFF.matrix + i * 4).writeFloat(value));
      proof = null; // A successful proof authorizes only this single warp arm.
    } else {
      proof = null;
    }
    const compileBegan = Date.now();
    const module = compiled ? compiled.module : new CModule(NATIVE_SOURCE, {
      packet,
      g_key18: exportAddress('g_key18'), g_keygate: exportAddress('g_keygate'),
      g_keytls: exportAddress('g_keytls'), g_keytlr: exportAddress('g_keytlr'),
      dm_gate_stubs: exportAddress('dm_gate_stubs'), dm_gate_top: exportAddress('dm_gate_top'),
      frame_now: exportAddress('dm_guest_frame'),
      pthread_getspecific: exportAddress('pthread_getspecific'),
      pthread_setspecific: exportAddress('pthread_setspecific'),
      native_cas: exportAddress('OSAtomicCompareAndSwap32Barrier'),
      warp_guest: checked.base.add(WARP_OFFSET),
      action_guest: checked.base.add(ACTION_OFFSET),
      task_self_port: exportAddress('mach_task_self_'),
      region_info: exportAddress('mach_vm_region'),
      port_deallocate: exportAddress('mach_port_deallocate'),
    });
    const compileEnded = Date.now();
    if (!compiled) compiled = { identity: checked.identity, packet, module,
      readState: new NativeFunction(module.read_state, 'int', []),
      cancelPending: new NativeFunction(module.cancel_pending, 'int', []) };
    const run = { ...checked, packet, module, nonce,
      timer: null, listener: null, actionListener: null,
      inactive: false, timedOut: false, cancelled: false, result: null,
      nativePhase: checked.mode === 'player_checkpoint' ? 'dryrun' : checked.mode,
      readState: compiled.readState, cancelPending: compiled.cancelPending,
      armTimings: { reused_module: reused, validation_ms: validated - began,
        compile_ms: compileEnded - compileBegan, range_queries: rangeQueries - queriesBefore,
        range_cache_hits: rangeHits - hitsBefore, range_cache_entries: rangeCache.length },
    };
    active = run;
    try {
      // Armed only after listener installation succeeds. No guest call from JS.
      const attachBegan = Date.now();
      run.listener = Interceptor.attach(checked.base.add(SELECTOR_OFFSET), {
        onEnter: module.on_enter, onLeave: module.on_leave,
      });
      if (checked.checkpoint && checked.checkpoint.action) {
        run.actionListener = Interceptor.attach(checked.base.add(ACTION_PHASE_OFFSET), {
          onEnter: module.on_action_enter,
        });
      }
      const attached = Date.now();
      Interceptor.flush();
      const flushed = Date.now();
      run.armTimings.attach_ms = attached - attachBegan;
      run.armTimings.flush_ms = flushed - attached;
      run.armTimings.total_ms = flushed - began;
      packet.add(OFF.state).writeS32(1);
      const deadline = Date.now() + checked.timeout * 1000;
      run.timer = setInterval(() => {
        if (!run.inactive && Date.now() >= deadline && !run.timedOut) {
          run.timedOut = true;
          run.cancelPending();
          send({ event: 'phase', nonce, phase: 'timeout_pending_cleanup',
            descriptor_restored: false, diagnostics: diagnostics(run) });
        }
        finish(run);
      }, 10);
      send({ event: 'phase', nonce, phase: 'armed', mode: checked.mode, timings: run.armTimings });
      return { armed: true, nonce, mode: checked.mode, timings: run.armTimings };
    } catch (error) {
      // Installation failures occur before publication of the armed packet.
      run.cancelPending();
      if (![2, 6].includes(run.readState())) detach(run);
      throw error;
    }
  },
  status() { return snapshot(); },
  snapshot() { return snapshot(); },
  cancel() {
    if (!active) return { descriptor_restored: true, active: false };
    active.cancelled = true;
    active.cancelPending();
    finish(active);
    return { descriptor_restored: active.inactive, active: !active.inactive,
      nonce: active.nonce, state: active.readState(), diagnostics: diagnostics(active) };
  },
};
