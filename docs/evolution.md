# Evolving a live game controller

`scripts/evolve_game.py` runs a persistent genetic search using nxnative's `DM_CTL`
interface. It reads validated guest memory and emits ordinary, bounded controller
presses. It does not patch game memory, alter saves or claim game completion.

The executable curriculum supports reciprocal navigation and checkpoint navigation.
Return to a declared start after each route, evaluate every candidate twice, select
full-success policies before failures and then by their worst trial,
retain two parents and mutate/cross over the others. Every generation re-evaluates
its parents. SQLite commits each finished trial and stores the population and RNG
state, so an interrupted run resumes the missing trial rather than starting over.
An implementation, session configuration or curriculum change requires a new database; fitness measured
under different conditions must not silently compete. Failed route attempts can
be scored and reversed on a declared reciprocal path. With checkpoint recovery configured, exceeding the declared damage allowance or leaving the training bounds records
a failed attempt and uses a guarded normal-game Load sequence. Invalid telemetry,
an unrecognized menu, a changed checkpoint or failed recovery stops the run.
Reciprocal navigation still stops on damage or an unsafe terrain transition.

## Native whole-attempt evolution

`scripts/evolve_replay.py` evaluates many-gene input timelines while the host
plays them at guest input reads. The default chromosome has 128 segments and
768 real-valued genes: left-stick X/Y, dash/action A, jump X, attack Y, and hold
frames per segment. Button genes above zero press that button. Hold genes round
to 1–30 frames; the chromosome repeats to the declared frame horizon. Controls
must be checked for the current game. The default policy is an action-sequence GA; the optional target-feedback policy
below evolves positional goals and braking parameters. Neither is a neural network
or an automatically learned map.

```sh
python3 scripts/evolve_replay.py --session /game/run/native-session.json \
  --objective /game/run/objective.json --output /game/run/native-training \
  --population 8 --repeats 2 --generations 0 --resident-handoff
```

The objective JSON declares `start`, `target` (3D points), `start_health`, and
`horizon_frames` (1–1,000,000). Optional fields are `segments` (128), `tolerance`
(0.5), `start_tolerance` (0.3), `poll_seconds` (0.05), `stall_seconds` (30), and
`initial_direction` (two input coordinates, default `[0, 1]`). With the optional
world-X/Z mode below, these mean world X and Z; otherwise they are camera-relative
stick X/Y. Use a fresh output
folder after changing code, session, objective or mutation settings. An earlier
compatible chromosome can seed the search with `--seed-champion`; its fitness is
never imported. Sparse crossover/mutation defaults to a 0.1 mutation probability
per gene and sigma 0.12 of its range. Selection follows a complete generation
with at least four candidates and two interleaved evaluations each; each death
is persisted immediately, while offspring are selected at generation boundaries.

Damage, falls, brief lack of movement and reaching the target do not end native
attempts. They continue until a native zero-health guard or the fixed horizon.
A horizon is a **censored survival observation**, never a death. Best observed
3D distance gives progress bins 0–99; three successive increasing-frame samples
inside tolerance with adjacent movement at most 0.05 give objective bin 100.
Fitness is `progress_bin * (horizon_frames + 1) + alive_frames`: progress outranks
passive survival, then longer survival wins within a bin. Positional stability
is not an engine grounded-state flag. Objectives, transitions and completion
flags still need game-specific validation.

The session additionally needs `recovery.death_steps`, starting in positively
observed `game_over` and finishing in `gameplay`. The ordinary checkpoint Load
sequence then restores the exact declared start. A visible HUD alone cannot
establish gameplay at zero health; some games retain it at Game Over. Validate
the menu identity and visibility after its death animation, and verify all save
hashes before and after recovery. The native adapter executes UI hold/settle
intervals as guest frames using `replay_fps` (default 60). This conversion and the
recovery sequence must be calibrated at the chosen clock/pacing settings.

