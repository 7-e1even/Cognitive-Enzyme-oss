"""Build the evidence report from saved records only. Makes no API calls."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from tests.eval.diagnostic.analyze import analyze
from tests.eval.diagnostic.assemble import assemble
from tests.eval.diagnostic.benefit import job_arms
from tests.eval.diagnostic.protocol import DEFAULT, canonical, dump, rows, sha


def response_state(response):
    if response.get("input_delivery_verified") is False or response.get("harness_error"):
        return "harness_fault"
    if response.get("ok"):
        return "completed"
    if response.get("timed_out"):
        return "time_budget"
    if "rate limit" in str(response.get("error", "")).lower():
        return "rate_limit"
    return "other_service_or_protocol"


def report(directory):
    directory = directory.resolve()
    assembled = assemble(directory)
    detail = analyze(directory)
    tasks = json.loads((directory / "inputs/tasks.json").read_text())
    recs = {r["id"]: r for r in rows(directory / "retrieval.jsonl")}
    cases = rows(directory / "assembled-cases.jsonl")
    raw = [json.loads(p.read_text()) for p in (directory / "api/solver").glob("*.json")]
    solve = [r for r in raw if r["key"].startswith("solve/")]
    grades = [r for r in raw if r["key"].startswith("grade/")]
    expected = Counter(a for t in tasks if t["solve_eligible"] for a in job_arms(t, recs[t["id"]]))
    per_arm = {arm: Counter(response_state(r["response"]) for r in solve if r["key"].endswith("/" + arm))
               for arm in ("none1", "none2", "system", "gold")}
    completed = [r["response"] for r in solve if r["response"].get("ok")]
    states = Counter(response_state(r["response"]) for r in solve)
    quality = Counter(r["completeness"] for r in rows(directory / "question-quality.jsonl"))
    solution_flags = sum(r.get("solution_in_question", False) for r in rows(directory / "question-quality.jsonl"))
    timeout_audit_path = directory / "timeout-session-audit.json"
    timeout_sessions = json.loads(timeout_audit_path.read_text()) if timeout_audit_path.exists() else []
    need = {}
    for group in ("math", "physics"):
        cohort = [r for r in cases if r["group"] == group and r["strict_input_subset"]]
        golden = [r for r in cohort if r["gold_ids"]]
        stable = [r for r in golden if r["bare_status"] == "both_correct"]
        actual_finished_fail = [r for r in golden if r["bare_status"] == "both_failed"
                                and not r["none1_budget_exhausted"] and not r["none2_budget_exhausted"]]
        need[group] = {"audit_screened_questions": len(cohort), "gold_questions": len(golden),
                       "two_bare_correct": len(stable),
                       "two_completed_bare_incorrect": len(actual_finished_fail),
                       "at_least_one_bare_budget_failure": sum(r["none1_budget_exhausted"] or r["none2_budget_exhausted"] for r in golden),
                       "complete_gold_triples": sum(all(r[a] is not None for a in ("none1", "none2", "gold")) for r in golden),
                       "stable_injected": sum(bool(r["system_ids"]) for r in stable),
                       "gold_rescued_both_failed": sum(r["need_class"] == "gold_rescued_both_wrong" for r in golden)}
    gate = gate_audit(cases)
    audit = {"solver_planned": sum(expected.values()), "solver_attempts_cached": len(solve),
             "solver_states": dict(states), "solver_by_arm": {a: {"planned": expected[a], **dict(c)} for a,c in per_arm.items()},
             "grade_states": dict(Counter(response_state(r["response"]) for r in grades)),
             "successful_solver_exact_inputs": sum(r.get("input_delivery_verified") is True for r in completed),
             "successful_solver_zero_cost": sum(r.get("cost") == 0 for r in completed),
             "quality_flags": dict(quality), "quality_note": "blind model flags include pilot input-audit reuse; agent follow-up is incomplete, not authoritative clean ground truth",
             "model_need": need, "confidence_gate": gate,
             "completion": {"retrieval": "complete", "model_need": "incomplete_provider_rate_limit"},
             "unattempted_solves": sum(expected.values()) - len(solve)}
    dump(directory / "campaign-audit.json", audit)
    lines = ["# 数学、物理记忆诊断：完整检索证据与未完成的模型需求对照", "",
             "检索阶段已完成；模型需求对照因免费模型持续限流而未完成。不能把未完成请求当作错误，也不能把下表的相关记忆漏项直接等同于‘模型需要却没拿到的提示’。本轮只改评测与记录，没有改生产架构。", "",
             "## 1. 统计口径", "",
             "89 道数学题、372 道物理题，共 461 题；事先排除 18 道已知问题题目的解题对照，剩 443 题。成熟冻结库含 152 条数学、306 条物理记忆。逐题排除本题/近重复题曾 ADD 或 UPDATE 的记忆及相近经验样本，剩 114 题的 146 个部分正例标注。未标记不等于负例；无法计算全库精确率或完整召回率。", "",
             "新数据来自实际匹配代码和独立测试索引，不是原学习过程逐时刻重放。关系快照和领域描述缺失；额外大模型判定、回退、扩写、随机探索关闭，参数差异均记入 manifest。生产 PostgreSQL 配置只读，未启动已停止的主应用。", "",
             "## 2. 正例在哪一层丢失", "", "| 最终位置 | 数学 | 物理 | 合计 |", "|---|---:|---:|---:|"]
    labels = [("融合召回未覆盖", "fused_recall_miss"), ("合并候选上限丢失", "recall_merge_or_route_drop"),
              ("排序/判定池上限", "rank_or_judge_pool_cut"), ("Jev 请求预算裁剪", "judge_budget_cut"),
              ("Jev 分数/选择规则未放行", "judge_or_selection_rejected"), ("Jev 服务失败", "selector_service_failure"),
              ("已注入", "injected")]
    for label,key in labels:
        a,b = [detail["layers"][g]["stages"].get(key,0) for g in ("math","physics")]
        lines.append(f"| {label} | {a} | {b} | {a+b} |")
    lines += ["", "146 个正例中，114 个进入融合召回，95 个在实际判定前被挡住（78 个请求预算、17 个排序/池上限）。最大的可定位损失发生在‘已召回 → Jev 实际看到候选’之间，不能将这 95 个归为 Jev 语义错判。", "",
              "纯向量精确 top50 覆盖 89/146；57 个不在 top50，其中 30 个被词法/经验样本等融合路径补回，另有 5 个原已在向量结果中的正例被合并后的 60 条上限挤掉。其余 27 个融合未召回。未发现近似索引遗漏或索引缺项，向量相关性排名仍有改进空间。", "",
              "名义判定池为 36，但实际中位数只有 4 条（3 条 109 题、4 条 294 题、5 条 50 题、6 条 5 题、0 条 3 题）。fit_pool 按预算只保留前缀；逐个调低 p 阈值无法救回没有被送去打分的候选。", "",
              "461 题融合候选可离线精确复现，0 次不一致；1783 个候选的原始 p/q 与候选映射核验 0 次不一致。当前没有找到 p/q 对错条目的证据。两条实际低 p 未放行记忆复核后分别是‘已需先构造镜像电荷链，规则只教通项求和’和‘核反应配平只辅助复合题的一部分’，不能直接记为 2 次确定的 Jev 错判。", "",
              "33 题题面超过 1500 字符，1213 次候选规则正文超过 400 字符。146 个完整题面/完整规则单条影子请求均成功，但它们同时改变了候选竞争与提示内容；p 的差异不能全部归因于截断。", "",
              "## 3. 模型是否真的需要提示", "",
              "使用同一 DeepSeek V4.1 Flash Free、low、16384 输出 token、180 秒：每题裸答一次，114 道有正例的题裸答两次；另测系统记忆、标注记忆。无系统注入时直接复用裸答；标注组是已标相关规则，不是官方答案，也不保证能帮助模型。", "",
              f"计划 {sum(expected.values())} 次解题请求，实际缓存 {len(solve)} 次：完成 {states['completed']}、推理超时 {states['time_budget']}、限流 {states['rate_limit']}、其他服务错误 {states['other_service_or_protocol']}；另有 {audit['unattempted_solves']} 次未调用。完成响应中 {audit['successful_solver_exact_inputs']} 次核验实际单份输入，{audit['successful_solver_zero_cost']} 次会话 cost=0，实际模型亦由 runner 核验。另已匹配核查 {len(timeout_sessions)} 个超时会话，全部有持续推理、无工具调用；它们仍只能说明在本预算下未完成。", "",
              "| 审核筛选后的有正例题 | 数学 | 物理 |", "|---|---:|---:|",
              f"| 裸答两次都对 | {need['math']['two_bare_correct']} | {need['physics']['two_bare_correct']} |",
              f"| 裸答两次+标注提示均有可判结果 | {need['math']['complete_gold_triples']} | {need['physics']['complete_gold_triples']} |",
              f"| 裸答均失败、标注提示救回 | {need['math']['gold_rescued_both_failed']} | {need['physics']['gold_rescued_both_failed']} |", "",
              "物理题的提示组尚没有完整可判对照，因此上表的 0 不是‘提示无效’。数学样本很小，其中部分失败仅是超时；不能据此估计全量需要提示的比例或确定线上置信度阈值。", "",
              f"审核筛选后，共 {sum(v['two_bare_correct'] for v in need.values())} 题两次裸答均对，其中 {sum(v['stable_injected'] for v in need.values())} 题系统仍选择注入记忆。这是已观察到的冗余候选；不意味着两次做对就证明永久掌握，也不能在提示组缺失时估计注入伤害。", "",
              f"筛选来自盲审模型标记及本助手逐题复核，尚未全部复核，不等于权威干净数据集。盲审标记 usable {quality['usable']}、incomplete {quality['incomplete']}、uncertain {quality['uncertain']}（其中 {solution_flags} 题题面带答案/分析）。usable 中也发现实验题误判：只有实验流程，没有读数，不能让闭卷模型生成原实验数值；此类已加排除覆盖。", "",
              "参考答案的具体争议：physelite-4845 的速度偏转角由给定参数得到约 0.090 rad，参考 0.046 rad 接近位移偏角；physelite-7131 临界张力公式在 cosθ=1/3 触零，对应 70.53°，参考后一问却给 48.2°；physelite-8676 的 1.42e7 m/s 与参考两位有效数字 1.4e7 相容，却可能被固定 1% 窗口拒绝。保留原题、原参考、原判分，单独标记争议，不把它们用于知识不足结论。", "",
              "## 4. 已修正的评测问题", "",
              "OpenCode 当前命令行参数入口会重复整段提示。正式组改成 stdin，并核验持久化用户消息恰好为一份原文；早先参数重复的模型性能数据保留为 pilot，全部排除出正式模型需求结论。复用的仅有模型无关检索数据和明确标记来源的盲审输入标记。", "",
              "限流错误没有命中原停止条件，导致短时间内留下大量失败请求；已增加跨评测进程的 service-block 停止标记，401/403/429、rate-limit/quota 均停止新调用。传输错误和判分失败保持未知；输入投递错误属于评测故障。历史缓存不覆盖，成功及预算耗尽结果无需重跑，续跑目录只准备缺项。冷却后的一次服务检查仍限流，未继续重试。", "",
              "## 5. 根据现有证据排优先级", "",
              "1. 先做候选可见性小实验：直接使用已有 judge.two_stage 分批判定路径，固定模型和选择阈值，对 12/24/36 扫描池比较候选覆盖、延迟、请求数与实际救回。离线可见性上限分别覆盖 43/82/96 个正例，尚非真实 Jev 或解题收益。不要直接放大单请求预算或仅降阈值。", "2. 再看 27 个融合漏召回与 5 个合并丢失的逐项排名，优先验证融合配额/截断，暂不替换 embedding。召回相关性与模型需求分开统计。", "3. 内容以‘适用条件 → 关键一步/必要事实 → 可执行检查’为单位，关键信息放前面。某题真正卡在镜像电荷构造时，‘先写几项再求和’太泛；仅提醒核反应配平也救不了整套热传导题。通用方法、具体事实、原题答案缓存分别标明用途；迁移评测排除原题答案。", "4. 需求门控先保留为待验证方案：模型/版本/推理预算下，两次裸答都对只说明本次可少提示；两次裸答失败且方法提示救回才是需求正例。超时、缺题面、坏参考、服务错误分开。自报 Confidence/Need_memory 仅作特征，样本补齐前不发布阈值。", "5. 现有 learning.skip_known 发生在写入时，针对抽象 trigger 问‘你会怎么做’，不能替代当前整题能否做对；PrimeRequest 当前也不含作答模型或失败步骤。先用缓存建立模型需求标签，外部验证失败后走已有 failure 触发路径，验证通过再考虑接口小扩展。", "",
              "此前发现的反馈归属错位、UPDATE 未经事实验证是学习闭环的独立问题；本轮的 146 对成熟库统计不能覆盖或否定旧学习轨迹中的问题。", "",
              "## 6. 缓存与复现", "",
              "本机 formal/ 是正式结果，父目录的模型结果是 pilot。server-evidence.tar.gz 保留服务器原始与限速重测数据，SHA256: a079d2bd3a86a14d4d2dca9b8440725b9af2a62e4de4e6a824add93a61e5cf9f。", "",
              "详细位置见 detailed-pairs.jsonl；完整逐题状态见 assembled-cases.jsonl；请求与响应见 api/；输入与执行代码身份见 inputs/manifest.json、execution-method/；逐题复核见 content-review.json、reference-checks.json；模型缺口见 ../recovery-prepared/pending-solves.json。", "",
              "以下命令只读缓存，不调用 API：", "", "```bash", "rtk .venv-eval/bin/python -m tests.eval.diagnostic.report", "rtk .venv-eval/bin/pytest -q tests/eval", "```", "",
              "本报告未完成物理提示收益、最优检索门槛或最终生产改动验证。保存了可继续收集的准确缺项，服务恢复后只补服务失败/未调用部分。", ""]
    output = directory / "report.md"
    output.write_text("\n".join(lines))
    dump(directory / "report-method.json", {"code_sha256": sha(Path(__file__).read_bytes()),
          "source_sha256": {n: sha((directory/n).read_bytes()) for n in
                             ("retrieval.jsonl", "assembled-answers.jsonl", "question-quality.jsonl", "content-review.json")},
          "api_calls": 0})
    return {"report": str(output), "audit": audit}


def gate_audit(cases):
    data = [r for r in cases if r["strict_input_subset"] and r["none1"] is not None
            and r["system"] is not None and r.get("confidence") is not None]
    def measure(cohort, threshold):
        use = lambda r: r["confidence"] < threshold
        return {"n": len(cohort), "retrieval_requests": sum(use(r) for r in cohort),
                "correct": sum(r["system"] if use(r) else r["none1"] for r in cohort),
                "none_correct": sum(r["none1"] for r in cohort),
                "rescues_suppressed": sum(not use(r) and r["none1"] is False and r["system"] is True for r in cohort)}
    calibration = [r for r in data if r["split"] == "calibration"]
    holdout = [r for r in data if r["split"] == "holdout"]
    options = [(t, measure(calibration,t)) for t in (0,50,60,70,80,90,95,100,101)]
    chosen = max(options, key=lambda pair:(pair[1]["correct"],-pair[1]["retrieval_requests"]))[0] if calibration else None
    return {"status": "exploratory_only_insufficient_observed_pairs", "chosen_on_calibration": chosen,
            "calibration": measure(calibration,chosen) if chosen is not None else {"n":0},
            "holdout": measure(holdout,chosen) if chosen is not None else {"n":len(holdout)},
            "policy_options": options,
            "caveat": "full closed-book draft observed; incomplete selected sample; no deployment threshold or saved-draft-cost claim"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory",type=Path,default=DEFAULT)
    args = parser.parse_args()
    print(json.dumps(report(args.directory),ensure_ascii=False,indent=2))
