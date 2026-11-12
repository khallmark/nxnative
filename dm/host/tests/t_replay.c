// Standalone replay tests reuse the HID test's host stubs and run its baseline suite.
// clang -O1 -Wall -Werror -Wno-unused-function -I.. -I../gen/<GAME> -I../../games/<GAME> -o build/t_replay t_replay.c ../hle_haptics.c ../hle_bigstack.c
#include <mach/mach_vm.h>
#include <stdint.h>
// Exercise the identity bracket deterministically: change one identity just
// after the real matrix read, without racing a writer against the test thread.
static uint64_t camera_test_matrix_address, *camera_test_changed_guard;
static unsigned camera_test_matrix_reads;
static kern_return_t camera_test_read(vm_map_read_t task, mach_vm_address_t address, mach_vm_size_t size,
                                     mach_vm_address_t output, mach_vm_size_t *got) {
    kern_return_t result = mach_vm_read_overwrite(task, address, size, output, got);
    if (camera_test_matrix_address && address == camera_test_matrix_address) {
        ++camera_test_matrix_reads;
        if (camera_test_changed_guard) { *camera_test_changed_guard ^= 1; camera_test_changed_guard = NULL; }
    }
    return result;
}
#define mach_vm_read_overwrite camera_test_read
#define main hid_baseline_main
#include "t_hid.c"
#undef main
#undef mach_vm_read_overwrite