If a verified death/Continue flow rewrites metadata such as an option file,
`recovery.mutable_after_death` can map only those observed paths to maximum byte
sizes. Every path must already have a starting hash in `files`. After the guarded
death flow returns to the exact anchor with sufficient health, all other hashes
must still match. The adapter archives each new metadata value, logs its old/new
hashes, and atomically persists the accepted hashes in `checkpoint-state.json`
for resume. Asynchronous metadata writes must remain unchanged for
`recovery.death_metadata_quiet_seconds` (default 0.5) within
`recovery.death_metadata_timeout_seconds` (default 5). The runner reconciles again
after the final normal Load, only within that same verified death recovery.
This bounded quiet interval is not a game-provided save-completion signal.
It never writes game saves. Before death and during a separate ordinary Load,
all files remain strict. Do not include progress/caption files merely to bypass
a failure; such a change needs a newly verified checkpoint and experiment.

The runner batches fixed telemetry and validates identities throughout attempts.
It does not perform actor discovery or Python steering during native timelines.
The optional target-feedback policy computes steering in the native input loop.
A changed identity, unexpected menu, changed save, invalid native epoch, read
error, or stalled guest frame counter interrupts evaluation without assigning
fitness. The host releases scripted input on death, cancellation and completion;
physical input is preserved and must not be used concurrently. SIGINT/SIGTERM
cancels replay, retains completed attempts and requests a normal pause when
possible. A killed Python process leaves at most its finite native timeline;
there is no controller heartbeat watchdog.

`episode_finished` records compile time, native survival frames, episode wall
time, frames per wall second, reset time and attempts per minute. The native
`stop_frame` freezes terminal timing; later observation latency does not extend
life. Memory reads are asynchronous, with start/end frame brackets reported as
observation skew. A second status check reconciles the same epoch and replay
bounds after each read. Position affects fitness only when its bracket ends
strictly before a confirmed later active frame or terminal frame. Same-frame
samples remain pending, so health recovery cannot turn post-death movement into
objective progress. Measure actual throughput and behavior before claiming speedup
from `DM_DETCLOCK` or headless mode; a fast timeline compiler is not fast gameplay.

### Resident UI handoff

Item messages and other living non-gameplay UI interrupt an attempt without a
fitness result, including an observation that also reaches the frame horizon.
Native death evidence takes precedence over a recovered health mirror. After
successful lethal-hook disarm and acknowledged input cancellation, the runner
archives the exact key, genome, compiled timeline, objective and observation in
`handoffs/<id>/event.json`. `handoff.json` marks the unresolved transition.

With `--resident-handoff`, the same Python process retains its Frida session,
resident scripts, bridge lock and open logs. It waits for explicit commands and
performs no reset, population selection or automatic dialogue advancement. Without
the flag it archives the interruption and then follows ordinary shutdown. This
retains evidence, but does not avoid a possible instrumentation unload delay.

A supervising controller reads `handoff-ack.json` and atomically replaces
`handoff-command.json` with one command. Every command needs its current
`handoff_id`, the exact `next_sequence`, and an `action`:

| Action | Additional fields | Behavior |
| --- | --- | --- |
| `input` | `expected_epoch`, nonempty `expected_reads`, `keys`, `hold_seconds`; optional `expected_ui` | Revalidates identities, living health and idle ownership, then sends one guarded ordinary button or neutral hold of 0.05–0.5 seconds through the same bridge. |
| `shutdown` | None | Requests actual cleanup and process exit; it does not acknowledge successful unload or resolve the milestone. |

`expected_reads` maps existing telemetry names to their exact integer arrays;
commands cannot introduce memory addresses. An optional named UI predicate must
already exist in the session. The epoch must match the latest acknowledged
status. Keys cannot contain analog controls, combinations or macros. Accepted
sequence numbers are durably consumed before validation or input: a failed guard
also consumes its number, and retrying an old command cannot press twice. The
acknowledgement records rejection or the resulting state and next sequence.
Runtime, identity, cancellation or persistence failures still stop the owner.

