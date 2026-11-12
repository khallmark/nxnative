#!/usr/bin/env python3
"""Continuously evolve many-gene native input timelines from a verified checkpoint."""
import argparse
import hashlib
import json
from pathlib import Path
import signal
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'dm'/'tools'))
from evolution.core import Evolution
from evolution.episodes import NativeEpisodes,validate_episode
from evolution.live import TrialStopped,ui_matches
from evolution.replay import ReplayBridge
from evolution.reset import FridaReset,prepare_fast_reset
from evolution.timeline import gene_space,seed_genome


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--session',type=Path,required=True)
    ap.add_argument('--objective',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--generations',type=int,default=0,help='0 runs continuously until interrupted')
    ap.add_argument('--population',type=int,default=8)
    ap.add_argument('--repeats',type=int,default=2)
    ap.add_argument('--mutation-rate',type=float,default=.1)
    ap.add_argument('--mutation-sigma',type=float,default=.12)
    ap.add_argument('--seed-champion',type=Path)
    args=ap.parse_args()
    if args.generations<0:ap.error('generations must be nonnegative')
    config=json.loads(args.session.read_text());spec=json.loads(args.objective.read_text());validate_episode(spec)
    if not config.get('recovery',{}).get('death_steps'):ap.error('Session must include calibrated death_steps')
    # This open-loop GA needs player/health/UI observations, not actor rediscovery.
    config.pop('actor_discovery',None)
    args.output.mkdir(parents=True,exist_ok=True)
    config['checkpoint_state']=str((args.output/'checkpoint-state.json').resolve())
    prepared=prepare_fast_reset(config)
    package=Path(__file__).resolve().parents[1]/'dm'/'tools'/'evolution'
    implementation=hashlib.sha256(b''.join(p.read_bytes() for p in [Path(__file__),*(package/n for n in ('core.py','live.py','replay.py','timeline.py','episodes.py','reset.py'))])+json.dumps(config,sort_keys=True).encode()+(prepared.fingerprint() if prepared else b'')).hexdigest()
    initial=json.loads(args.seed_champion.read_text())['genome'] if args.seed_champion else seed_genome(spec.get('segments',128),spec.get('initial_direction',[0,1]))
    evolution=Evolution(args.output/'evolution.sqlite',[spec],args.population,args.repeats,implementation=implementation,
                        bounds=gene_space(spec.get('segments',128)),initial_genome=initial,
                        mutation_rate=args.mutation_rate,mutation_sigma=args.mutation_sigma)
    def interrupt(signum,frame):raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,interrupt)
    bridge=None;backend=None;log=None
    try:
        bridge=ReplayBridge(config)
        log=(args.output/'trajectory.jsonl').open('a')
        backend=FridaReset(prepared,bridge,log) if prepared else None
        env=NativeEpisodes(bridge,spec,log,backend);env.checkpoint.verify_files()
        status=bridge.rpc('replay_status')
        if status['active']:raise TrialStopped('Another native timeline is active')
        state=bridge.sample(actors=False)
        if ui_matches(config,state,'pause'):bridge.unpause()
        bridge.running=True
        _,reset_seconds=env.restore()
        final=evolution.state['generation']+args.generations;run_start=time.perf_counter();attempts=0
        while not args.generations or evolution.state['generation']<final:
            key=evolution.pending()
            if key is None:
                report=evolution.advance();tmp=args.output/'champion.tmp'
                tmp.write_text(json.dumps(report,indent=2)+'\n');tmp.replace(args.output/'champion.json')
                print(json.dumps(dict(event='generation',**report)),flush=True);continue
            print(json.dumps(dict(event='episode_started',key=key,genes=len(initial),reset_seconds=reset_seconds)),flush=True)
            result=env.evaluate(evolution.state['candidates'][key[1]])
            attempts+=1;result['reset_seconds']=reset_seconds
            result['attempts_per_minute']=attempts*60/max(time.perf_counter()-run_start,1e-9)
            evolution.record(key,result)
            print(json.dumps(dict(event='episode_finished',key=key,**result)),flush=True)
            _,reset_seconds=env.restore()
    except (TrialStopped,ValueError,OSError) as exc:
        with (args.output/'interruptions.jsonl').open('a') as interruptions:
            interruptions.write(json.dumps(dict(error=str(exc),state=bridge.last if bridge else None))+'\n')
        raise
    finally:
        try:
            if backend is not None:
                try:backend.close()
                finally:
                    # A queued game reload may outlive failed instrumentation.
                    # Releasing native input is safe; a Plus/menu fallback is not.
                    if backend.uncertain and bridge is not None:bridge.running=False
        finally:
            try:
                if bridge is not None:bridge.close()
            finally:
                if log is not None:log.close()
                evolution.db.close()


if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:
        print('Interrupted; completed attempts retained.',file=sys.stderr);sys.exit(130)
    except (TrialStopped,ValueError,OSError) as exc:
        print(str(exc),file=sys.stderr);sys.exit(1)