static char replay_test_dir[PATH_MAX];
static void replay_test_stop(void) {
    pthread_mutex_lock(&g_sync_mu);
    if (atomic_load(&g_replay_on)) replay_stop_locked("cancelled");
    pthread_mutex_unlock(&g_sync_mu);
}
static replay_step *test_parse(const char *spec, size_t *n, const char **error) {
    char *copy = strdup(spec); replay_step *steps = replay_parse(copy, n, error); free(copy); return steps;
}
static void test_invalid(const char *spec, const char *why) {
    size_t n = 0; const char *error = NULL; replay_step *steps = test_parse(spec, &n, &error);
    CHECK(!steps && error && !strcmp(error, why), spec); free(steps);
}
static int test_install(const char *spec, uint64_t address, uint32_t threshold) {
    size_t n = 0; const char *error = NULL; replay_step *steps = test_parse(spec, &n, &error);
    if (!steps) { CHECK(0, "test timeline parses"); return 0; }
    error = replay_install(steps, n, address, threshold, NULL);
    if (error) { free(steps); CHECK(0, error); return 0; } return 1;
}
static uint64_t test_input(uint64_t frame) {
    test_frame = frame; keys_sync(); uint64_t value = atomic_load(&g_sc_btn);
    atomic_exchange(&g_sc_latch, 0); atomic_exchange(&g_sc_peek_latch, 0); return value;
}
static void t_replay_parse(void) {
    CHECK(ctl_poll_us(NULL) == 100000 && ctl_poll_us("") == 100000, "control poll default");
    CHECK(ctl_poll_us("1000") == 1000 && ctl_poll_us("1000000") == 1000000, "control poll inclusive bounds");
    CHECK(ctl_poll_us("999") == 100000 && ctl_poll_us("1000001") == 100000 && ctl_poll_us("1e3") == 100000 && ctl_poll_us("+1000") == 100000, "invalid control poll falls back");
    test_invalid("", "empty_timeline");
    test_invalid("0:A~f2", "frame_syntax");
    test_invalid("f0:A~2", "frame_syntax");
    test_invalid("f0:A", "frame_syntax");
    test_invalid("f0:A~f0", "frame_syntax");
    test_invalid("f-1:A~f2", "frame_syntax");
    test_invalid("f+1:A~f2", "frame_syntax");
    test_invalid("f1.5:A~f2", "frame_syntax");
    test_invalid("f0:A~f2*3/4", "frame_syntax");
    test_invalid("f18446744073709551616:A~f1", "frame_syntax");
    test_invalid("f18446744073709551615:A~f1", "frame_overflow");
    test_invalid("f0:A~f3 f2:B~f1", "overlap");
    test_invalid("f2:A~f1 f0:B~f1", "overlap");
    test_invalid("f0:A~f1 f0:B~f1", "overlap");
    test_invalid("f0:Unknown~f1", "key_syntax");
    test_invalid("f0:A+~f1", "key_syntax");
    test_invalid("f0:+A~f1", "key_syntax");
    test_invalid("f0:A++X~f1", "key_syntax");
    test_invalid("f0:Lnan/0~f1", "key_syntax");
    test_invalid("f0:L0/inf~f1", "key_syntax");
    test_invalid("f0:L1.001/0~f1", "key_syntax");
    test_invalid("f0:L0/1junk~f1", "key_syntax");
    test_invalid("f0:T-0.1x0~f1", "key_syntax");
    test_invalid("f0:T0x1.1~f1", "key_syntax");
    size_t n; const char *error = NULL;
    replay_step *steps = test_parse("f0:L0.25/-0.5+R1/0+A+X~f2, f2:LUp+LLeft~f3\nf5:T0.5x0.25~f1\nf6:~f1", &n, &error);
    CHECK(steps && n == 4 && !error, "strict parser accepts analog, diagonal, touch and neutral");
    if (steps) {
        CHECK(steps[0].input.btn == (DM_BTN_A | DM_BTN_X) && steps[0].input.lx == 8192 && steps[0].input.ly == -16384 && steps[0].input.rx == 32767, "strict analog rounding");
        CHECK(steps[1].input.lx == -32767 && steps[1].input.ly == 32767, "cardinal diagonal input");
        CHECK(steps[2].input.tap && steps[2].input.tx == .5f && steps[2].input.ty == .25f, "touch coordinates");
        free(steps);
    }
    steps = test_parse("f9007199254740993:A~f2", &n, &error);
    CHECK(steps && steps[0].at == 9007199254740993ull && steps[0].end == 9007199254740995ull, "integer frames preserve values above double exact range"); free(steps);
    char badguard[] = "health=4:4294967296", goodguard[] = "health=0x1000:0", unaligned[] = "health=1001:0";
    uint64_t address; uint32_t threshold;
    CHECK(replay_guard(badguard, &address, &threshold) != NULL, "guard threshold overflow rejected");
    CHECK(replay_guard(unaligned, &address, &threshold) != NULL, "guard alignment checked");
    CHECK(!replay_guard(goodguard, &address, &threshold) && address == 0x1000 && threshold == 0, "guard hex address parsed");
}
static void t_replay_frames(void) {
    replay_test_stop(); atomic_store(&g_sync_on, 0); test_frame = 100;
    dm_input_set(0, 0, 0, 0, 0); for (int p = 0; p < DM_PLAYERS; p++) dm_pad_set_player(p, 0, 0, 0, 0, 0);
    atomic_store(&g_peek_latch, 0);
    CHECK(test_install("f0:A~f2 f2:A+X~f1 f5:B~f2", 0, 0), "install frame replay");
    uint64_t epoch = g_replay.epoch;
    CHECK(g_replay.start == 101 && g_replay.end == 108 && g_replay.next == 0, "relative replay rebased at current frame+1");
    CHECK(test_input(100) == 0 && test_input(101) == DM_BTN_A && test_input(102) == DM_BTN_A, "hold starts exactly at rebased frame");
    test_frame = 103; keys_sync();
    CHECK(atomic_load(&g_sc_btn) == (DM_BTN_A | DM_BTN_X) && atomic_exchange(&g_sc_latch, 0) == DM_BTN_X, "adjacent steps preserve A hold and latch only new X");
    CHECK(test_input(104) == 0 && test_input(105) == 0 && g_replay.applied == -1, "gap releases input");
    CHECK(test_input(106) == DM_BTN_B && test_input(107) == DM_BTN_B, "later step follows gap");
    CHECK(test_input(108) == 0 && !atomic_load(&g_replay_on) && !strcmp(g_replay.reason, "completed") && g_replay.next == 3, "completion releases at exclusive end");
    CHECK(g_replay.epoch == epoch, "status evolution does not alter epoch");
    test_frame = 200; test_install("f0:A~f10", 0, 0); test_input(201);
    dm_input_set(DM_BTN_B, 0, 0, 0, 0); script_set(DM_BTN_A | DM_BTN_X, 1, 2, 0, 0);
    CHECK(test_install("f0:Y~f1", 0, 0), "active replay replacement");
    uint64_t peek; int32_t axes[4]; dm_input_peek(&peek, axes);
    CHECK(peek == DM_BTN_B && atomic_load(&g_sc_latch) == 0 && !atomic_load(&g_sc_axes), "replacement clears scripted latches and preserves physical edge");
    dm_input_set(0, 0, 0, 0, 0); atomic_store(&g_latch, 0);
    CHECK(test_input(201) == 0 && test_input(202) == DM_BTN_Y && test_input(203) == 0, "replacement rebases without old steps leaking");
    test_frame = 300; test_install("f0:A~f1 f1:B~f1 f4:X~f3", 0, 0);
    CHECK(test_input(303) == 0 && !atomic_load(&g_sc_latch), "missed expired steps do not synthesize late presses");
    CHECK(test_input(305) == DM_BTN_X, "next live step survives skipped frames");
    CHECK(test_input(304) == 0 && !strcmp(g_replay.reason, "frame_regression"), "frame regression fails closed");
    test_frame = 400; test_install("f0:T0.2x0.4~f2 f2:T0.8x0.6~f2", 0, 0);
    test_input(401); int downs = touch_downs; test_input(403);
    CHECK(touch_down && touch_downs == downs && touch_x == .8f, "adjacent touches remain continuous");
    replay_test_stop(); CHECK(!touch_down, "cancel lifts touch");
    size_t n; const char *error = NULL; replay_step *steps = test_parse("f0:A~f1", &n, &error);
    test_frame = UINT64_MAX - 1;
    CHECK(!strcmp(replay_install(steps, n, 0, 0, NULL), "frame_overflow"), "absolute rebase overflow rejected"); free(steps);
    test_frame = 500; atomic_store(&g_legacy_keys_on, 1);
    steps = test_parse("f0:A~f1", &n, &error); epoch = g_replay.epoch;
    CHECK(!strcmp(replay_install(steps, n, 0, 0, NULL), "legacy_seconds_active") && g_replay.epoch == epoch, "legacy seconds playback excludes replacement");
    free(steps); atomic_store(&g_legacy_keys_on, 0);
    atomic_store(&g_sync_on, 1); g_sync_held = 1; test_install("f0:X~f1", 0, 0);
    CHECK(!atomic_load(&g_sync_on) && !g_sync_held, "legacy frame replay is atomically replaced"); replay_test_stop();
}
typedef struct { uint64_t self, tag; float rotation[9]; } camera_fixture;
static replay_camera camera_test_init(camera_fixture *fixture) {
    memset(fixture, 0, sizeof *fixture);
    fixture->self = (uint64_t)(uintptr_t)fixture; fixture->tag = 0x12345678;
    fixture->rotation[0] = fixture->rotation[4] = fixture->rotation[8] = 1;
    return (replay_camera){(uint64_t)(uintptr_t)fixture->rotation,
        (uint64_t)(uintptr_t)&fixture->self, fixture->self,
        (uint64_t)(uintptr_t)&fixture->tag, fixture->tag};
}
static void camera_test_yaw(camera_fixture *fixture, float cosine, float sine) {
    memset(fixture->rotation, 0, sizeof fixture->rotation);
    fixture->rotation[0] = fixture->rotation[8] = cosine;
    fixture->rotation[2] = sine; fixture->rotation[6] = -sine; fixture->rotation[4] = 1;
}
static int test_install_world(const char *spec, const replay_camera *camera, uint64_t health) {
    size_t n = 0; const char *error = NULL; replay_step *steps = test_parse(spec, &n, &error);
    if (!steps) { CHECK(0, "world timeline parses"); return 0; }
    error = replay_install(steps, n, health, 0, camera);
    if (error) { free(steps); CHECK(0, error); return 0; }
    return 1;
}
static void t_replay_world_parse(void) {
    const char *bad[] = {
        "world_xz=", "world_xz=1000:2000:1:2008", "world_xz=1000:2000:1:2008:2:3",
        "world_xz=0:2000:1:2008:2", "world_xz=1001:2000:1:2008:2",
        "world_xz=1000:0:1:2008:2", "world_xz=1000:2001:1:2008:2",
        "world_xz=1000:2000:1:0:2", "world_xz=1000:2000:1:2009:2",
        "world_xz=ffffffffffffffe0:2000:1:2008:2", "world_xz=1000:fffffffffffffff8:1:2008:2",
        "world_xz=1000:2000:1:fffffffffffffff8:2", "world_xz=1000:2000:-1:2008:2",
        "world_xz=1000:2000:10000000000000000:2008:2", "world_xz=1000:2000:1:2008:2junk",
        "world_xz=1000:2000:1::2", "world_xz=1000:2000:1:2008:+2"
    };
    for (size_t i = 0; i < sizeof bad / sizeof *bad; i++) {
        char *copy = strdup(bad[i]); replay_camera camera = {0};
        const char *error = replay_camera_parse(copy, &camera);
        CHECK(error && !strcmp(error, "camera_syntax") && !camera.rotation_address, bad[i]); free(copy);
    }
    char good[] = "world_xz=0x1000:2000:0:0X2008:ffffffffffffffff"; replay_camera camera;
    CHECK(!replay_camera_parse(good, &camera) && camera.rotation_address == 0x1000 &&
          camera.guard1_value == 0 && camera.guard2_value == UINT64_MAX, "camera fields accept exact hex uint64 values including zero guards");
}
static void t_replay_world_math(void) {
    camera_fixture fixture; replay_camera camera = camera_test_init(&fixture);
    float basis[4] = {0}; int32_t x, y; key_step step = {.lx = 32767};
    CHECK(!replay_camera_read(&camera, basis) && replay_world_axes(&step, basis, &x, &y) && x == 32767 && y == 0,
          "identity camera maps world positive X right");
    step.lx = 0; step.ly = 32767;
    CHECK(replay_world_axes(&step, basis, &x, &y) && x == 0 && y == -32767, "identity camera maps world positive Z backward");
    camera_test_yaw(&fixture, 0, 1); step.lx = 32767; step.ly = 0;
    CHECK(!replay_camera_read(&camera, basis) && replay_world_axes(&step, basis, &x, &y) && x == 0 && y == -32767,
          "quarter turn keeps world X direction");
    camera_test_yaw(&fixture, -1, 0);
    CHECK(!replay_camera_read(&camera, basis) && replay_world_axes(&step, basis, &x, &y) && x == -32767 && y == 0,
          "half turn reverses required camera-relative stick");
    camera_test_yaw(&fixture, sqrtf(.5f), sqrtf(.5f));
    CHECK(!replay_camera_read(&camera, basis) && replay_world_axes(&step, basis, &x, &y) && abs(x - 23170) <= 1 && abs(y + 23170) <= 1,
          "diagonal camera produces radial unit stick");
    camera_test_init(&fixture); fixture.rotation[4] = fixture.rotation[8] = sqrtf(.5f);
    fixture.rotation[5] = -sqrtf(.5f); fixture.rotation[7] = sqrtf(.5f);
    step.lx = 8192; step.ly = -16384;
    CHECK(!replay_camera_read(&camera, basis) && replay_world_axes(&step, basis, &x, &y) && x == 8192 && y == 16384,
          "pitched camera preserves horizontal direction and partial strength");
    // Archived live camera capture: pitched and looking almost along world +X.
    const float observed[9] = {.0199851226f, .0864698812f, -.9960539341f,
        -1.58111924e-9f, .9962528944f, .0864871591f, .9998002648f, -.00172845484f, .0199102368f};
    memcpy(fixture.rotation, observed, sizeof observed); step.lx = 32767; step.ly = 0;
    CHECK(!replay_camera_read(&camera, basis) && replay_world_axes(&step, basis, &x, &y) &&
          abs(x - 655) <= 1 && abs(y - 32760) <= 1, "archived pitched camera maps world X to nearly forward stick");
    float skew[] = {1, 0, .6f, -.8f}; step.lx = 0; step.ly = -32767;
    CHECK(replay_world_axes(&step, skew, &x, &y) && fabsf(x + .6f * y) < 2 && y > 0,
          "basis inverse cancels cross-axis motion for nonorthogonal projections");
    step.lx = step.ly = 32767; camera_test_init(&fixture);
    CHECK(!replay_camera_read(&camera, basis) && replay_world_axes(&step, basis, &x, &y) &&
          abs(x - 23170) <= 1 && abs(y + 23170) <= 1, "world diagonal clamps radially instead of clipping axes");
    step.lx = step.ly = 0;
    CHECK(replay_world_axes(&step, basis, &x, &y) && !x && !y, "neutral stays neutral");
    float invalid[] = {NAN, INFINITY, -INFINITY, 2.f};
    for (size_t i = 0; i < sizeof invalid / sizeof *invalid; i++) {
        camera_test_init(&fixture); fixture.rotation[4] = invalid[i];
        CHECK(!strcmp(replay_camera_read(&camera, basis), "camera_basis_error"), "all matrix entries must be finite and rotation bounded");
    }
    camera_test_init(&fixture); fixture.rotation[8] = .01f;
    CHECK(!strcmp(replay_camera_read(&camera, basis), "camera_basis_error"), "near-vertical forward basis rejected");
    camera_test_init(&fixture); fixture.rotation[2] = -1; fixture.rotation[8] = 0;
    CHECK(!strcmp(replay_camera_read(&camera, basis), "camera_basis_error"), "parallel horizontal basis rejected");
    camera_test_init(&fixture); fixture.self ^= 1;
    CHECK(!strcmp(replay_camera_read(&camera, basis), "camera_identity_error"), "first identity mismatch rejected");
    camera_test_init(&fixture); fixture.tag ^= 1;
    CHECK(!strcmp(replay_camera_read(&camera, basis), "camera_identity_error"), "second identity mismatch rejected");
    camera_test_init(&fixture); camera_test_matrix_address = camera.rotation_address;
    camera_test_changed_guard = &fixture.self;
    CHECK(!strcmp(replay_camera_read(&camera, basis), "camera_identity_error"), "identity change during matrix read is rejected by closing bracket");
    camera_test_matrix_address = 0;
}
static void t_replay_world_frames(void) {
    replay_test_stop(); test_frame = 1000;
    camera_fixture fixture; replay_camera camera = camera_test_init(&fixture);
    camera_test_matrix_address = camera.rotation_address; camera_test_matrix_reads = 0;
    CHECK(test_install_world("f0:L1/0+R0.25/-0.5+A+T0.2x0.4~f8 f10:L0/1+X~f2", &camera, 0), "install world timeline");
    CHECK(camera_test_matrix_reads == 1 && g_replay.camera_frame == UINT64_MAX, "installation validates camera without stamping an input frame");
    CHECK(test_input(1000) == 0 && camera_test_matrix_reads == 1, "no camera refresh before start frame");
    CHECK(test_input(1001) == DM_BTN_A && atomic_load(&g_sc_axes) == pack(32767, 0, 8192, -16384), "world input preserves right stick and buttons");
    int downs = touch_downs; unsigned reads = camera_test_matrix_reads;
    camera_test_yaw(&fixture, 0, 1); test_input(1001);
    CHECK(atomic_load(&g_sc_axes) == pack(32767, 0, 8192, -16384) && camera_test_matrix_reads == reads,
          "same-frame HID reads use one camera snapshot");
    dm_input_set(DM_BTN_B, 1234, 2345, 0, 0); dm_pad_set(DM_BTN_Y, -4321, 0, 0, 0);
    test_frame = 1002; keys_sync();
    CHECK(atomic_load(&g_sc_axes) == pack(0, -32767, 8192, -16384) && g_replay.camera_frame == 1002 && g_replay.applied == 0,
          "camera rotates a held gene without advancing the timeline");
    CHECK(!atomic_load(&g_sc_latch) && !atomic_load(&g_sc_peek_latch) && touch_downs == downs && touch_down,
          "camera refresh creates neither new button edges nor repeated touches");
    CHECK(atomic_load(&g_btn) == DM_BTN_B && atomic_load(&g_axes) == pack(1234, 2345, 0, 0) &&
          atomic_load(&g_pad_btn[0]) == DM_BTN_Y && atomic_load(&g_pad_axes[0]) == pack(-4321, 0, 0, 0),
          "world conversion leaves both physical input sources unchanged");
    CHECK(test_input(1009) == 0 && !atomic_load(&g_sc_axes) && !touch_down, "world timeline gap releases stick and touch");
    CHECK(test_input(1011) == DM_BTN_X && atomic_load(&g_sc_axes) == pack(-32767, 0, 0, 0), "world step after gap uses current camera");
    reads = camera_test_matrix_reads; fixture.self ^= 1;
    CHECK(test_input(1013) == 0 && !strcmp(g_replay.reason, "completed") && camera_test_matrix_reads == reads,
          "horizon completion precedes camera identity failure");
    camera_test_init(&fixture); test_frame = 1020; test_install("f0:L0/1~f4", 0, 0);
    test_input(1021); camera_test_yaw(&fixture, -1, 0); test_input(1022);
    CHECK(atomic_load(&g_sc_axes) == pack(0, 32767, 0, 0) && !g_replay.camera.rotation_address,
          "replacement without world option restores ordinary camera-relative semantics");
    camera_test_matrix_address = 0; replay_test_stop();
    dm_input_set(0, 0, 0, 0, 0); dm_pad_set(0, 0, 0, 0, 0);
    atomic_store(&g_latch, 0); atomic_store(&g_peek_latch, 0); atomic_store(&g_pad_latch[0], 0);
}
static void t_replay_world_failures(void) {
    camera_fixture fixture; replay_camera camera = camera_test_init(&fixture);
    replay_test_stop(); test_frame = 1100; test_install("f0:A~f20", 0, 0); test_input(1101);
    uint64_t epoch = g_replay.epoch; size_t n; const char *error = NULL;
    replay_step *steps = test_parse("f0:X~f4", &n, &error); fixture.tag ^= 1;
    error = replay_install(steps, n, 0, 0, &camera);
    CHECK(error && !strcmp(error, "camera_identity_error") && g_replay.epoch == epoch &&
          atomic_load(&g_sc_btn) == DM_BTN_A && steps[0].at == 0, "invalid world installation preserves current replay and unre-based input"); free(steps);
    camera_test_init(&fixture); test_install_world("f0:A+L1/0~f20", &camera, 0); test_input(1102);
    dm_input_set(DM_BTN_B, 400, 0, 0, 0); fixture.rotation[0] = NAN;
    CHECK(test_input(1103) == 0 && !strcmp(g_replay.reason, "camera_basis_error") && !atomic_load(&g_sc_axes) &&
          !atomic_load(&g_sc_latch) && atomic_load(&g_btn) == DM_BTN_B, "bad camera stops scripted input and preserves physical input");
    uint32_t health = 10; camera_test_init(&fixture);
    test_install_world("f0:A~f20", &camera, (uint64_t)(uintptr_t)&health); test_input(1104); epoch = g_replay.epoch;
    health = 0; fixture.tag ^= 1;
    CHECK(test_input(1105) == 0 && !strcmp(g_replay.reason, "death") && g_replay.last_health == 0 &&
          g_replay.stop_frame == 1105 && g_replay.epoch == epoch, "health death wins over simultaneous camera failure");
    health = 10; camera_test_init(&fixture);
    test_install_world("f0:X~f20", &camera, (uint64_t)(uintptr_t)&health); test_input(1106); epoch = g_replay.epoch;
    CHECK(dm_replay_terminal(epoch, (uint64_t)(uintptr_t)&health, 10, 0) && !strcmp(g_replay.reason, "death") &&
          !atomic_load(&g_sc_axes), "native terminal release works with world mode");
    mach_vm_address_t page = 0;
    CHECK(mach_vm_allocate(mach_task_self(), &page, 4096, VM_FLAGS_ANYWHERE) == KERN_SUCCESS, "allocate camera test page");
    if (page) {
        camera_test_init(&fixture); memcpy((void *)(uintptr_t)page, fixture.rotation, sizeof fixture.rotation);
        camera.rotation_address = page; test_install_world("f0:A~f10", &camera, 0); test_input(1107);
        CHECK(mach_vm_deallocate(mach_task_self(), page, 4096) == KERN_SUCCESS, "unmap camera test page");
        CHECK(test_input(1108) == 0 && !strcmp(g_replay.reason, "camera_read_error"), "invalidated camera mapping stops without direct dereference");
    }
    camera = camera_test_init(&fixture); test_install_world("f0:A~f10", &camera, 0); test_input(1109);
    fixture.self ^= 1;
    CHECK(test_input(1110) == 0 && !strcmp(g_replay.reason, "camera_identity_error"), "changed active camera binding stops replay");
    replay_test_stop(); dm_input_set(0, 0, 0, 0, 0); atomic_store(&g_latch, 0); atomic_store(&g_peek_latch, 0);
}
static void t_replay_health(void) {
    uint32_t health = 6; test_frame = 600;
    test_install("f0:A~f20", (uint64_t)(uintptr_t)&health, 5);
    CHECK(test_input(601) == DM_BTN_A && g_replay.health_valid && g_replay.last_health == 6, "safe native health read");
    health = 5; CHECK(test_input(601) == DM_BTN_A, "guard reads once per frame");
    CHECK(test_input(602) == 0 && !strcmp(g_replay.reason, "death") && g_replay.last_health == 5, "death threshold releases input");
    CHECK(!atomic_load(&g_sc_latch) && !atomic_load(&g_sc_peek_latch) && !atomic_load(&g_sc_axes), "death clears pending scripted presses");
    test_input(610); CHECK(g_replay.stop_frame == 602, "death frame survives later observations");
    test_install("f0:A~f20", 4, 0);
    CHECK(test_input(603) == 0 && !strcmp(g_replay.reason, "guard_read_error") && !g_replay.health_valid, "unreadable pointer stops without dereference");
    mach_vm_address_t page = 0;
    CHECK(mach_vm_allocate(mach_task_self(), &page, 4096, VM_FLAGS_ANYWHERE) == KERN_SUCCESS, "allocate guarded test page");
    if (page) {
        *(uint32_t *)(uintptr_t)page = 10;
        test_install("f0:X~f20", page, 0); CHECK(test_input(604) == DM_BTN_X, "mapped guard permits input");
        CHECK(mach_vm_deallocate(mach_task_self(), page, 4096) == KERN_SUCCESS, "unmap guarded test page");
        CHECK(test_input(605) == 0 && !strcmp(g_replay.reason, "guard_read_error"), "invalidated guard stops an active replay");
    }
    health = 0; test_install("f0:X~f2", (uint64_t)(uintptr_t)&health, 0);
    CHECK(test_input(606) == 0 && !strcmp(g_replay.reason, "death"), "already dead state never presses");
}
static void test_terminal_rejected(uint64_t epoch, uint64_t address, uint32_t old_health, uint32_t new_health, const char *why) {
    unsigned char before[sizeof g_replay], after[sizeof g_replay]; memcpy(before, &g_replay, sizeof before);
    int active = atomic_load(&g_replay_on), down = touch_down, downs = touch_downs;
    uint64_t buttons = atomic_load(&g_sc_btn), axes = atomic_load(&g_sc_axes);
    uint64_t latch = atomic_load(&g_sc_latch), peek = atomic_load(&g_sc_peek_latch);
    CHECK(!dm_replay_terminal(epoch, address, old_health, new_health), why);
    memcpy(after, &g_replay, sizeof after);
    CHECK(!memcmp(before, after, sizeof before) && atomic_load(&g_replay_on) == active &&
          atomic_load(&g_sc_btn) == buttons && atomic_load(&g_sc_axes) == axes &&
          atomic_load(&g_sc_latch) == latch && atomic_load(&g_sc_peek_latch) == peek &&
          touch_down == down && touch_downs == downs, "rejected terminal preserves replay and scripted input");
}
static void t_replay_terminal(void) {
    replay_test_stop(); test_frame = 620;
    uint32_t health = 6; uint64_t address = (uint64_t)(uintptr_t)&health;
    CHECK(test_install("f0:A+L0.25/-0.5+T0.2x0.4~f20", address, 5), "install terminal replay");
    uint64_t stale_epoch = g_replay.epoch;
    CHECK(test_install("f0:A+L0.25/-0.5+T0.2x0.4~f20", address, 5), "replace terminal replay");
    uint64_t epoch = g_replay.epoch;
    test_frame = 621; keys_sync();
    CHECK(atomic_load(&g_sc_btn) == DM_BTN_A && atomic_load(&g_sc_axes) &&
          atomic_load(&g_sc_latch) == DM_BTN_A && atomic_load(&g_sc_peek_latch) == DM_BTN_A && touch_down,
          "terminal test has held axes, touch and pending button edges");
    test_terminal_rejected(stale_epoch, address, 6, 5, "old replay epoch cannot stop replacement");
    test_terminal_rejected(epoch, address + sizeof health, 6, 5, "wrong health address rejected");
    test_terminal_rejected(epoch, address, 7, 6, "nonterminal health result rejected");
    test_terminal_rejected(epoch, address, 5, 0, "already terminal old health rejected");
    dm_input_set(DM_BTN_B, 1234, 0, 0, 0);
    CHECK(dm_replay_terminal(epoch, address, 6, 5), "matching committed threshold crossing accepted in same frame");
    CHECK(!atomic_load(&g_replay_on) && !strcmp(g_replay.reason, "death") && g_replay.stop_frame == 621 &&
          g_replay.epoch == epoch && g_replay.health_valid && g_replay.last_health == 5,
          "native terminal freezes current death frame and event health without changing epoch");
    CHECK(!atomic_load(&g_sc_btn) && !atomic_load(&g_sc_axes) && !atomic_load(&g_sc_latch) &&
          !atomic_load(&g_sc_peek_latch) && !touch_down, "native terminal immediately releases scripted inputs");
    CHECK(atomic_load(&g_btn) == DM_BTN_B && atomic_load(&g_axes) == pack(1234, 0, 0, 0) &&
          (atomic_load(&g_latch) & DM_BTN_B) && (atomic_load(&g_peek_latch) & DM_BTN_B),
          "native terminal preserves physical input and edges");
    // The game has already recovered before the next HID read; fitness keeps the event.
    health = 12; test_input(630);
    CHECK(g_replay.stop_frame == 621 && g_replay.last_health == 5 && g_replay.epoch == epoch,
          "health recovery and later HID reads cannot replace terminal timing or health");
    test_terminal_rejected(epoch, address, 6, 0, "duplicate inactive terminal rejected");
    dm_input_set(0, 0, 0, 0, 0); atomic_store(&g_latch, 0); atomic_store(&g_peek_latch, 0);
    CHECK(test_install("f0:X~f20", 0, 0), "install replay without a health guard");
    test_terminal_rejected(g_replay.epoch, 0, 1, 0, "unguarded replay cannot accept terminal event");
    replay_test_stop();
    CHECK(test_install("f0:X~f20", address, 0), "install zero-threshold terminal replay");
    epoch = g_replay.epoch; test_input(631); test_frame = 633;
    CHECK(dm_replay_terminal(epoch, address, 1, 0) && g_replay.stop_frame == 633 &&
          g_replay.last_frame == 631 && g_replay.last_health == 0 && g_replay.epoch == epoch,
          "terminal frame is callback frame, even before the next HID read");
}
static void t_replay_terminal_restore(void) {
    replay_test_stop(); test_frame = 640;
    uint32_t health = 6; uint64_t address = (uint64_t)(uintptr_t)&health;
    CHECK(test_install("f0:A+L0/1~f20", address, 0), "install terminal recovery replay");
    uint64_t epoch = g_replay.epoch; test_input(641); health = 0;
    CHECK(!dm_replay_terminal_restore(epoch - 1, address, 6, 0, 10) && !health && atomic_load(&g_replay_on),
          "stale epoch cannot recover health");
    CHECK(!dm_replay_terminal_restore(epoch, address + sizeof health, 6, 0, 10) && !health && atomic_load(&g_replay_on),
          "wrong guard cannot recover health");
    CHECK(!dm_replay_terminal_restore(epoch, address, 0, 0, 10) && !health &&
          !dm_replay_terminal_restore(epoch, address, 6, 1, 10) && !health &&
          !dm_replay_terminal_restore(epoch, address, 6, 0, 0) && !health && atomic_load(&g_replay_on),
          "invalid crossing or terminal recovery HP rejected");
    health = 4;
    CHECK(!dm_replay_terminal_restore(epoch, address, 6, 0, 10) && health == 4 && atomic_load(&g_replay_on),
          "failed health CAS leaves replay active and health unchanged");
    health = 0; test_frame = 643;
    CHECK(dm_replay_terminal_restore(epoch, address, 6, 0, 10) && health == 10 && !atomic_load(&g_replay_on) &&
          g_replay.last_health == 0 && g_replay.health_valid && g_replay.stop_frame == 643 && g_replay.epoch == epoch &&
          !strcmp(g_replay.reason, "death") && !atomic_load(&g_sc_btn) && !atomic_load(&g_sc_axes),
          "successful CAS recovers HP and freezes terminal truth with input release");
    CHECK(!dm_replay_terminal_restore(epoch, address, 6, 0, 10) && health == 10 && g_replay.stop_frame == 643,
          "duplicate recovery cannot change HP or terminal frame");
    test_frame = 650; test_install("f0:X~f20", address, 0); epoch = g_replay.epoch; test_input(651);
    health = 0; test_input(652); test_frame = 653;
    CHECK(dm_replay_terminal_restore(epoch, address, 10, 0, 10) && health == 10 && g_replay.stop_frame == 652,
          "HID observing zero first still allows same-epoch recovery and keeps earlier death frame");
    test_install("f0:X~f20", address, 0); epoch = g_replay.epoch; replay_test_stop(); health = 0;
    CHECK(!dm_replay_terminal_restore(epoch, address, 10, 0, 10) && !health && !strcmp(g_replay.reason, "cancelled"),
          "cancelled replay cannot recover health");
    health = 10; test_frame = 660; test_install("f0:X~f1", address, 0); epoch = g_replay.epoch;
    test_input(661); test_input(662); health = 0;
    CHECK(!dm_replay_terminal_restore(epoch, address, 10, 0, 10) && !health && !strcmp(g_replay.reason, "completed"),
          "completed replay cannot recover health");
    health = 10; test_install("f0:X~f20", address, 0); epoch = g_replay.epoch;
    test_install("f0:A~f20", address, 0); health = 0;
    CHECK(!dm_replay_terminal_restore(epoch, address, 10, 0, 10) && !health && atomic_load(&g_replay_on),
          "replaced replay cannot recover its former guard");
    test_install("f0:X~f20", 4, 0); epoch = g_replay.epoch;
    CHECK(!dm_replay_terminal_restore(epoch, 4, 10, 0, 10) && atomic_load(&g_replay_on), "unmapped recovery address safely rejected");
    mach_vm_address_t page = 0;
    CHECK(mach_vm_allocate(mach_task_self(), &page, 4096, VM_FLAGS_ANYWHERE) == KERN_SUCCESS, "allocate recovery guard test page");
    if (page) {
        *(uint32_t *)(uintptr_t)page = 0; test_install("f0:X~f20", page, 0); epoch = g_replay.epoch;
        CHECK(mach_vm_protect(mach_task_self(), page, 4096, 0, VM_PROT_READ) == KERN_SUCCESS, "protect recovery page read-only");
        CHECK(!dm_replay_terminal_restore(epoch, page, 10, 0, 10) && !*(uint32_t *)(uintptr_t)page && atomic_load(&g_replay_on),
              "read-only recovery storage rejected without a write fault");
        CHECK(mach_vm_deallocate(mach_task_self(), page, 4096) == KERN_SUCCESS, "remove recovery guard test page");
    }
    replay_test_stop();
}
static void test_write(const char *path, const char *text) {
    FILE *f = fopen(path, "w"); CHECK(f != NULL, "create test protocol file"); if (f) { CHECK(fputs(text, f) >= 0, "write test protocol file"); CHECK(!fclose(f), "close test protocol file"); }
}
static char *test_command(const char *command, const char *ack) {
    char *copy = strdup(command); CHECK(replay_command(copy), "native command dispatched"); free(copy);
    const char *error = NULL; char *text = replay_file(ack, 1u << 20, &error);
    CHECK(text != NULL, "atomic JSON response exists"); return text;
}
static void t_replay_protocol(void) {
    char timeline[PATH_MAX], request[PATH_MAX], ack[PATH_MAX], command[PATH_MAX * 3];
    snprintf(timeline, sizeof timeline, "%s/timeline", replay_test_dir);
    snprintf(request, sizeof request, "%s/request", replay_test_dir);
    snprintf(ack, sizeof ack, "%s/ack", replay_test_dir);
    test_write(timeline, "f0:L0/1+A~f3\nf3:L0/1+A+X~f2\nf5:~f1\n");
    test_frame = 700; snprintf(command, sizeof command, "replay %s %s", timeline, ack);
    char *json = test_command(command, ack);
    CHECK(json && strstr(json, "\"protocol\":1") && strstr(json, "\"ok\":true") && strstr(json, "\"start_frame\":701") && strstr(json, "\"steps\":3"), "start ACK exposes accepted timeline"); free(json);
    test_input(701); uint64_t epoch = g_replay.epoch;
    test_write(timeline, "f0:Bad~f2"); json = test_command(command, ack);
    CHECK(json && strstr(json, "\"ok\":false") && strstr(json, "\"error\":\"key_syntax\"") && g_replay.epoch == epoch && atomic_load(&g_sc_btn) == DM_BTN_A, "rejected replay leaves current timeline intact"); free(json);
    snprintf(command, sizeof command, "replay_status %s", ack); json = test_command(command, ack);
    CHECK(json && strstr(json, "\"index\":1") && strstr(json, "\"step\":0") && strstr(json, "\"active\":true"), "status reports native current step"); free(json);
    uint32_t health = 0x12345678; char reads[256]; snprintf(reads, sizeof reads, "%llx 4\n4 4\n", (unsigned long long)(uintptr_t)&health); test_write(request, reads);
    snprintf(command, sizeof command, "replay_observe %s %s", request, ack); json = test_command(command, ack);
    CHECK(json && strstr(json, "\"hex\":\"78563412\"") && strstr(json, "\"hex\":null,\"error\":\"read_error\"") && strstr(json, "\"observed_frame_end\":701"), "observer returns bounded bytes and explicit safe-read failures"); free(json);
    test_write(request, "4 4097\n"); json = test_command(command, ack);
    CHECK(json && strstr(json, "\"ok\":false") && strstr(json, "\"reads\":[]"), "invalid request returns no partial observation"); free(json);
    char many[2048] = {0}; for (int i = 0; i < 129; i++) strcat(many, "4 1\n");
    test_write(request, many); json = test_command(command, ack);
    CHECK(json && strstr(json, "\"error\":\"read_limit\""), "observer enforces count limit"); free(json);
    memset(many, 0, sizeof many); for (int i = 0; i < 17; i++) strcat(many, "4 4096\n");
    test_write(request, many); json = test_command(command, ack);
    CHECK(json && strstr(json, "\"error\":\"read_limit\""), "observer enforces total bytes limit"); free(json);
    snprintf(command, sizeof command, "replay_cancel %s", ack); json = test_command(command, ack);
    CHECK(json && strstr(json, "\"active\":false") && strstr(json, "\"reason\":\"cancelled\"") && !atomic_load(&g_sc_btn), "cancel ACK follows release"); free(json);
    snprintf(command, sizeof command, "replay %s %s health=4:0", timeline, ack); test_write(timeline, "f0:A~f2"); json = test_command(command, ack);
    CHECK(json && strstr(json, "\"guard_address\":\"0x4\""), "command accepts explicit guard"); free(json);
    test_input(702); CHECK(!strcmp(g_replay.reason, "guard_read_error"), "command guard is applied natively");
    snprintf(command, sizeof command, "replay %s %s extra extra", timeline, ack); json = test_command(command, ack);
    CHECK(json && strstr(json, "\"error\":\"command_syntax\""), "extra command arguments rejected"); free(json);
    char fifo[PATH_MAX]; snprintf(fifo, sizeof fifo, "%s/fifo", replay_test_dir); CHECK(!mkfifo(fifo, 0600), "create nonregular test input");
    const char *error = NULL; char *text = replay_file(fifo, 1024, &error);
    CHECK(!text && error && !strcmp(error, "file_size"), "nonregular timeline rejected without blocking"); free(text);
    char blocked[PATH_MAX]; snprintf(blocked, sizeof blocked, "%s/blocked", replay_test_dir);
    CHECK(!mkdir(blocked, 0700), "create ACK publication failure target");
    snprintf(command, sizeof command, "replay %s %s", timeline, blocked);
    CHECK(replay_command(command) && !atomic_load(&g_replay_on) && !strcmp(g_replay.reason, "ack_error"), "failed ACK publication releases newly accepted replay");
    CHECK(!rmdir(blocked), "remove ACK failure target");
    unlink(fifo); unlink(timeline); unlink(request); unlink(ack);
}
static void t_replay_world_protocol(void) {
    char timeline[PATH_MAX], ack[PATH_MAX], command[PATH_MAX * 3], option[256], expected[160];
    snprintf(timeline, sizeof timeline, "%s/world-timeline", replay_test_dir);
    snprintf(ack, sizeof ack, "%s/world-ack", replay_test_dir);
    test_write(timeline, "f0:L1/0+A~f8");
    camera_fixture fixture; replay_camera camera = camera_test_init(&fixture); uint32_t health = 10;
    snprintf(option, sizeof option, "world_xz=%llx:%llx:%llx:%llx:%llx",
             (unsigned long long)camera.rotation_address, (unsigned long long)camera.guard1_address,
             (unsigned long long)camera.guard1_value, (unsigned long long)camera.guard2_address, (unsigned long long)camera.guard2_value);
    test_frame = 1200; snprintf(command, sizeof command, "replay %s %s %s health=%llx:0", timeline, ack, option, (unsigned long long)(uintptr_t)&health);
    char *json = test_command(command, ack);
    snprintf(expected, sizeof expected, "\"rotation_address\":\"0x%llx\",\"guard1_address\":\"0x%llx\"", (unsigned long long)camera.rotation_address, (unsigned long long)camera.guard1_address);
    CHECK(json && strstr(json, "\"ok\":true") && strstr(json, "\"left_stick_space\":\"world_xz\"") && strstr(json, expected) &&
          strstr(json, "\"last_camera_frame\":0"), "world start ACK reports exact binding and no transformed frame"); free(json);
    test_input(1201); snprintf(command, sizeof command, "replay_status %s", ack); json = test_command(command, ack);
    CHECK(json && strstr(json, "\"last_camera_frame\":1201"), "world status ACK reports actual native camera frame"); free(json);
    snprintf(command, sizeof command, "replay %s %s health=%llx:0 %s", timeline, ack, (unsigned long long)(uintptr_t)&health, option);
    json = test_command(command, ack); CHECK(json && strstr(json, "\"ok\":true"), "world and health options accept either order"); free(json);
    uint64_t epoch = g_replay.epoch;
    snprintf(command, sizeof command, "replay %s %s %s %s", timeline, ack, option, option);
    json = test_command(command, ack); CHECK(json && strstr(json, "\"error\":\"command_syntax\"") && g_replay.epoch == epoch, "duplicate world option rejected atomically"); free(json);
    snprintf(command, sizeof command, "replay %s %s health=4:0 health=8:0", timeline, ack);
    json = test_command(command, ack); CHECK(json && strstr(json, "\"error\":\"command_syntax\"") && g_replay.epoch == epoch, "duplicate health option rejected atomically"); free(json);
    snprintf(command, sizeof command, "replay %s %s world_xz=1:2:3:4:5", timeline, ack);
    json = test_command(command, ack); CHECK(json && strstr(json, "\"error\":\"camera_syntax\"") && g_replay.epoch == epoch, "invalid world binding rejected before replacement"); free(json);
    snprintf(command, sizeof command, "replay %s %s", timeline, ack);
    json = test_command(command, ack);
    CHECK(json && strstr(json, "\"left_stick_space\":\"camera\"") && strstr(json, "\"world_xz\":null") && strstr(json, "\"last_camera_frame\":0"),
          "default ACK explicitly reports camera-relative mode"); free(json);
    replay_test_stop(); unlink(timeline); unlink(ack);
}
static _Atomic int replay_stress_stop;
static void *replay_reader_thread(void *arg) {
    (void)arg;
    while (!atomic_load(&replay_stress_stop)) { keys_sync(); sched_yield(); }
    return NULL;
}
static void t_replay_threads(void) {
    replay_test_stop(); test_frame = 10000; atomic_store(&replay_stress_stop, 0);
    camera_fixture fixture; replay_camera camera = camera_test_init(&fixture);
    pthread_t reader; int started = !pthread_create(&reader, NULL, replay_reader_thread, NULL);
    CHECK(started, "concurrent native HID reader starts"); if (!started) return;
    for (int i = 0; i < 1000; i++) {
        size_t n; const char *error = NULL; replay_step *steps = test_parse("f0:A~f2 f2:X~f2", &n, &error);
        if (!steps || (error = replay_install(steps, n, 0, 0, i & 1 ? &camera : NULL))) { free(steps); CHECK(0, "concurrent replay replacement"); break; }
        // Make the first step due without racing the fixture's non-atomic clock.
        pthread_mutex_lock(&g_sync_mu); --g_replay.start; --g_replay.steps[0].at; pthread_mutex_unlock(&g_sync_mu);
        if (i & 1) replay_test_stop();
    }
    atomic_store(&replay_stress_stop, 1); CHECK(!pthread_join(reader, NULL), "concurrent native reader joins");
    replay_test_stop(); CHECK(!atomic_load(&g_sc_btn), "concurrent replace/cancel leaves neutral state");
}
static void t_replay_large(void) {
    const size_t count = 50000, capacity = count * 64; char *text = malloc(capacity); size_t used = 0;
    CHECK(text != NULL, "large timeline allocation"); if (!text) return;
    for (size_t i = 0; i < count; i++) used += (size_t)snprintf(text + used, capacity - used, "f%zu:L0.125/-0.75+A~f4\n", i * 4);
    struct timespec before, after; clock_gettime(CLOCK_MONOTONIC, &before);
    size_t n; const char *error = NULL; replay_step *steps = replay_parse(text, &n, &error); clock_gettime(CLOCK_MONOTONIC, &after);
    double elapsed = (after.tv_sec - before.tv_sec) + (after.tv_nsec - before.tv_nsec) / 1e9;
    CHECK(steps && n == count && !error, "50k gene timeline parses");
    printf("replay parse: %zu steps, %zu bytes, %.3f ms\n", n, used, elapsed * 1000);
    free(steps); free(text);
}
int main(void) {
    int baseline = hid_baseline_main();
    strcpy(replay_test_dir, "/tmp/nxnative-replay-test-XXXXXX"); CHECK(mkdtemp(replay_test_dir) != NULL, "temporary protocol directory");
    t_replay_parse(); t_replay_frames(); t_replay_world_parse(); t_replay_world_math(); t_replay_world_frames(); t_replay_world_failures(); t_replay_health(); t_replay_terminal(); t_replay_terminal_restore(); t_replay_protocol(); t_replay_world_protocol(); t_replay_threads(); t_replay_large();
    replay_test_stop(); free(g_replay.steps); g_replay.steps = NULL;
    CHECK(!rmdir(replay_test_dir), "remove protocol directory without leftovers");
    printf("hid + native replay: %d checks, %d failed\n", checks, fails); return baseline || fails != 0;
}
