// Standalone replay tests reuse the HID test's host stubs and run its baseline suite.
// clang -O1 -Wall -Werror -Wno-unused-function -I.. -I../gen/<GAME> -I../../games/<GAME> -o build/t_replay t_replay.c ../hle_haptics.c ../hle_bigstack.c
#define main hid_baseline_main
#include "t_hid.c"
#undef main

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
    error = replay_install(steps, n, address, threshold);
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
    CHECK(!strcmp(replay_install(steps, n, 0, 0), "frame_overflow"), "absolute rebase overflow rejected"); free(steps);
    test_frame = 500; atomic_store(&g_legacy_keys_on, 1);
    steps = test_parse("f0:A~f1", &n, &error); epoch = g_replay.epoch;
    CHECK(!strcmp(replay_install(steps, n, 0, 0), "legacy_seconds_active") && g_replay.epoch == epoch, "legacy seconds playback excludes replacement");
    free(steps); atomic_store(&g_legacy_keys_on, 0);
    atomic_store(&g_sync_on, 1); g_sync_held = 1; test_install("f0:X~f1", 0, 0);
    CHECK(!atomic_load(&g_sync_on) && !g_sync_held, "legacy frame replay is atomically replaced"); replay_test_stop();
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
static _Atomic int replay_stress_stop;
static void *replay_reader_thread(void *arg) {
    (void)arg;
    while (!atomic_load(&replay_stress_stop)) { keys_sync(); sched_yield(); }
    return NULL;
}
static void t_replay_threads(void) {
    replay_test_stop(); test_frame = 10000; atomic_store(&replay_stress_stop, 0);
    pthread_t reader; int started = !pthread_create(&reader, NULL, replay_reader_thread, NULL);
    CHECK(started, "concurrent native HID reader starts"); if (!started) return;
    for (int i = 0; i < 1000; i++) {
        size_t n; const char *error = NULL; replay_step *steps = test_parse("f0:A~f2 f2:X~f2", &n, &error);
        if (!steps || (error = replay_install(steps, n, 0, 0))) { free(steps); CHECK(0, "concurrent replay replacement"); break; }
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
    t_replay_parse(); t_replay_frames(); t_replay_health(); t_replay_protocol(); t_replay_threads(); t_replay_large();
    replay_test_stop(); free(g_replay.steps); g_replay.steps = NULL;
    CHECK(!rmdir(replay_test_dir), "remove protocol directory without leftovers");
    printf("hid + native replay: %d checks, %d failed\n", checks, fails); return baseline || fails != 0;
}
