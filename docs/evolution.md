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
must be checked for the current game. This is an action-sequence GA, not a neural
network or an automatically learned map.

```sh
python3 scripts/evolve_replay.py --session /game/run/native-session.json \
  --objective /game/run/objective.json --output /game/run/native-training \
  --population 8 --repeats 2 --generations 0
```

The objective JSON declares `start`, `target` (3D points), `start_health`, and
`horizon_frames` (1–1,000,000). Optional fields are `segments` (128), `tolerance`
(0.5), `start_tolerance` (0.3), `poll_seconds` (0.05), `stall_seconds` (30), and
`initial_direction` (two stick coordinates, default `[0, 1]`). Use a fresh output
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
for resume. It never writes game saves. Before death and during ordinary Load,
all files remain strict. Do not include progress/caption files merely to bypass
a failure; such a change needs a newly verified checkpoint and experiment.

The runner batches fixed telemetry and validates identities throughout attempts.
It does not perform actor discovery or reactive steering during native timelines.
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
observation skew. Measure actual throughput and behavior before claiming speedup
from `DM_DETCLOCK` or headless mode; a fast timeline compiler is not fast gameplay.

### Native replay protocol

Start the host with a fresh `DM_CTL` path. `DM_CTL_POLL_US=1000` reduces its command
poll interval from the default 100,000 microseconds; accepted values are integral
1,000–1,000,000. The native input loop does not wait for Python observations.

```text
replay /absolute/timeline /absolute/ack [health=hex-address:decimal-u32-threshold]
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
