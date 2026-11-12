#!/usr/bin/env python3
"""Continuously evolve many-gene native input timelines from a verified checkpoint."""
import argparse
import hashlib
import json
from pathlib import Path
import signal
import sys
import time
import traceback
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'dm'/'tools'))
from evolution.core import Evolution
from evolution.episodes import NativeEpisodes,NativeUIInterruption,validate_episode
from evolution.handoff import assert_no_pending_handoff,wait_for_handoff
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
    ap.add_argument('--initial-sigma',type=float,default=.35,help='Initial population mutation spread; independent of later mutation sigma')
    ap.add_argument('--seed-champion',type=Path)
    ap.add_argument('--resident-handoff',action='store_true',help='Keep the same attachment resident for explicitly guarded UI commands')
    args=ap.parse_args()
    if args.generations<0:ap.error('generations must be nonnegative')
    config=json.loads(args.session.read_text());spec=json.loads(args.objective.read_text());validate_episode(spec)
    if not config.get('recovery',{}).get('death_steps'):ap.error('Session must include calibrated death_steps')
    # This open-loop GA needs player/health/UI observations, not actor rediscovery.
    config.pop('actor_discovery',None)
    args.output.mkdir(parents=True,exist_ok=True)
    assert_no_pending_handoff(args.output,config)
    config['checkpoint_state']=str((args.output/'checkpoint-state.json').resolve())
    prepared=prepare_fast_reset(config)
    package=Path(__file__).resolve().parents[1]/'dm'/'tools'/'evolution'
    implementation=hashlib.sha256(b''.join(p.read_bytes() for p in [Path(__file__),*(package/n for n in ('core.py','live.py','replay.py','timeline.py','episodes.py','reset.py','handoff.py'))])+json.dumps(config,sort_keys=True).encode()+(prepared.fingerprint() if prepared else b'')).hexdigest()
    policy=spec.get('policy','timeline_v1')
    initial=json.loads(args.seed_champion.read_text())['genome'] if args.seed_champion else seed_genome(spec.get('segments',128),spec.get('initial_direction',[0,1]),policy)
    evolution=Evolution(args.output/'evolution.sqlite',[spec],args.population,args.repeats,implementation=implementation,
                        bounds=gene_space(spec.get('segments',128),policy),initial_genome=initial,
                        mutation_rate=args.mutation_rate,mutation_sigma=args.mutation_sigma,initial_sigma=args.initial_sigma)
    def interrupt(signum,frame):raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,interrupt)
    bridge=None;backend=None;log=None;primary=None;cleanup_errors=[]
    try:
        bridge=ReplayBridge(config)
        assert_no_pending_handoff(args.output,config)
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
            genome=evolution.state['candidates'][key[1]]
            try:result=env.evaluate(genome)
            except NativeUIInterruption as interruption:
                wait_for_handoff(bridge,backend,args.output,interruption,key=key,genome=genome,
                    objective=spec,implementation=implementation,log=log,resident=args.resident_handoff)
                break
            attempts+=1;result['reset_seconds']=reset_seconds
            result['attempts_per_minute']=attempts*60/max(time.perf_counter()-run_start,1e-9)
            evolution.record(key,result)
            print(json.dumps(dict(event='episode_finished',key=key,**result)),flush=True)
            _,reset_seconds=env.restore()
    except BaseException as exc:
        primary=exc
        if isinstance(exc,(TrialStopped,ValueError,OSError)):
            try:
                with (args.output/'interruptions.jsonl').open('a') as interruptions:
                    interruptions.write(json.dumps(dict(error=str(exc),state=bridge.last if bridge else None,
                        traceback=traceback.format_exc()))+'\n')
            except BaseException as logging_error:
                cleanup_errors.append(('interruptions.write',logging_error,logging_error.__traceback__))
        raise
    finally:
        def cleanup(operation,callback):
            try:callback();return True
            except BaseException as error:
                cleanup_errors.append((operation,error,error.__traceback__));return False
        if backend is not None:
            backend_closed=cleanup('backend.close',backend.close)
            # A failed close may leave instrumentation active even if its flag
            # was not updated. Release native input without a Plus/menu fallback.
            if bridge is not None and (not backend_closed or backend.uncertain):bridge.running=False
        if bridge is not None:cleanup('bridge.close',bridge.close)
        if log is not None:cleanup('trajectory.close',log.close)
        cleanup('database.close',evolution.db.close)
        for operation,error,tb in cleanup_errors:
            try:
                with (args.output/'cleanup-errors.jsonl').open('a') as errors:
                    errors.write(json.dumps(dict(event='cleanup_error',operation=operation,error=str(error),
                        traceback=''.join(traceback.format_exception(type(error),error,tb))))+'\n')
            except BaseException as logging_error:
                # Diagnostics must never replace the original failure either.
                try:print(f'{operation} failed: {error}; cleanup log failed: {logging_error}',file=sys.stderr)
                except BaseException:pass
        if primary is None and cleanup_errors:
            _,error,tb=cleanup_errors[0]
            raise error.with_traceback(tb)


if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:
        print('Interrupted; completed attempts retained.',file=sys.stderr);sys.exit(130)
    except (TrialStopped,ValueError,OSError) as exc:
        print(str(exc),file=sys.stderr);sys.exit(1)
