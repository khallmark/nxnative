#!/usr/bin/env python3
"""Continuously evolve a feedback controller for a declared live game curriculum."""
import argparse
import hashlib
import json
from pathlib import Path
import signal
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'dm'/'tools'))
from evolution.core import Evolution
from evolution.live import Bridge, LiveTrials, TrialStopped


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--session',type=Path,required=True,help='Local runtime addresses and PID, never commit')
    ap.add_argument('--curriculum',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--seed-champion',type=Path,help='Seed a NEW experiment from an earlier champion; all trials are re-evaluated')
    ap.add_argument('--generations',type=int,default=1,help='0 evolves until interrupted')
    ap.add_argument('--population',type=int,default=8)
    ap.add_argument('--repeats',type=int,default=2)
    ap.add_argument('--start-paused',action='store_true',help='Normal pause menu visually confirmed')
    ap.add_argument('--inspect',action='store_true',help='Read telemetry only; send no buttons')
    args=ap.parse_args()
    if args.generations<0:
        ap.error('generations must be nonnegative')
    config=json.loads(args.session.read_text()); tasks=json.loads(args.curriculum.read_text())
    if not tasks or any(t.get('kind')!='reciprocal_navigation' for t in tasks):
        ap.error('This stage supports validated reciprocal_navigation tasks only')
    args.output.mkdir(parents=True,exist_ok=True)
    bridge=Bridge(config)
    try:
        state=bridge.sample()
        print(json.dumps(dict(event='attached',state=state)),flush=True)
        if args.inspect:
            return
        if not args.start_paused:
            ap.error('Visually confirm pause, then pass --start-paused')
        # This version accepts multiple scoring tasks at the same start anchor.
        from evolution.core import distance
        if any(distance(state['position'],t['start'])>t.get('start_tolerance',.3) for t in tasks):
            raise TrialStopped('Outside curriculum start: no input sent')
        package=Path(__file__).resolve().parents[1]/'dm'/'tools'/'evolution'
        implementation=hashlib.sha256(b''.join(p.read_bytes() for p in [Path(__file__),package/'core.py',package/'live.py'])).hexdigest()
        evolution=Evolution(args.output/'evolution.sqlite',tasks,args.population,args.repeats,implementation=implementation,
                            initial_genome=json.loads(args.seed_champion.read_text())['genome'] if args.seed_champion else None)
        final=evolution.state['generation']+args.generations
        def interrupt(signum,frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM,interrupt)
        with (args.output/'trajectory.jsonl').open('a') as log:
            env=LiveTrials(bridge,log)
            bridge.unpause()
            while not args.generations or evolution.state['generation']<final:
                key=evolution.pending()
                if key is None:
                    report=evolution.advance()
                    tmp=args.output/'champion.tmp'
                    tmp.write_text(json.dumps(report,indent=2)+'\n');tmp.replace(args.output/'champion.json')
                    print(json.dumps(dict(event='generation',**report)),flush=True)
                    continue
                g,i,t,r=key
                print(json.dumps(dict(event='trial_started',generation=g,candidate=i,task=t,repeat=r)),flush=True)
                try:
                    result=env.evaluate(tasks[t],evolution.state['candidates'][i])
                except TrialStopped as exc:
                    if exc.result is not None:
                        evolution.record(key,exc.result)
                    with (args.output/'interruptions.jsonl').open('a') as errors:
                        errors.write(json.dumps(dict(key=key,error=str(exc),state=bridge.last))+'\n')
                    raise
                evolution.record(key,result)
                print(json.dumps(dict(event='trial_finished',key=key,**result)),flush=True)
    finally:
        bridge.close()


if __name__=='__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Interrupted; completed trials retained.',file=sys.stderr)
        sys.exit(130)
    except (TrialStopped,ValueError,OSError) as exc:
        print(str(exc),file=sys.stderr)
        sys.exit(1)
