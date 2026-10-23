"""Offline report for the paired, frozen-candidate Jev experiment."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from tests.eval.diagnostic.protocol import canonical, dump, rows, sha
from tests.eval.diagnostic.two_stage import ARMS, DEFAULT_OUTPUT, bucket, quantile, summarize


def analyze(directory):
    directory=directory.resolve()
    summary=summarize(directory)
    plan=json.loads((directory/"plan.json").read_text())
    records={(r["id"],r["arm"]):r for r in rows(directory/"results.jsonl")}
    tasks={t["id"]:t for t in json.loads((directory/"inputs/tasks.json").read_text())}
    recs={r["id"]:r for r in rows(directory/"inputs/retrieval.jsonl")}
    banks=json.loads((directory/"inputs/banks.json").read_text())
    memories={e["id"]:e for bank in banks.values() for e in bank}
    details=[]
    for pair in plan["target_pairs"]:
        pid,eid=pair["id"],pair["memory_id"]
        source_pool=recs[pid]["trace"]["judge_pool_before_fit"]
        row={**pair,"rank_bucket":bucket(pair,recs[pid]),
             "original_raw_pool_rank":source_pool.index(eid)+1 if eid in source_pool else None,
             "rule":memories[eid]["then"],"arms":{}}
        for arm in ARMS:
            r=records.get((pid,arm))
            if not r:
                row["arms"][arm]={"state":"unmeasured"}
                continue
            visible=eid in r["scan_ids"]
            scored=eid in r["p"]
            injected=eid in r["injected_ids"]
            state=("injected" if r["judge_ok"] and injected else "not_in_scan" if not visible
                   else "judge_failed_or_timed_out" if not r["judge_ok"]
                   else "scan_batch_failed_or_unparsed" if not scored else "scored_not_selected")
            row["arms"][arm]={"state":state,"p":r["p"].get(eid),"q":r["q"].get(eid),
                                "joint_compared":eid in r["q"],"injected":injected,"judge_ok":r["judge_ok"]}
        details.append(row)
    wire=Counter()
    for arm in ARMS:
        arm_summary=summary["arms"][arm]
        measured=[r for (pid,a),r in records.items() if a==arm]
        arm_summary["loss_states"]=dict(Counter(d["arms"][arm]["state"] for d in details))
        statuses=Counter()
        tokens=Counter()
        response_models=Counter()
        for r in measured:
            statuses.update(r["wire_statuses"])
            for path in (directory/"trials"/r["id"]/arm).glob("http-*.json"):
                value=json.loads(path.read_text())
                response=value.get("response") or {}
                if value.get("status")==200:
                    response_models[response.get("model","missing")]+=1
                    tokens.update({k:v for k,v in (response.get("usage") or {}).items() if isinstance(v,(int,float))})
        wire.update(statuses)
        arm_summary["wire_statuses"]=dict(statuses)
        arm_summary["provider_reported_tokens"]=dict(tokens)
        arm_summary["response_models"]=dict(response_models)
        nominees=lambda r:sorted((eid for eid in r["scan_ids"] if eid in r["p"]),
                                  key=lambda eid:r["p"][eid],reverse=True)[:plan["compare_k"]]
        arm_summary["second_stage_budget_cut_trials"]=sum(r["judge_ok"] and len(r["q"])<len(nominees(r)) for r in measured) if arm!="single" else 0
        arm_summary["joint_compared_p50"]=quantile([len(r["q"]) for r in measured],.5)
        controls=[]
        for pid in plan["control_question_ids"]:
            a,b=records.get((pid,arm)),records.get((pid,"single"))
            if not a or not b:continue
            positives=set(tasks[pid]["expected_ids"])
            baseline=set(b["injected_ids"])&positives
            lost=baseline-set(a["injected_ids"])
            diagnosis={}
            for eid in lost:
                state=("scan_batch_failed_or_unparsed" if eid in a["scan_ids"] and eid not in a["p"]
                       else "outside_scan" if eid not in a["scan_ids"]
                       else "second_stage_budget_cut" if eid in nominees(a) and eid not in a["q"]
                       else "second_stage_p_ranking_cut" if eid not in nominees(a)
                       else "scored_selection_changed")
                diagnosis[eid]={"state":state,"p":a["p"].get(eid),"q":a["q"].get(eid),
                                "single_p":b["p"].get(eid),"single_q":b["q"].get(eid),
                                "stage2_nominees":nominees(a),"actual_joint_ids":list(a["q"])}
            controls.append({"id":pid,"single_positive_ids":sorted(baseline),
                             "retained":sorted(baseline&set(a["injected_ids"])),
                             "lost":sorted(lost),"loss_diagnosis":diagnosis,"judge_ok":a["judge_ok"]})
        arm_summary["controls"]=controls
        arm_summary["control_positive_pairs_retained"]=sum(len(c["retained"]) for c in controls)
        arm_summary["control_positive_pairs_lost"]=sum(len(c["lost"]) for c in controls)
        paired=[(r,records[r["id"],"single"]) for r in measured if (r["id"],"single") in records]
        arm_summary["paired_injection_count_delta"]=sum(len(a["injected_ids"])-len(b["injected_ids"]) for a,b in paired)
        arm_summary["unlabelled_injections"]=sum(eid not in tasks[r["id"]]["expected_ids"] for r in measured for eid in r["injected_ids"])
        base_calls=sum(b["wire_attempts"] for a,b in paired)
        arm_summary["paired_wire_ratio"]=sum(a["wire_attempts"] for a,b in paired)/base_calls if base_calls else None
        arm_summary["paired_extra_wire_per_question"]=sum(a["wire_attempts"]-b["wire_attempts"] for a,b in paired)/len(paired) if paired else None
        arm_summary["late_trials_over_6s"]=sum(r["jev_stage_s"]>6.05 for r in measured)
    summary["sample_strata"]=dict(Counter(d["group"]+"/"+d["rank_bucket"] for d in details))
    summary["actual_wire_statuses"]=dict(wire)
    summary["complete"]=len(records)==len(plan["question_ids"])*len(ARMS)
    base_set={(d["id"],d["memory_id"]) for d in details if d["arms"]["scan12"]["state"]=="injected"}
    summary["target_changes_vs_scan12"]={}
    for arm in ("scan24","scan36"):
        selected={(d["id"],d["memory_id"]) for d in details if d["arms"][arm]["state"]=="injected"}
        summary["target_changes_vs_scan12"][arm]={"new":sorted(selected-base_set),"lost":sorted(base_set-selected)}
    summary["limits"]=["stratified small sample, not a random prevalence estimate for all 95 pairs",
                       "positive labels are partial; unlabelled injection is unknown, not a false positive",
                       "one fresh trial per question/arm; tail latency and service stability need a larger follow-up",
                       "Jev-stage timing excludes retrieval and solver inference; no solve-success claim",
                       "unchanged thresholds, state/rule truncation, raw candidate pool and native timeout"]
    dump(directory/"analysis.json",summary)
    (directory/"pair-analysis.jsonl").write_text("".join(canonical(r)+"\n" for r in details))
    write_report(directory,summary,plan,details)
    dump(directory/"analysis-method.json",{"code_sha256":sha(Path(__file__).read_bytes()),"api_calls":0,
          "source_sha256":{n:sha((directory/n).read_bytes()) for n in ("plan.json","results.jsonl")},
          "execution_runner_sha256":plan["runner_sha256"]})
    return summary


def write_report(directory,summary,plan,details):
    n=summary["sample_lost_pairs"]
    lines=["# 现有分批判定的小范围对照", "",
           f"冻结候选、模型、提示语、规则/题面截断、选择阈值与 6 秒判定预算，比较原单批与现有 two_stage 扫描 12/24/36 条。样本为 {summary['target_questions']} 道损失题、{summary['controls']} 道原已注入题；损失题覆盖 95 组中的 {n} 组。按领域/原候选排名分层，再按固定 ID 哈希抽取，没有使用 p、影子判定或解题成绩挑题。", "",
           "所有配置都在 61 服务器的独立测试进程中；生产配置及数据不写入。检索、embedding、rerank 和解题 API 均不调用，只对同一候选调用 Jev。每个题目/方案一次新请求，采用轮换顺序；计时含扫描并发排队、第二阶段比较及超时，不含上游检索和模型解题。各试次使用独立 HTTP 客户端，包含连接建立成本；不能直接当作线上连接复用条件下的延迟。", "",
           "## 95 组的可见性上限", "", "| 方案 | 全部 95 组中最多能看到 | 本次样本最多能看到 |", "|---|---:|---:|"]
    for arm in ARMS:
        v=plan["visibility_upper_bounds"][arm]
        lines.append(f"| {arm} | {v['all_95']} | {v['sample_lost_pairs']} |")
    lines += ["", "95 组里，17 组在原始判定池之外。现有分批路径只改变原池中的判定方式，无法救回这 17 组。其余 78 组是请求预算裁剪；上限表还包含额外加入的类比候选，所以不等同于简单截取前 N 条。上限表示能被扫描，不表示能通过选择，更不表示能帮助解题。", "",
              "## 实测结果", "", "| 方案 | 样本损失组拿到分数 | 最终注入 | 判定 p50 / p90 秒 | 配对延迟增量中位数 | 实际 HTTP/题 |", "|---|---:|---:|---:|---:|---:|"]
    for arm,a in summary["arms"].items():
        fmt=lambda x:f"{x:.3f}" if x is not None else "—"
        lines.append(f"| {arm} | {a['lost_pairs_now_scored']}/{n} | {a['lost_pairs_injected']}/{n} | {fmt(a['latency_p50_s'])} / {fmt(a['latency_p90_s'])} | {fmt(a['paired_latency_delta_p50_s'])} | {a['wire_per_question']:.2f} |" if a['wire_per_question'] is not None else f"| {arm} | 未完成 | | | | |")
    lines += ["", "实际 HTTP 计数包含并发限流后的原生重试及失败请求；没有用缓存降低计时，也没有额外补重试来美化结果。p90 是本次小样本分位数，不能视作稳定的线上尾延迟。", "",
              "| 方案 | 扫描范围外 | 请求/整阶段失败而无分数 | 有分数但未注入 | 新增 HTTP/题 | 原已注入正例保留/丢失 |", "|---|---:|---:|---:|---:|---:|"]
    for arm,a in summary["arms"].items():
        states=a["loss_states"]
        failures=states.get("scan_batch_failed_or_unparsed",0)+states.get("judge_failed_or_timed_out",0)
        extra=a["paired_extra_wire_per_question"]
        lines.append(f"| {arm} | {states.get('not_in_scan',0)} | {failures} | {states.get('scored_not_selected',0)} | {extra:.2f} | {a['control_positive_pairs_retained']} / {a['control_positive_pairs_lost']} |" if extra is not None else f"| {arm} | 未完成 | | | | |")
    lines += ["", f"原始 HTTP 状态合计：`{canonical(summary['actual_wire_statuses'])}`。整体返回有效判定仍可能有扫描批失败，逐条缺分在上表单独保留，不记成语义拒绝。", "",
              "## 当前选择建议与具体退步", "",
              "本次优先保留扫描 12 条作为下一轮验证候选。它已选回 7/17 个损失组，同题延迟增量中位数约 0.99 秒，实际 HTTP 为单批的 5.5 倍。扫描 24 条净数量仍为 7，但具体是新增 1 组、因扫描批 503 丢失原先的 1 组，不能解释成 24 条没有额外覆盖价值。扫描 36 条比 12 条多选回 2 组，HTTP 升到每题 13 次，观察到 30/260 次 503，并丢了一个对照正例；目前不建议直接全局开到 36。生产配置仍未更改。", "",
              "对照退步 physelite-2824：两体衰变方法记忆单批 p≈0.342、q≈0.733；扫描 36 后仍拿到 p≈0.330，却没有 q。它进入扫描后的 top6，但第二阶段的联合比较又被 fit_pool 从 6 条裁到 4 条，排在第 5 的它被移出比较；低 p 的记忆因此失去原先高 q 的入选通道。这次退步不能仅归因于同一试次的 503。", "",
              "这个发现支持一个后续的小修候选：保留原单批的比较候选/主选证据，核验第二阶段实际比较覆盖，避免新增候选把原已能选中的低 p、高 q 条目挤走。本轮只定位和记录，没有实现该规则，也没有更改选择阈值。", "",
              "服务报告的 input_tokens 合计（非费用估算）：single 328076，scan12 527518，scan24 696847，scan36 798250。大致为 1 / 1.61 / 2.12 / 2.43 倍，不能把 HTTP 次数倍数直接当作费用倍数；失败请求是否计费未作推断。", "",
              "## 解释边界", "",
              "这里只验证已有相关正例能否重新获得判定并最终注入，没有重跑解题，因此不能称作‘救回了多少题’。未标记的新注入不能直接算错；本次不能估计完整精确率。对照题只检查原已注入正例是否保留，数量不足以证明整体没有退步。", "",
              "样本按排名分层，数学及池外项占比与全部 95 组不同，不应把样本成功比例直接乘以 95。全部 95 组目前能够确定的是可见性上限；其余实际选择效果需扩大验证。", "",
              "## 逐条结果", "", "| 题目 / 记忆 | 原池排名 | single | scan12 | scan24 | scan36 |", "|---|---:|---|---|---|---|"]
    state_names={"injected":"已注入","not_in_scan":"范围外","scored_not_selected":"有分未选",
                 "scan_batch_failed_or_unparsed":"扫描无分","judge_failed_or_timed_out":"判定失败","unmeasured":"未测"}
    for row in details:
        cells=[]
        for arm in ARMS:
            r=row["arms"][arm]
            p=r.get("p")
            cells.append(state_names[r["state"]]+(f" p={p:.3f}" if p is not None else ""))
        lines.append(f"| {row['id']} / {row['memory_id'][:8]} | {row['original_raw_pool_rank'] or '池外'} | "+" | ".join(cells)+" |")
    lines += ["", "`plan.json` 固定样本与方案；`replay-check.json` 保存旧请求/注入的零网络一致性检查；`trials/` 保存每次真实 HTTP 输入、响应、耗时与状态；`pair-analysis.jsonl` 保存完整规则与逐条 p/q；`execution.json` 保存模型、代码和端点身份哈希。离线重算：", "",
              "```bash", "rtk .venv-eval/bin/python -m tests.eval.diagnostic.two_stage_report", "```", ""]
    (directory/"report.md").write_text("\n".join(lines))


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--directory",type=Path,default=DEFAULT_OUTPUT)
    args=p.parse_args()
    result=analyze(args.directory)
    print(json.dumps({k:v for k,v in result.items() if k not in ("all_95_visibility_upper_bounds",)},ensure_ascii=False,indent=2))