There is no automatic resume or checkpoint rebinding. Acquisition can change
inventory, saves and the objective even after the HUD returns. Preserve and
validate the new milestone, then use a fresh experiment. An existing
`handoff.json` and a marker beside the owned control file block startup before
attachment or checkpoint restoration, including after an explicit shutdown.
The control marker is checked again under the bridge lock; changing only the
output directory cannot bypass it. Do not delete that evidence merely to restart
the old experiment. A fresh host/control session and validated checkpoint belong
to a new experiment. This mechanism prevents UI from initiating teardown;
it cannot make an already timed-out remote unload safe to reuse. See the
[UI lifecycle finding](reference/services.md#item-dialogue-interrupts-a-native-training-episode).

### In-place player checkpoints

The native runner accepts an optional `fast_reset` session object. Its `script`
is an absolute path to a UTF-8 Frida script, pinned by a full `sha256`; `pid`,
`build_id`, `mainbase`, and `address_binding` bind it to the verified live game.
`timeout_seconds` bounds requests to 0.1–60 seconds. The optional Python `frida`
package must be available in the runner's interpreter. Script bytes, Frida version,
reset implementation and configuration join the experiment fingerprint.

The [Ocarina profile](../dm/games/0400486024DEC000/README.md#in-place-training-checkpoints)
provides a Darwin arm64 bridge with its own build and code offsets. Use mode
`player_checkpoint`, a captured nine-float `target_rotation`, and the profile's
session bindings for `player`, `hud_flags`, `plus_flags` and `code_fingerprints`.
The objective supplies the start position and health. These bindings must be
rediscovered after a restart; the build ID is an explicit operator binding,
not automatic game identification.

This reset keeps the current world loaded. It validates a native callback on the
guest stack, restores the player's physics/controller pose and health, preserves
the thread's virtual registers, and requires consecutive-frame readback
before reporting `restored`. The Ocarina bridge also bounds frame-to-frame position
and rotation-element change to 0.005, retaining the absolute anchor checks; being
inside the anchor tolerance alone does not prove the player stopped moving. It does not restore camera state, actors, random
state, animation, inventory or quest progress. It is a **player checkpoint**;
candidates encounter a continuously evolving world. Do not compare its fitness
with experiments that reload the entire scene.

Only an inactive native replay and positively identified living gameplay may use
this reset path. Save hashes remain strict. A failed or uncertain reset stops the
runner; there is no automatic menu fallback. Cleanup waits for native callbacks
to finish before removing instrumentation. An uncertain reset also suppresses the
runner's normal Plus-button cleanup because the screen may be transitioning.
Ordinary Game Over recovery remains necessary unless a separately verified lethal
damage hook has prevented entry into that flow.

On reset failure, the runner retains the original exception and captures the
profile's bounded `snapshot()` evidence before cancellation. Queued phase messages
and cleanup failures stay in the trajectory diagnostics. Runner shutdown records
cleanup operations and tracebacks separately in `cleanup-errors.jsonl`; those
errors do not replace the primary failure. The snapshot is diagnostic evidence,
not completion proof, and does not advance the reset or call guest functions.

The optional top-level `native_guard_health_address` selects an aligned u32
address for the native terminal guard independently of the `health` telemetry
mirror. It defaults to `reads.health.address`. Use the authoritative health store
when a native damage callback is bound to that store; the existing two-u32 mirror
still supplies current/max telemetry. Both addresses need runtime identity proof.

The bridge and validated mapping spans remain resident across successful resets;
each retry still rereads code, actor ownership, health and UI. Native checks verify
current writable health pages before mutation. Hooks detach after each restore,
and explicit shutdown unloads the idle script. The profile records measured
startup and warm-reset latency; automatic death retry must be measured separately.
A successful native call alone does not certify a completed end-to-end retry.
With Ocarina's opt-in action reset, the engine Wait request and warp occur on
separate actor updates, with ownership/context checks at each stage. Cancellation
can release the pending interval without a warp; pre-warp and post-warp guest
frame deadlines are independent. Neither action nor warp is repeated to hide a
failed stability check.

A descriptor-restoration ACK proves only the bridge's cleanup contract. Frida
script unload has its own instrumentation teardown barrier and may still fail.
A cancelled unload is not permission to assume teardown finished or to reattach;
retain the original failure and independent cleanup diagnostics.

### World-direction movement genes

A player checkpoint leaves the camera moving. Camera-relative chromosomes can
therefore take different routes from the same start. The optional session object
`native_world_xz` makes timeline `Lx/y` mean a world X/Z direction and magnitude.
The native input loop converts it using the current camera once per guest frame,
including inside an unchanged held segment; Python still submits an entire attempt.
Buttons, right stick and UI recovery inputs keep their original meanings.

```json
{
  "native_world_xz": {
    "rotation_address": "0x1000",
    "guard1_address": "0x2000",
    "guard1_value": "0x3000",
    "guard2_address": "0x4000",
    "guard2_value": "0x5000"
  }
}
```

These are illustrative addresses, not a runnable game binding. All five values
are hexadecimal uint64 strings. The rotation address points to nine float32
values in a row-major camera-to-world matrix. Two aligned uint64 equality guards
bind its identity; select them from verified game evidence. The host rereads both
guards around the matrix read and rejects unreadable, changed or degenerate data.
Horizontal right is `(r[0], r[6])`; forward is `(-r[2], -r[8])`. After normalizing
both projections, the host solves their two-dimensional basis and radially clamps
the resulting stick to unit length. This convention must be calibrated per game.
The read is not an atomic scene snapshot. A known camera actor is not proof that
it remains the active camera through cutscenes or camera-mode transitions.

The adapter requires a matching native mode/binding ACK. Unsupported hosts reject
the extra option; default camera-relative sessions continue to work. Runtime
camera failures release scripted input and interrupt evaluation without fitness.
Changing coordinate modes requires a fresh experiment. Derive a world-mode seed
from world-space route evidence; reinterpreting old camera-relative champion genes
changes their actions. Camera normalization does not rewind hazards or random state.

### Native target-feedback genes

Set the objective's `policy` to `target_feedback_v1` to evolve goals that react
to player position. The default `timeline_v1` keeps the original stick genes.
Feedback additionally requires `native_position_xyz` in the session, alongside
`native_world_xz`:

```json
{
  "native_position_xyz": {
    "position_address": "0x6000",
    "guard1_address": "0x7000",
    "guard1_value": "0x8000",
    "guard2_address": "0x9000",
    "guard2_value": "0xa000"
  }
}
```

These illustrative hexadecimal addresses are not a runnable binding. Position
points to three float32 values. Two aligned uint64 equality guards identify the
player; the host checks them before and after each safe position read. The host
contains no game offsets. Unreadable, changed-identity or nonfinite/out-of-range
position data releases scripted input and interrupts scoring.

Each feedback segment has eight genes, giving **1,024 genes at 128 segments**:

| Gene | Bounds | Walking seed |
| --- | --- | --- |
| `target_dx`, `target_dz` | -8 to 8 world units relative to the objective | 0 |
| `strength` | 0 to 1 controller magnitude | 1 |
| `slow_radius` | 0.25 to 2 world units | 0.75 |
| `dash`, `jump`, `attack` | -1 to 1; positive presses A, X, Y | -1 |
| `frames` | 1 to 30 presented frames per segment | 30 |

The objective's `feedback_stop_radius` defaults to 0.1, must be within 0–0.2,
and must be below its scoring tolerance. Python compiles each segment into
`Gx/y/z/strength/stop/slow` plus any button genes. Target X/Z include the segment's
offsets; target Y stays the objective's Y. Fixed decimal formatting avoids a
scientific-notation `+` being interpreted as a button separator.

On each new guest input frame, native code computes X/Z distance `d` and
`strength * clamp((d-stop)/(slow-stop), 0, 1)`, points toward the target, and uses
the current world-camera projection. Feedback preserves that computed controller
magnitude even with a skewed camera basis. An unchanged held segment updates only
axes, without repeating button/touch edges. Inside the stop radius it is neutral.
Health/death and horizon checks retain precedence.

This is horizontal steering, not pathfinding, a ground-contact test, or a native
success flag. The evaluator still requires stable **3D** arrival. Vertical mismatch,
obstacles, deadzones and low-strength/large-slow-radius mutants can prevent arrival
and receive ordinary failure fitness. For illustration, a hypothetical 0.2 stick
deadzone with the default strength/slow/stop values stalls near radius 0.23; that
is a synthetic model, not a measured Ocarina deadzone. Qualify movement and quiet
terrain height in the actual game before adopting a new objective.

Frame-counted button and segment-transition timing remains sensitive to game time
per presented frame. Target steering corrects endpoint motion while a goal is
held; it does not normalize jump timing or rewind the world. A new policy uses a
fresh database and fresh fitness evaluations; a 768-gene stick chromosome cannot
be silently reinterpreted as a 1,024-gene goal chromosome. `--initial-sigma` controls
initial population spread independently of later `--mutation-sigma`; its default
remains 0.35, while a narrowly seeded feedback qualification can use 0.04.

### Native replay protocol

Start the host with a fresh `DM_CTL` path. `DM_CTL_POLL_US=1000` reduces its command
poll interval from the default 100,000 microseconds; accepted values are integral
1,000–1,000,000. The native input loop does not wait for Python observations.

```text
replay /absolute/timeline /absolute/ack [health=hex-address:decimal-u32-threshold] [world_xz=rotation-address:guard1-address:guard1-value:guard2-address:guard2-value] [position_xyz=position-address:guard1-address:guard1-value:guard2-address:guard2-value]
replay_status /absolute/ack
replay_cancel /absolute/ack
replay_observe /absolute/read-request /absolute/ack
```

Every request uses a unique ACK path, without whitespace. The host publishes
protocol-1 JSON atomically. An accepted replay increments `epoch`, starts at the
current guest frame plus one and replaces only frame-based input. Legacy scripts
containing seconds block replacement. Manual DM_CTL button/drag commands are
rejected during native replay. Invalid replacements leave the active replay
intact; failure to publish the start ACK cancels the accepted replacement.

Timeline lines use `f0:L0/1+A~f12`, then e.g. `f12:L0/1+A+X~f3`.
Starts and positive holds are integral, ordered and nonoverlapping. Adjacent
intervals preserve held buttons; gaps release them. Empty keys mean neutral.
Seconds, repeats, unknown keys, nonfinite sticks and overflowing frames are
rejected. The limits are 1,048,576 steps and a 128 MiB regular file. Guards read
an aligned current-health u32 once per new input frame, release at or below the
threshold, and report unreadable memory separately. A readable address alone
cannot prove actor identity; the Python profile provides that validation.

ACKs report `ok`, `error`, `epoch`, `current_frame`, `start_frame`, exclusive
`end_frame`, `stop_frame`, step indices, `active`, `reason`, and guard evidence.
World-direction support adds `left_stick_space` (`camera` or `world_xz`),
`world_xz` (null or the five canonical hexadecimal binding fields above), and
`last_camera_frame` (zero before the first transform or in camera-relative mode).
Position feedback adds `movement_mode` (`timeline` or `target_feedback`),
`position_xyz` (null or canonical five-field binding), `last_position_frame`, and
`last_position` (null before its first read, otherwise XYZ). The adapter requires
an exact feedback mode/binding ACK and cancels uncertain owned input before runner
cleanup. Old hosts remain compatible with ordinary timelines; they cannot
silently accept feedback. UI recovery omits both world and position bindings.

All five fields of each binding option are hexadecimal; optional arguments can
appear in any order. A `G` token requires both bindings, conflicts with every
left-stick token in that segment, and may coexist with buttons/right-stick input.
A position binding without any `G` segment is rejected. Native `G` fields require
finite target coordinates with absolute value below 1,000,000, strength in 0–1,
and `0 <= stop < slow <= 1000`. Invalid replacements preserve the old replay.
The observer accepts lines `hex-address decimal-byte-count`, at most 128 ranges,
4 KiB each and 64 KiB total. Each result has address, size, hexadecimal bytes or
an explicit error; failed reads never become zero-filled observations. The
Python adapter chunks and reassembles larger reads. `current_frame` and
`observed_frame_end` bracket the reads; this is not a frozen scene snapshot.

## Running the waypoint controller

Keep the session configuration, curriculum, dumps, SQLite databases and game saves
outside this repository. Start the game with a fresh `DM_CTL` file. Discover and
validate the addresses for that running process and game build. Do not reuse an
address configuration after restarting the game. The adapter checks the PID,
actor identities, finite position/camera values and health range before acting.
A PID plus memory identities is not automatic actor discovery or a build detector.

The session JSON contains `pid`, absolute `control` and `scratch` paths, `reads`
and `identity`. Each named read has an `address` (a hexadecimal string) and a
Python `struct` format. Required decoded reads are `position` (`3f`),
`camera_rotation` (`9f`, row-major), and `health` (`2I`, current and maximum).
Optional `hazard` is `3f`. Identity reads such as actor vtable and self pointer use
`Q` and must appear in `identity` with arrays of expected integers or hex strings.
Scratch paths cannot contain whitespace because of the host command parser.

Each curriculum entry contains:

- `id` and `kind`: `reciprocal_navigation` or `checkpoint_navigation`;
- `start`: three world coordinates, with optional `start_tolerance` (default 0.3);
- `targets`: ordered three-coordinate waypoints on a verified reciprocal route;
- `bounds`: three `[minimum, maximum]` ranges containing that safe route;
- `min_health`, `timeout` in seconds and optional arrival `tolerance` (default 0.25);
- checkpoint-only `max_damage` (default zero): cumulative observed health loss
  allowed per route, independent of the minimum-health floor. Healing does not
  erase earlier damage. Each lost health unit subtracts 25 from fitness;
- optional `start_health` (default `min_health`): required health at episode start
  and after reload. For example, start at 12, allow 2 damage, and stop below 10.

All tasks in this version share a start anchor. Verify a reciprocal route and its
reverse before unattended evaluation. Checkpoint tasks require a tested reload
sequence and a save that restores the declared start and minimum health. A bounding box does not prove terrain is walkable.
Observe the normal inventory pause menu before using `--start-paused`. With UI probes configured, the adapter verifies the pause and gameplay states
before proceeding. Without them, it owns subsequent pause toggles during that run. Do not use other input tools or
manual controls concurrently. The trainer lock prevents another trainer instance,
but cannot prevent an unrelated process from appending controller commands.

```sh
python3 scripts/evolve_game.py --session /game/run/session.json \
  --curriculum /game/run/curriculum.json --output /game/run/training --inspect
python3 scripts/evolve_game.py --session /game/run/session.json \
  --curriculum /game/run/curriculum.json --output /game/run/training \
  --population 8 --repeats 2 --generations 1 --start-paused
```

Use `--seed-champion /old/run/champion.json` with a new output directory to carry
learned genes across a controller change. Prior fitness is never imported; the
seed and its offspring must all be evaluated again.

Use `--generations 0` to continue evolving until interrupted. SIGINT/SIGTERM retains
completed evaluations and requests pause. The host releases each input after at
most 0.4 seconds for movement and 0.5 seconds for menu steps; loss of telemetry stops further actions. Pause uses normal controller input. Configured memory predicates verify the
result; without those predicates it remains an unverified toggle. Death, dialogue
and engine failure can prevent it; inspect the game after an abnormal stop.

`trajectory.jsonl` records observations and actions; `interruptions.jsonl` records
aborted trials; `evolution.sqlite` is authoritative for populations and evaluations.
`champion.json` is the latest generation report, not evidence of game victory.
The steering genes control hold duration, gain, braking, hazard prediction horizon,
repulsion strength and passing side. Hazard prediction assumes locally constant
velocity; it has not yet been validated on live boulder crossings.

## Checkpoint recovery

The optional session `ui` object maps screen names to nonempty lists of integer
predicates. Every predicate must match: `read` names a configured telemetry read,
`index` selects an element (default zero), optional `mask` is bitwise AND, and
`equals` is the expected integer or hex string. At minimum, define `pause` and
`gameplay`; the latter must use positive evidence of active gameplay, not merely
absence of a menu. Validate screen identities as well as visibility flags. A
freed screen object can retain stale flags. All addresses and UI predicates are
local, process-specific observations; this is not automatic screen discovery.

The session `recovery` object contains:

- `files`: absolute save-file paths mapped to SHA256 digests. Cover the selected
  save and metadata that determines its position in the Load list. The files are
  read only; any hash change aborts recovery and requires a fresh checkpoint and
  experiment. Preserve an independently verified backup of original saves.
- `steps`: ordered objects with `keys`, `before`, and `after`. Screen guards name
  entries in `ui`. Allowed keys are `Plus`, `R`, `L`, `LUp`, `LDown`, `A`, `B`, or
  an empty neutral input. Optional `hold` is 0.05–0.5 seconds (default 0.15),
  `settle` is 0–2 seconds (default 0.3), and `timeout` is 0.1–60 seconds (default 3).

Each step observes its required screen, sends one bounded input, then observes the
expected result. The gameplay-to-pause Plus transition is special: damage stagger
can ignore a press even with an active HUD. It observes UI state again and may
retry after 0.5 seconds only while gameplay remains positively identified, within
the step timeout. An observed pause receives no further press; unknown screens
receive no retry. Other menu buttons are never retried on an uncertain result.
The sequence starts
and finishes in observed `gameplay`. Save hashes are checked before and after;
the final position, health and bounds must match the curriculum before another
trial can begin. A checkpoint task uses this reset after both success and failure.
Unknown screens stop the runner. Configured `death_steps` handle only a positively
observed Game Over screen before the ordinary gameplay-starting Load flow.

First test recovery separately from evolution:

```sh
python3 scripts/evolve_game.py --session /game/run/session.json \
  --curriculum /game/run/curriculum.json --output /game/run/recovery-check \
  --start-paused --recover-only
```

This verifies the configured flow and leaves the game paused. Also verify recovery
after physically moving away from the save position and after a failed episode.
`recovery.jsonl` records the standalone check; training recovery events are written
to `trajectory.jsonl`. A reload that cannot restore the anchor stops evolution;
a measured trial result is retained even if its subsequent reset fails.

A checkpoint task can set `settle_targets` to one boolean per target. `false`
marks an intermediate point to pass through without a neutral settling delay;
the final target must be `true`. Health and 3D bounds are still checked before
crediting every arrival. Keep the final destination clear of hazards: stopping
on a boulder lane makes the evaluation depend on surviving an artificial wait.
Reciprocal tasks retain settled arrival at every point.

Checkpoint tasks can also declare `target_buttons`, one string per target: empty
for ordinary walking, `A` for a dash/action pulse, `X` for jump, or `A+X` for both.
Each string combines with the evolved camera-relative stick input for that leg.
Inputs retain the evolved 0.1–0.4 second hold; the host releases between samples,
so repeated A pulses do not establish continuous dash behavior. These controls
must be verified in the current game and scene. No other button or command text
is accepted. Reciprocal tasks cannot use action targets.

Every jump target must settle, including intermediate ones. Arrival requires
three neutral 0.25-second observations inside its 3D tolerance, with no adjacent
position change exceeding 0.05 units. A transient airborne crossing is not enough;
while the candidate landing remains in tolerance but is moving, the runner keeps
input released and obeys the task deadline. Health, damage and terrain guards
still apply to every sample. Jump settling observations are logged. This is a
measured positional stability check, not an engine grounded-state flag, and does
not discover launch points or certify unseen landing geometry. Declare the full
jump arc in the bounds and validate the destination height before training.

## Actor discovery across scene loads

An optional `actor_discovery` session profile resolves named scene actors through
an observed handle pool. This fixes an observed loading failure: the game freed a
boulder while keeping player addresses stable, leaving NaN position data at the
old address. The controller now clears scene bindings in menus/loading, discovers
the live actor after gameplay returns, and validates its handle serial, pointer,
vtable, self pointer, name and finite position on every observation. Reused or
freed handles trigger read-only rediscovery before the next policy decision.
Missing required actors and duplicate matching names stop input.

The profile contains:

- `ranges`: allowed `[start, end)` memory ranges, with integers or hex strings.
- `pool`: `address`, `count`, `stride`, `flags` (offset list), `mask`, `equals`,
  `pointer` offset and `serial` offset. Handle fields are little-endian 64-bit
  values. Every configured flag comparison must match; invalid pointers and
  empty serial markers are excluded.
- `object`: header `size`, expected `vtable` at offset zero, and byte offsets
  `self`, `name` and `position`. Position is three little-endian floats; name is
  an ASCII C string of at most 127 bytes.
- `targets`: observation names mapped to `{ "name": "ActorName", "required": true }`.
  The `hazard` target supplies the steering policy's obstacle position. Optional
  absent actors produce no observation; only use optional hazards in a curriculum
  that does not require their presence.

Discovery requires configured UI probes and cannot coexist with a fixed `hazard`
read. It is bounded to the declared ranges and 64 MiB of scan pages. Cached actor
observations read small current headers, names and handles in the same DM_CTL
request as player/UI state. This avoids a second 100 ms control-file poll per
action while preserving current identity checks; invalid cached identities still
trigger rediscovery. Discovery pages are not retained on disk. Loading and pause checks use the stable UI/player reads,
so a freed scene actor cannot prevent an observed pause. After loading, required
actors get a bounded readiness check before another trial can begin.

This is scene-load recovery within a validated process/build. It does not locate
the handle pool, player, camera, health or UI roots after a process restart. Those
profile addresses must still be rediscovered and validated for a new session.
A save notification also does not prove the desired start: saving before Link
fully exits a crawl can restore the last standing position on the other side.
Always verify the actual loaded position before training.

## Evidence and remaining work

The first live persistent trial in Ocarina of Time exposed momentum after input
release. Settled arrival and distance-dependent braking fix the reset criterion.
Automated tests cover deterministic resume, incomplete-generation rejection,
worst-task selection, implementation/curriculum incompatibility, synthetic search
improvement, crossing-hazard response, height separation and final-action damage.
Synthetic tests establish mechanics, not general gameplay ability. Recovery tests
cover stale UI flags, changed saves, wrong menus, wrong loaded positions, failed
episode scoring and preservation of an evaluation when recovery fails.

The user objective remains an evolving agent that beats Ocarina of Time. This
implementation is its navigation-training foundation. Completion requires all of:

1. Restart-safe actor and progress discovery, including scene/menu and inventory.
2. Repeatable saved checkpoints and automatic normal-game recovery, with originals
   preserved and restore validation before training resumes.
3. A curriculum graph that advances through acquired items, traversal, puzzles,
   dungeons and bosses. Navigation coordinates alone cannot certify a milestone.
4. Evolved action policies for climbing, jumping, interaction, targeting, defense
   and combat, evaluated on varied starts/timings and independent repeat trials.
5. Reliable hazard/terrain handling and recovery, verified live without substituting
   synthetic fitness for game progress.
6. An observed final-boss victory and ending sequence, with corresponding saved
   progress and a reproducible event/telemetry record.

None of the remaining requirements is certified by a short navigation benchmark.
