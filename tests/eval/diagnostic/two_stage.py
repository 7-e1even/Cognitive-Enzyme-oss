"""Small paired Jev experiment using frozen candidates and existing production code.

prepare/report are offline. run calls only the configured Jev, in an isolated
process; it never retrieves, solves, learns, changes a memory, or writes settings.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import math
import shutil
import time
from collections import Counter
from pathlib import Path

from tests.eval.diagnostic.protocol import DEFAULT, append, canonical, dump, rows, sha

ARMS = {"single": None, "scan12": 12, "scan24": 24, "scan36": 36}
LOST = {"judge_budget_cut", "rank_or_judge_pool_cut"}
DEFAULT_OUTPUT = DEFAULT.parent / "two-stage-small"


def restore(task, rec, banks):
    from app import settings
    from tests.eval.diagnostic.collect import FrozenPG
    from app.matching import pipeline as pl
    from app.matching.scorer import compute_score
    from app.matching.pipeline import MatchResult

    memories = FrozenPG(banks[task["group"]], set(task["excluded"])).memories
    text_by_hash = {sha(text): text for e in memories.values()
                    for text in e.exemplars + e.anti_exemplars}
    hits = []
    for hit in rec.get("exemplar_hits", []):
        if hit["text_sha256"] not in text_by_hash:
            raise ValueError("Frozen exemplar missing: " + task["id"])
        hits.append({**hit, "_score": hit["score"], "text": text_by_hash[hit["text_sha256"]]})
    positive = [h for h in hits if h["kind"] == "pos"]
    hints, scores = pl._best_texts(positive), pl._best_scores(positive)
    negative = pl._best_texts([h for h in hits if h["kind"] == "anti"]) if settings.get("exemplar.anti") else {}
    for eid in rec["trace"]["recalled_ids"]:
        e = memories[eid]
        e._hint, e._hint_score = hints.get(eid, ""), scores.get(eid, 0.)
        e._anti_hint = negative.get(eid, "")
    refined = [memories[eid] for eid in rec["trace"]["refined_ids"]]
    pool = [memories[eid] for eid in rec["trace"]["judge_pool_before_fit"]]
    analogies = pl._analogy_candidates(refined)
    if any(e.id not in {x.id for x in pool} for e in analogies):
        raise ValueError("Analogy candidate absent from recorded raw pool")
    scored = [MatchResult(enzyme=memories[eid], score=score, tier=tier)
              for eid, score, tier in rec["trace"]["scored"]]
    have = {r.enzyme.id for r in scored}
    extras = {e.id: compute_score(e, 0.) for e in refined if e.id not in have}
    return {"memories": memories, "pool": pool, "analogies": analogies,
            "scored": scored, "refined": refined, "extra_scores": extras}


def select(app, restored, max_results):
    from app import settings
    from app.matching.pipeline import MatchResult
    from app.matching import pipeline as pl
    # Selection mutates MatchResult. Every arm receives its own instances.
    candidates = [dataclasses.replace(r) for r in restored["scored"]]
    if app is None:
        selected = pl._apply_fallback(candidates, max_results)
    else:
        have = {r.enzyme.id for r in candidates}
        rescued = set()
        if settings.get("judge.rescue"):
            extra = [MatchResult(enzyme=e, score=restored["extra_scores"][e.id], tier="peripheral")
                     for e in restored["refined"] if e.id in app.p and e.id not in have]
            candidates += extra
            rescued = {r.enzyme.id for r in extra}
        selected = pl._apply_jev_dosage(candidates, app, max_results, rescued_ids=rescued)
    injected = [r.enzyme.id for r in selected if r.enzyme.confidence >= .25 or r.exploratory][:max_results]
    return selected, injected


def scan_ids(restored, count):
    ids = [e.id for e in restored["pool"][:count]]
    return ids + [e.id for e in restored["analogies"] if e.id not in ids]


def bucket(pair, rec):
    pool = rec["trace"]["judge_pool_before_fit"]
    if pair["memory_id"] not in pool:
        return "absent"
    rank = pool.index(pair["memory_id"]) + 1
    return "1-12" if rank <= 12 else "13-24" if rank <= 24 else "25+"


def choose_questions(pairs, recs):
    # Only original loss stage/domain/rank and a fixed ID hash are used. No p,
    # shadow score, solver answer or benefit result is read for sample selection.
    quotas = [("math", "absent", 1), ("math", "25+", 1), ("math", "1-12", 2),
              ("physics", "absent", 2), ("physics", "25+", 2),
              ("physics", "13-24", 4), ("physics", "1-12", 4)]
    chosen = []
    for group, rank, count in quotas:
        candidates = {p["id"] for p in pairs if p["group"] == group and bucket(p,recs[p["id"]]) == rank}
        candidates -= set(chosen)
        ordered = sorted(candidates,key=lambda pid:sha("two-stage-small-v1:" + pid))
        if len(ordered) < count:
            raise ValueError("Insufficient questions for prespecified stratum")
        chosen += ordered[:count]
    return chosen


def prepare(source, directory):
    from app import settings
    source, directory = source.resolve(), directory.resolve()
    if directory.exists():
        raise ValueError("Use a new experiment directory")
    metadata = json.loads((source / "retrieval-manifest.json").read_text())
    settings._values.clear()
    settings._values.update(metadata["policy"])
    recs = {r["id"]:r for r in rows(source / "retrieval.jsonl")}
    tasks = {t["id"]:t for t in json.loads((source / "inputs/tasks.json").read_text())}
    banks = json.loads((source / "inputs/banks.json").read_text())
    pairs = [{k:r[k] for k in ("id","group","memory_id","stage")} for r in rows(source / "detailed-pairs.jsonl") if r["stage"] in LOST]
    if len(pairs) != 95:
        raise ValueError("Expected the frozen 95-pair loss cohort")
    target_ids = choose_questions(pairs,recs)
    controls = sorted({r["id"] for r in rows(source / "detailed-pairs.jsonl") if r["stage"] == "injected"} - set(target_ids),
                      key=lambda pid:sha("two-stage-controls-v1:"+pid))[:4]
    chosen = target_ids + controls
    sample_pairs = [p for p in pairs if p["id"] in target_ids]
    restored = {pid:restore(tasks[pid],recs[pid],banks) for pid in {p["id"] for p in pairs}}
    visibility = {}
    for arm, count in ARMS.items():
        seen = {pid: set(scan_ids(r,count)) if count else {e for e,_ in recs[pid]["trace"]["jev_pool"]}
                for pid,r in restored.items()}
        visibility[arm] = {"all_95":sum(p["memory_id"] in seen[p["id"]] for p in pairs),
                           "sample_lost_pairs":sum(p["memory_id"] in seen[p["id"]] for p in sample_pairs)}
    directory.mkdir(parents=True)
    inp = directory / "inputs"
    inp.mkdir()
    dump(inp / "tasks.json",[tasks[pid] for pid in chosen])
    dump(inp / "banks.json",banks)
    dump(inp / "metadata.json",metadata)
    (inp / "retrieval.jsonl").write_text("".join(canonical(recs[pid])+"\n" for pid in chosen))
    manifest = {"protocol":"frozen-candidate-two-stage-small-v1", "source":str(source),
                "question_ids":chosen,"target_question_ids":target_ids,"control_question_ids":controls,
                "target_pairs":sample_pairs,"all_95_pairs":pairs,"visibility_upper_bounds":visibility,
                "selection":"fixed domain/rank strata; IDs hashed with two-stage-small-v1; no probabilities or solver outcomes used",
                "arms":ARMS,"scan_batch":metadata["policy"]["judge.scan_batch"],
                "compare_k":metadata["policy"]["judge.compare_k"],"timeout_s":metadata["policy"]["judge.timeout"],
                "trials_per_question_arm":1,"arm_order":"balanced Latin-square rotation; hashed question order",
                "latency":"fresh HTTP on every arm; end-to-end Jev stage including semaphore/choice and native 6s timeout; excludes retrieval and solve",
                "frozen_files":{p.name:sha(p.read_bytes()) for p in inp.iterdir()},
                "source_sha256":{n:sha((source/n).read_bytes()) for n in ("retrieval.jsonl","detailed-pairs.jsonl","retrieval-manifest.json")},
                "core_sha256":core_hashes(),"runner_sha256":sha(Path(__file__).read_bytes())}
    dump(directory / "plan.json",manifest)
    return {"directory":str(directory),"target_questions":len(target_ids),"controls":len(controls),
            "sample_lost_pairs":len(sample_pairs),"visibility_upper_bounds":visibility,"api_calls":0}


def core_hashes():
    import app.typesafe_client as tc
    import app.matching.pipeline as pl
    import app.matching.selection as selection
    return {name:sha(Path(module.__file__).read_bytes()) for name,module in
            (("typesafe_client",tc),("pipeline",pl),("selection",selection))}


def load(directory):
    plan = json.loads((directory / "plan.json").read_text())
    for name,value in plan["frozen_files"].items():
        if sha((directory/"inputs"/name).read_bytes()) != value:
            raise ValueError("Experiment input changed: "+name)
    if core_hashes() != plan["core_sha256"]:
        raise ValueError("Production matching source differs from the planned version")
    tasks = {t["id"]:t for t in json.loads((directory/"inputs/tasks.json").read_text())}
    recs = {r["id"]:r for r in rows(directory/"inputs/retrieval.jsonl")}
    banks = json.loads((directory/"inputs/banks.json").read_text())
    metadata = json.loads((directory/"inputs/metadata.json").read_text())
    return plan,tasks,recs,banks,metadata


async def validate(directory):
    """Replay saved native answers, verifying exact request and hook selections."""
    from app import settings
    import app.typesafe_client as tc
    from app.config import TypeSafeConfig
    from app.matching import pipeline as pl
    plan,tasks,recs,banks,metadata = load(directory)
    settings._values.clear()
    settings._values.update(metadata["policy"])
    original = tc.system_one
    checked = []
    try:
        for pid in plan["question_ids"]:
            rec,task = recs[pid],tasks[pid]
            restored = restore(task,rec,banks)
            async def replay(state,questions,config,timeout=0):
                if canonical({"state":state,"questions":questions}) != canonical(rec["trace"]["jev_request"]):
                    raise ValueError("Native request replay differs: "+pid)
                return {"answers":rec["trace"]["jev_answers"],"model":rec["trace"]["jev_model"]}
            tc.system_one = replay
            app = await tc.judge_applicability(task["problem"]["problem"],restored["pool"],TypeSafeConfig(api_key="offline"),
                                               analogies=restored["analogies"])
            _,injected = select(app,restored,metadata["max_results"])
            if injected != rec["injected_ids"]:
                raise ValueError("Native selection replay differs: "+pid)
            checked.append(pid)
    finally:
        tc.system_one = original
    dump(directory/"replay-check.json",{"checked":checked,"request_mismatches":0,"injection_mismatches":0,"api_calls":0})
    return {"checked":len(checked),"request_mismatches":0,"injection_mismatches":0,"api_calls":0}


class FreshHTTP:
    """Record every wire attempt; never cache-hit while measuring latency."""
    def __init__(self,directory,secrets):
        import httpx
        self.client=httpx.AsyncClient(timeout=10.)
        self.directory=directory
        self.secrets=[s for s in secrets if s]
        self.records=[]
        self.active=0
        self.peak=0
        self.auth_block=False
    @property
    def is_closed(self):
        return self.client.is_closed
    def clean(self,value):
        if isinstance(value,str):
            for secret in self.secrets:value=value.replace(secret,"[REDACTED]")
            return value
        if isinstance(value,dict):return {k:self.clean(v) for k,v in value.items()}
        if isinstance(value,list):return [self.clean(v) for v in value]
        return value
    async def post(self,url,*,headers=None,json=None,timeout=None):
        import httpx
        index=len(self.records)
        record={"attempt":index,"request":json,"request_sha256":sha(canonical(json)),
                "endpoint_sha256":sha(url),"started_monotonic":time.perf_counter()}
        self.records.append(record)
        self.active+=1
        self.peak=max(self.peak,self.active)
        try:
            response=await self.client.post(url,headers=headers,json=json,timeout=timeout)
            try:body=response.json()
            except ValueError:body={"text":response.text[:2000]}
            record.update(status=response.status_code,response=self.clean(body))
            if response.status_code in (401,402,403):self.auth_block=True
            return response
        except asyncio.CancelledError:
            record.update(status="cancelled_by_stage_deadline")
            raise
        except httpx.HTTPError as exc:
            record.update(status=type(exc).__name__)
            raise
        finally:
            self.active-=1
            record["secs"]=round(time.perf_counter()-record["started_monotonic"],6)
            dump(self.directory/f"http-{index:03d}.json",record)


async def run(directory):
    from app import settings
    from app.matching import pipeline as pl
    import app.typesafe_client as tc
    from tests.eval.diagnostic.collect import configuration
    plan,tasks,recs,banks,metadata=load(directory)
    if sha(Path(__file__).read_bytes()) != plan["runner_sha256"]:
        raise ValueError("Experiment runner changed after sample selection")
    await validate(directory)
    config,registry,current=await configuration()
    if config.typesafe.model != metadata["models"]["jev"]:
        raise ValueError("Configured Jev model changed")
    settings._values.clear()
    settings._values.update(metadata["policy"])
    completed={(r["id"],r["arm"]) for r in rows(directory/"results.jsonl")}
    dump(directory/"execution.json",{"core_sha256":core_hashes(),"model":config.typesafe.model,
                                    "endpoint_sha256":sha(config.typesafe.api_url.rstrip('/')+'/systemone'),
                                    "runner_sha256":sha(Path(__file__).read_bytes()),"production_writes":0,
                                    "fresh_http":True,"retrieval_api_calls":0,"solver_api_calls":0})
    ordered=sorted(plan["question_ids"],key=lambda pid:sha("two-stage-order-v1:"+pid))
    arms=list(ARMS)
    consecutive_service_failures=0
    try:
        for index,pid in enumerate(ordered):
            task,rec=tasks[pid],recs[pid]
            restored=restore(task,rec,banks)
            order=arms[index%len(arms):]+arms[:index%len(arms)]
            for arm in order:
                if (pid,arm) in completed:continue
                if (directory/"service-block.json").exists():return
                trialdir=directory/"trials"/pid/arm
                trialdir.mkdir(parents=True,exist_ok=False)
                http=FreshHTTP(trialdir,[p.api_key for p in registry.providers.values()])
                tc._client=http
                settings._values["judge.two_stage"]=arm!="single"
                settings._values["judge.scan_pool"]=ARMS[arm] or metadata["policy"]["judge.scan_pool"]
                trace={}
                started=time.perf_counter()
                judges=pl._start_judges(task["problem"]["problem"],restored["pool"],config.typesafe,None,restored["analogies"])
                try:
                    app,_=await pl._collect_judges(judges[0],judges[1],judges[2],trace,None,
                                                   task["problem"]["problem"],judges[3])
                finally:
                    pending=[t for t in judges[:2] if t is not None and not t.done()]
                    for t in pending:t.cancel()
                    if pending:await asyncio.gather(*pending,return_exceptions=True)
                elapsed=time.perf_counter()-started
                selected,injected=select(app,restored,metadata["max_results"])
                record={"id":pid,"group":task["group"],"arm":arm,"order":order.index(arm),
                        "judge_ok":app is not None,"jev_stage_s":round(elapsed,6),"wire_attempts":len(http.records),
                        "wire_statuses":dict(Counter(str(r["status"]) for r in http.records)),"peak_http_concurrency":http.peak,
                        "injected_ids":injected,"selected_ids":[r.enzyme.id for r in selected],
                        "historical_injected_ids":rec["injected_ids"],"native_pool_ids":[e for e,_ in rec["trace"]["jev_pool"]],
                        "raw_pool_ids":[e.id for e in restored["pool"]],"logical_calls":app.calls if app else None,
                        "p":app.p if app else {},"q":app.q if app else {},"none":app.none if app else None,
                        "scan_ids":scan_ids(restored,ARMS[arm]) if ARMS[arm] else [e for e,_ in rec["trace"]["jev_pool"]],
                        "application":dataclasses.asdict(app) if app else None,"trace":trace}
                dump(trialdir/"result.json",record)
                append(directory/"results.jsonl",record)
                completed.add((pid,arm))
                print(f"trial {len(completed)}/{len(tasks)*len(ARMS)} {pid} {arm} ok={app is not None} s={elapsed:.3f} wire={len(http.records)}",flush=True)
                await http.client.aclose()
                failed_service=app is None and any(r.get("status") in (401,402,403,429,500,502,503) for r in http.records)
                consecutive_service_failures=consecutive_service_failures+1 if failed_service else 0
                if http.auth_block or consecutive_service_failures>=2:
                    dump(directory/"service-block.json",{"reason":"provider auth rejection or two consecutive failed-service trials; no further automatic collection"})
                    return
    finally:
        tc._client=None
    print(json.dumps(summarize(directory),ensure_ascii=False,indent=2),flush=True)


def quantile(values,q):
    if not values:return None
    ordered=sorted(values)
    rank=(len(ordered)-1)*q
    low=math.floor(rank)
    high=math.ceil(rank)
    return ordered[low]+(ordered[high]-ordered[low])*(rank-low)


def summarize(directory):
    plan=json.loads((directory/"plan.json").read_text())
    records={(r["id"],r["arm"]):r for r in rows(directory/"results.jsonl")}
    result={"target_questions":len(plan["target_question_ids"]),"controls":len(plan["control_question_ids"]),
            "sample_lost_pairs":len(plan["target_pairs"]),"all_95_visibility_upper_bounds":plan["visibility_upper_bounds"],"arms":{}}
    pair_details=[]
    for arm in ARMS:
        measured=[r for (pid,a),r in records.items() if a==arm]
        complete_pairs=[p for p in plan["target_pairs"] if (p["id"],arm) in records]
        recovered=[p for p in complete_pairs if records[p["id"],arm]["judge_ok"] and p["memory_id"] in records[p["id"],arm]["injected_ids"]]
        for p in complete_pairs:
            r=records[p["id"],arm];eid=p["memory_id"]
            pair_details.append({**p,"arm":arm,"judge_ok":r["judge_ok"],"planned_visible":eid in r["scan_ids"],
                                 "scored":eid in r["p"],"p":r["p"].get(eid),"q":r["q"].get(eid),
                                 "injected":eid in r["injected_ids"],"seconds":r["jev_stage_s"]})
        deltas=[r["jev_stage_s"]-records[r["id"],"single"]["jev_stage_s"] for r in measured if (r["id"],"single") in records]
        result["arms"][arm]={"measured_questions":len(measured),"judge_successes":sum(r["judge_ok"] for r in measured),
                              "target_pairs_measured":len(complete_pairs),"lost_pairs_now_scored":sum(p["memory_id"] in records[p["id"],arm]["p"] for p in complete_pairs),
                              "lost_pairs_injected":len(recovered),"latency_p50_s":quantile([r["jev_stage_s"] for r in measured],.5),
                              "latency_p90_s":quantile([r["jev_stage_s"] for r in measured],.9),
                              "paired_latency_delta_p50_s":quantile(deltas,.5),"wire_attempts":sum(r["wire_attempts"] for r in measured),
                              "wire_per_question":sum(r["wire_attempts"] for r in measured)/len(measured) if measured else None,
                              "historical_injections_retained":sum(len(set(r["historical_injected_ids"])&set(r["injected_ids"])) for r in measured),
                              "all_injected":sum(len(r["injected_ids"]) for r in measured)}
    dump(directory/"summary.json",result)
    (directory/"pairs.jsonl").write_text("".join(canonical(r)+"\n" for r in pair_details))
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("stage",choices=("prepare","validate","run","report"))
    p.add_argument("--source",type=Path,default=DEFAULT)
    p.add_argument("--directory",type=Path,default=DEFAULT_OUTPUT)
    args=p.parse_args()
    if args.stage=="prepare":out=prepare(args.source,args.directory)
    elif args.stage=="validate":out=asyncio.run(validate(args.directory))
    elif args.stage=="run":out=asyncio.run(run(args.directory))
    else:out=summarize(args.directory)
    if out is not None:print(json.dumps(out,ensure_ascii=False,indent=2))


if __name__=="__main__":main()
