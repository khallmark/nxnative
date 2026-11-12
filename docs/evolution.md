# Evolving a live game controller

`scripts/evolve_game.py` runs a persistent genetic search using nxnative's `DM_CTL`
interface. It reads validated guest memory and emits ordinary, bounded controller
presses. It does not patch game memory, alter saves or claim game completion.

The current executable curriculum is reciprocal navigation: return to a declared
start after each route, evaluate every candidate twice, select by its worst trial,
retain two parents and mutate/cross over the others. Every generation re-evaluates
its parents. SQLite commits each finished trial and stores the population and RNG
state, so an interrupted run resumes the missing trial rather than starting over.
An implementation or curriculum change requires a new database; fitness measured
under different conditions must not silently compete. Failed route attempts can
be scored and reversed on a declared reciprocal path. Damage, invalid telemetry,
unsafe height, failed reset or leaving the training bounds stops the live run.

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

- `id` and `kind: "reciprocal_navigation"`;
- `start`: three world coordinates, with optional `start_tolerance` (default 0.3);
- `targets`: ordered three-coordinate waypoints on a verified reciprocal route;
- `bounds`: three `[minimum, maximum]` ranges containing that safe route;
- `min_health`, `timeout` in seconds and optional arrival `tolerance` (default 0.25).

All tasks in this version share a start anchor. Verify the route and its reverse
before unattended evaluation. A bounding box does not prove terrain is walkable.
Observe the normal inventory pause menu before using `--start-paused`. The adapter
owns subsequent pause toggles during that run. Do not use other input tools or
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
most 0.4 seconds; loss of telemetry stops further actions. Pause is a normal button
toggle, not an independently verified game pause API. Death, dialogue and engine
failure can prevent it; inspect the game before resuming after an abnormal stop.

`trajectory.jsonl` records observations and actions; `interruptions.jsonl` records
aborted trials; `evolution.sqlite` is authoritative for populations and evaluations.
`champion.json` is the latest generation report, not evidence of game victory.
The steering genes control hold duration, gain, braking, hazard prediction horizon,
repulsion strength and passing side. Hazard prediction assumes locally constant
velocity; it has not yet been validated on live boulder crossings.

## Evidence and remaining work

The first live persistent trial in Ocarina of Time exposed momentum after input
release. Settled arrival and distance-dependent braking fix the reset criterion.
Automated tests cover deterministic resume, incomplete-generation rejection,
worst-task selection, implementation/curriculum incompatibility, synthetic search
improvement, crossing-hazard response, height separation and final-action damage.
Synthetic tests establish mechanics, not general gameplay ability.

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
