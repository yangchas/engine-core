#!/usr/bin/env python3
"""Measure guardedian jobs without permitting artifact/catalog writes."""
from __future__ import annotations
import argparse, json, os, statistics, subprocess, sys, time, tracemalloc
from pathlib import Path

LIVE_ROOT = Path('/home/exedev/repos/engine_core-live')
REGISTRY = LIVE_ROOT / 'src/engine_core/guardian/datasets.registry.json'
WRITE_FLAGS = {'--execute', '--write', '--td-write', '--commit', '--publish'}


def registry_jobs():
    return json.loads(REGISTRY.read_text())['items']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', required=True)
    ap.add_argument('--out', default='/home/exedev/fw/research/C365/timings.json')
    args = ap.parse_args()
    items = registry_jobs()
    results = []
    for item in items:
        spec = item.get('fetch_job')
        if not spec:
            continue
        argv = spec.get('argv', []) if isinstance(spec, dict) else spec
        write_args = [x for x in argv if x in WRITE_FLAGS]
        # The registered jobs invoke writing modes. Never execute these against live stores.
        results.append({'id': item['id'], 'kind': 'fetch_job', 'status': 'UNAVAILABLE',
                        'reason': 'registered command requires write mode; no read-only fetch adapter',
                        'attempts': 0, 'samples_seconds': [], 'peak_memory_bytes': None,
                        'write_occurred': False, 'write_flags': write_args})

    # Startup handlers with explicit safe read-only inputs. Boot apply is planned only;
    # no production state/catalog write is allowed and no fetch command is dispatched.
    code = r'''import json,sys,resource
sys.path.insert(0, "/home/exedev/repos/engine_core-live/src")
from engine_core.guardian.startup import boot_trade_day,boot_data_health,boot_reconcile,boot_apply_plan
from pathlib import Path
D=sys.argv[1]; ROOT=Path("/home/exedev/core-data")
trade=boot_trade_day(D,ROOT,probes=False)
health=boot_data_health(D,None,ROOT,dry_run=True)
rec=boot_reconcile(D,"POST_MARKET",health,ROOT)
plan=rec.get("detail",{}).get("plan",[])
apply=boot_apply_plan(D,plan,ROOT,phase="POST_MARKET",dry_run=True)
print(json.dumps({"boot_trade_day":trade,"boot_data_health":health,"boot_reconcile":rec,"boot_apply_plan":apply,"peak_rss_bytes":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024}))'''
    for name in ('boot_trade_day','boot_data_health','boot_reconcile','boot_apply_plan'):
        samples=[]; peaks=[]; statuses=[]
        for _ in range(3):
            env=os.environ.copy(); env['PYTHONPATH']=str(LIVE_ROOT/'src'); env['GUARDIAN_DRY_RUN']='1'
            tracemalloc.start(); start=time.perf_counter()
            try:
                p=subprocess.run([sys.executable,'-c',code,args.date],cwd=LIVE_ROOT,env=env,
                                 capture_output=True,text=True,timeout=30,check=False)
                samples.append(time.perf_counter()-start)
                _, peak=tracemalloc.get_traced_memory(); peaks.append(peak); tracemalloc.stop()
                if p.returncode: statuses.append('FAILED:'+str(p.returncode))
                else:
                    payload=json.loads(p.stdout.splitlines()[-1]); statuses.append(payload[name].get('status','unknown')); peaks[-1]=int(payload['peak_rss_bytes'])
            except subprocess.TimeoutExpired:
                tracemalloc.stop(); samples.append(30.0); statuses.append('UNAVAILABLE:timeout_30s')
        results.append({'id':name,'kind':'boot_handler','status':'MEASURED' if all(x not in ('failed','unknown') and not x.startswith(('FAILED','UNAVAILABLE')) for x in statuses) else 'PARTIAL',
                        'attempts':len(samples),'samples_seconds':[round(x,4) for x in samples],
                        'min_seconds':round(min(samples),4),'median_seconds':round(statistics.median(samples),4),
                        'max_seconds':round(max(samples),4),'peak_memory_bytes':max(peaks) if peaks else None,
                        'handler_statuses':statuses,'write_occurred':False,
                        'suggested_cost':('light' if statistics.median(samples)<10 else 'medium' if statistics.median(samples)<=45 else 'heavy'),
                        'measurement':'read-only probes + dry-run planning; fetches not dispatched'})
    payload={'date':args.date,'generated_at':time.strftime('%Y-%m-%dT%H:%M:%S%z'),
             'source_registry':str(REGISTRY),'repetitions':3,'write_occurred':False,
             'cost_bands_seconds':{'light':'<10','medium':'10-45','heavy':'>45'},'jobs':results}
    out=Path(args.out); out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
    for row in results:
        print(row['id'],row['status'],row.get('min_seconds','-'),row.get('median_seconds','-'),row.get('max_seconds','-'))
    print('JSON',out)

if __name__=='__main__': main()
