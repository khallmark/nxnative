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
be scored and reversed on a declared reciprocal path. With checkpoint recovery configured, damage or leaving the training bounds records
a failed attempt and uses a guarded normal-game Load sequence. Invalid telemetry,
an unrecognized menu, a changed checkpoint or failed recovery stops the run.
Reciprocal navigation still stops on damage or an unsafe terrain transition.

## Running

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
- `min_health`, `timeout` in seconds and optional arrival `tolerance` (default 0.25).

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
expected result. It never retries an uncertain button press. The sequence starts
and finishes in observed `gameplay`. Save hashes are checked before and after;
the final position, health and bounds must match the curriculum before another
trial can begin. A checkpoint task uses this reset after both success and failure.
Unknown screens and death are not automatically recoverable unless the configured
flow explicitly handles their observed state; the current gameplay-starting flow
stops on them.

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
observations read small current headers, names and handles; discovery pages are
not retained on disk. Loading and pause checks use the stable UI/player reads,
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
