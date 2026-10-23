"""Paired pre/post-fix Jev replay over all 95 deferred positive pairs."""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import importlib.util
import json
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

from tests.eval.diagnostic.protocol import DEFAULT, canonical, dump, rows, sha, append
from tests.eval.diagnostic.two_stage import FreshHTTP, core_hashes, restore, select, quantile

OUTPUT = DEFAULT.parent / "two-stage-fix"


def legacy_module(directory):
    name = "app._jev_before_comparison_fix"
    spec = importlib.util.spec_from_file_location(name, directory/"legacy-method/app/typesafe_client.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def prepare(source, directory):
    from app import settings
    source, directory = source.resolve(), directory.resolve()
    if (directory/"plan.json").exists():
        raise ValueError("Experiment plan already exists")
    pairs = rows(source/"detailed-pairs.jsonl")
    targets = [{k:p[k] for k in ("id","group","memory_id","stage")} for p in pairs
               if p["stage"] in ("judge_budget_cut","rank_or_judge_pool_cut")]
    controls = [{k:p[k] for k in ("id","group","memory_id","stage")} for p in pairs if p["stage"]=="injected"]
    assert len(targets)==95 and len(controls)==16
    chosen = sorted({p["id"] for p in targets+controls}, key=lambda pid:sha("compare-fix-order-v1:"+pid))
    tasks = {t["id"]:t for t in json.loads((source/"inputs/tasks.json").read_text())}
    recs = {r["id"]:r for r in rows(source/"retrieval.jsonl")}
    metadata = json.loads((source/"retrieval-manifest.json").read_text())
    settings._values.clear()
    settings._values.update(metadata["policy"])
    banks = json.loads((source/"inputs/banks.json").read_text())
    directory.mkdir(exist_ok=True,parents=True)
    inp = directory/"inputs"
    inp.mkdir(exist_ok=False)
    dump(inp/"tasks.json",[tasks[pid] for pid in chosen])
    dump(inp/"banks.json",banks)
    dump(inp/"metadata.json",metadata)
    (inp/"retrieval.jsonl").write_text("".join(canonical(recs[pid])+"\n" for pid in chosen))
    for name in ("app/typesafe_client.py","app/matching/pipeline.py","app/settings.py","app/distill/export.py"):
        path = directory/"fixed-method"/name
        path.parent.mkdir(exist_ok=True,parents=True)
        shutil.copy2(name,path)
    # The legacy implementation is the frozen pre-fix snapshot saved by the
    # first small experiment; never reconstruct it from the current checkout.
    legacy_source = source.parent / "two-stage-fix/legacy-method"
    if not legacy_source.exists():
        raise ValueError("Missing frozen pre-fix source snapshot")
    shutil.copytree(legacy_source, directory/"legacy-method")
    plan = {"protocol":"comparison-budget-fix-v1", "question_ids":chosen,"target_pairs":targets,"control_pairs":controls,
            "arms":["legacy12","fixed12"],"stress_case":"physelite-2824","stress_arms":["legacy36","fixed36"],
            "scope":"all 95 deferred pairs and all 16 originally injected positives; no solve-outcome selection",
            "source_sha256":{name:sha((source/name).read_bytes()) for name in ("retrieval.jsonl","detailed-pairs.jsonl")},
            "input_sha256":{p.name:sha(p.read_bytes()) for p in inp.iterdir()},
            "fixed_core_sha256":core_hashes(),"legacy_typesafe_sha256":sha((directory/"legacy-method/app/typesafe_client.py").read_bytes()),
            "runner_sha256":sha(Path(__file__).read_bytes()),"timeout_s":metadata["policy"]["judge.timeout"],
            "latency":"fresh HTTP; shared keepalive client; same-question rotated legacy/fixed order; native stage deadline",
            "solver_status":"free provider availability check still rate-limited; do not substitute another solver"}
    dump(directory/"plan.json",plan)
    return {"questions":len(chosen),"target_pairs":len(targets),"original_control_pairs":len(controls),
            "planned_trials":2*len(chosen)+2,"api_calls":0}


class SharedHTTP(FreshHTTP):
    def __init__(self,directory,secrets,client):
        self.client,self.directory,self.secrets=client,directory,[s for s in secrets if s]
        self.records=[]
        self.active=0
        self.peak=0
        self.auth_block=False


def verify(directory):
    plan=json.loads((directory/"plan.json").read_text())
    for name,h in plan["input_sha256"].items():
        if sha((directory/"inputs"/name).read_bytes())!=h:raise ValueError("Input changed: "+name)
    if core_hashes()!=plan["fixed_core_sha256"]:raise ValueError("Fixed code differs from plan")
    if sha((directory/"legacy-method/app/typesafe_client.py").read_bytes())!=plan["legacy_typesafe_sha256"]:
        raise ValueError("Legacy code differs from plan")
    if sha(Path(__file__).read_bytes())!=plan["runner_sha256"]:raise ValueError("Runner differs from plan")
    return plan


async def run(directory):
    import httpx
    from app import settings
    import app.typesafe_client as fixed
    from app.matching import pipeline as pl
    from tests.eval.diagnostic.collect import configuration
    directory=directory.resolve()
    plan=verify(directory)
    legacy=legacy_module(directory)
    tasks={t["id"]:t for t in json.loads((directory/"inputs/tasks.json").read_text())}
    recs={r["id"]:r for r in rows(directory/"inputs/retrieval.jsonl")}
    banks=json.loads((directory/"inputs/banks.json").read_text())
    metadata=json.loads((directory/"inputs/metadata.json").read_text())
    settings._values.clear();settings._values.update(metadata["policy"])
    # Before any network calls, verify the original request and injection mapping
    # for the entire cohort using the saved raw answers.
    checked=[]
    source_one=legacy.system_one
    try:
        for pid in plan["question_ids"]:
            task,rec=tasks[pid],recs[pid]
            restored=restore(task,rec,banks)
            async def replay(state,questions,config,timeout=0):
                if canonical({"state":state,"questions":questions})!=canonical(rec["trace"]["jev_request"]):
                    raise ValueError("Original request mismatch: "+pid)
                return {"model":metadata["models"]["jev"],"answers":rec["trace"]["jev_answers"]}
            legacy.system_one=replay
            from app.config import TypeSafeConfig
            app=await legacy.judge_applicability(task["problem"]["problem"],restored["pool"],TypeSafeConfig(api_key="offline"),analogies=restored["analogies"])
            _,picked=select(app,restored,metadata["max_results"])
            if picked!=rec["injected_ids"]:raise ValueError("Original injection mismatch: "+pid)
            checked.append(pid)
    finally:legacy.system_one=source_one
    dump(directory/"replay-check.json",{"questions":checked,"request_mismatches":0,"selection_mismatches":0,"api_calls":0})
    config,registry,_=await configuration()
    if config.typesafe.model!=metadata["models"]["jev"]:raise ValueError("Jev model changed")
    settings._values.clear();settings._values.update(metadata["policy"])
    completed={(r["id"],r["arm"]) for r in rows(directory/"results.jsonl")}
    jobs=[(plan["stress_case"],arm) for arm in plan["stress_arms"]]
    for i,pid in enumerate(plan["question_ids"]):
        arms=plan["arms"] if i%2==0 else list(reversed(plan["arms"]))
        jobs.extend((pid,arm) for arm in arms)
    dump(directory/"execution.json",{"model":config.typesafe.model,"endpoint_sha256":sha(config.typesafe.api_url.rstrip('/')+'/systemone'),
         "fixed_core_sha256":core_hashes(),"legacy_typesafe_sha256":plan["legacy_typesafe_sha256"],"solver_api_calls":0,
         "retrieval_api_calls":0,"production_writes":0,"latency_includes_native_deadline":True,"shared_http_client":True})
    old_functions=(pl.judge_applicability,pl.judge_two_stage,pl.fit_pool)
    fail_streak=0
    async with httpx.AsyncClient(timeout=10.) as client:
        try:
            for pid,arm in jobs:
                if (pid,arm) in completed:continue
                if (directory/"service-block.json").exists():break
                implementation=legacy if arm.startswith("legacy") else fixed
                pl.judge_applicability=implementation.judge_applicability
                pl.judge_two_stage=implementation.judge_two_stage
                pl.fit_pool=implementation.fit_pool
                settings._values["judge.two_stage"]=True
                settings._values["judge.scan_pool"]=36 if arm.endswith("36") else 12
                task,rec=tasks[pid],recs[pid]
                restored=restore(task,rec,banks)
                trialdir=directory/"trials"/pid/arm
                trialdir.mkdir(parents=True,exist_ok=False)
                http=SharedHTTP(trialdir,[p.api_key for p in registry.providers.values()],client)
                implementation._client=http
                trace={}
                started=time.perf_counter()
                judges=pl._start_judges(task["problem"]["problem"],restored["pool"],config.typesafe,None,restored["analogies"])
                try:
                    app,_=await pl._collect_judges(judges[0],judges[1],judges[2],trace,None,task["problem"]["problem"],judges[3])
                finally:
                    pending=[t for t in judges[:2] if t is not None and not t.done()]
                    for t in pending:t.cancel()
                    if pending:await asyncio.gather(*pending,return_exceptions=True)
                elapsed=time.perf_counter()-started
                picked,injected=select(app,restored,metadata["max_results"])
                record={"id":pid,"group":task["group"],"arm":arm,"judge_ok":app is not None,
                        "jev_stage_s":round(elapsed,6),"wire_attempts":len(http.records),
                        "wire_statuses":dict(Counter(str(x["status"]) for x in http.records)),
                        "peak_http_concurrency":http.peak,"injected_ids":injected,
                        "selected":[{"id":r.enzyme.id,"reason":r.reason,"exploratory":r.exploratory} for r in picked],
                        "p":app.p if app else {},"q":app.q if app else {},"none":app.none if app else None,
                        "application":dataclasses.asdict(app) if app else None,"trace":trace}
                dump(trialdir/"result.json",record);append(directory/"results.jsonl",record)
                completed.add((pid,arm))
                print(f"trial {len(completed)}/{len(jobs)} {pid} {arm} ok={app is not None} s={elapsed:.3f} calls={len(http.records)}",flush=True)
                service_failure=app is None and any(x.get("status") in (401,402,403,429,500,502,503) for x in http.records)
                fail_streak=fail_streak+1 if service_failure else 0
                if http.auth_block or fail_streak>=2:
                    dump(directory/"service-block.json",{"reason":"auth rejection or repeated provider failure; stop remaining API calls"})
                    break
        finally:
            pl.judge_applicability,pl.judge_two_stage,pl.fit_pool=old_functions
            legacy._client=None;fixed._client=None
    print(json.dumps(report(directory),ensure_ascii=False,indent=2),flush=True)


def report(directory):
    directory=directory.resolve()
    plan=json.loads((directory/"plan.json").read_text())
    records={(r["id"],r["arm"]):r for r in rows(directory/"results.jsonl")}
    result={"target_pairs":95,"original_control_pairs":16,"questions":len(plan["question_ids"]),"arms":{},"stress":{}}
    for arm in plan["arms"]:
        measured=[r for (pid,a),r in records.items() if a==arm]
        idx={r["id"]:r for r in measured}
        target=[p for p in plan["target_pairs"] if p["id"] in idx]
        original=[p for p in plan["control_pairs"] if p["id"] in idx]
        wire=Counter();usage=Counter()
        for r in measured:
            wire.update(r["wire_statuses"])
            for path in (directory/"trials"/r["id"]/arm).glob("http-*.json"):
                raw=json.loads(path.read_text())
                if raw.get("status")==200:usage.update((raw.get("response") or {}).get("usage") or {})
        result["arms"][arm]={"measured_questions":len(measured),"judge_successes":sum(r["judge_ok"] for r in measured),
          "pairs_measured":len(target),"target_scored":sum(p["memory_id"] in idx[p["id"]]["p"] for p in target),
          "target_injected":sum(idx[p["id"]]["judge_ok"] and p["memory_id"] in idx[p["id"]]["injected_ids"] for p in target),
          "original_positive_retained":sum(p["memory_id"] in idx[p["id"]]["injected_ids"] for p in original),
          "wire_attempts":sum(r["wire_attempts"] for r in measured),"wire_statuses":dict(wire),"reported_usage":dict(usage),
          "latency_p50_s":quantile([r["jev_stage_s"] for r in measured],.5),"latency_p90_s":quantile([r["jev_stage_s"] for r in measured],.9),
          "all_injected":sum(len(r["injected_ids"]) for r in measured)}
    for arm in plan["stress_arms"]:
        r=records.get((plan["stress_case"],arm))
        if r:
            eid="4f051286-6273-4582-a071-fb02b8e871c5"
            result["stress"][arm]={"p":r["p"].get(eid),"q":r["q"].get(eid),"injected":eid in r["injected_ids"],"judge_ok":r["judge_ok"],
                                   "compared":len(r["q"]),"seconds":r["jev_stage_s"],"wire_attempts":r["wire_attempts"]}
    paired=[(records[pid,"legacy12"],records[pid,"fixed12"]) for pid in plan["question_ids"] if (pid,"legacy12") in records and (pid,"fixed12") in records]
    result["paired_latency_delta_p50_s"]=quantile([b["jev_stage_s"]-a["jev_stage_s"] for a,b in paired],.5)
    result["complete"]=len(records)==2*len(plan["question_ids"])+2
    dump(directory/"summary.json",result)
    return result


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("stage",choices=("prepare","run","report"))
    p.add_argument("--source",type=Path,default=DEFAULT)
    p.add_argument("--directory",type=Path,default=OUTPUT)
    args=p.parse_args()
    if args.stage=="prepare":result=prepare(args.source,args.directory)
    elif args.stage=="run":result=asyncio.run(run(args.directory))
    else:result=report(args.directory)
    if result:print(json.dumps(result,ensure_ascii=False,indent=2))
