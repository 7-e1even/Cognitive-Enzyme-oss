"""Offline case audit and isolated reproductions; no application/client imports."""
from __future__ import annotations

import argparse
import ast
import copy
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tests.eval.offline import diagnose as d
from tests.eval.offline.records import DATA, ROOT, digest, read_rows

REVIEW = Path(__file__).parent / "annotations/case_review.v2.json"
OUTPUT = d.DEFAULT_OUTPUT / "forensics"


def compile_nodes(nodes: list, namespace: dict, filename: str) -> None:
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              *copy.deepcopy(nodes)], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), filename, "exec"), namespace)


def immediate(coro):
    """Only mocked coroutines that finish without I/O are allowed here."""
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    finally:
        coro.close()
    raise RuntimeError("Offline reproduction unexpectedly suspended for I/O")


def reproduce_feedback_cache(root: Path = ROOT) -> dict:
    """Execute the real match/cache/report methods with mocked pipeline/storage."""
    path = root / "app/core/engine.py"
    tree = ast.parse(path.read_text())
    engine = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Engine")
    names = {"match", "_remember_contexts", "recent_context", "_remember_applicability",
             "recent_applicability", "last_decision", "report_batch", "record_outcome", "_persist_decision"}
    methods = [n for n in engine.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    outcomes, updates, decisions = [], [], []

    async def pipeline(**kw):
        probability = {"question A": .2, "question B": .9}[kw["context"]]
        return [SimpleNamespace(enzyme=SimpleNamespace(id="same-memory"), applicability=probability)]

    async def insert_decision(record):
        decisions.append(record)

    async def insert_outcome(eid, outcome, source, **kw):
        outcomes.append({"memory_id": eid, "outcome": outcome, "source": source, **kw})

    async def hebbian(self, eid, success, **kw):
        observed = {"memory_id": eid, "success": success, "context": self.recent_context(eid),
                    "applicability": self.recent_applicability(eid)}
        updates.append(observed)
        return observed

    ids = iter(("decision-A", "decision-B"))
    namespace = {"build_search_query": lambda context, features: context, "decision_log_on": lambda: True,
                 "run_pipeline": pipeline, "time": SimpleNamespace(monotonic=lambda: 100.0),
                 "uuid": SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=next(ids))),
                 "_APPLICABILITY_MAX": 5000, "_APPLICABILITY_TTL": 3600, "_CONTEXT_TTL": 21600,
                 "context_hash": d.context_hash, "shadow_judges": lambda source: [],
                 "build_record": lambda did, *a, **kw: {"id": did, "policy": {}, "request": {"questions": {}}}}
    cls = ast.ClassDef(name="ProbeEngine", bases=[], keywords=[], body=methods, decorator_list=[])
    compile_nodes([cls], namespace, str(path))
    probe_class = namespace["ProbeEngine"]
    probe_class.hebbian_update = hebbian
    probe_class._background = lambda self, coro: immediate(coro)
    probe = probe_class()
    probe._contexts, probe._applicability, probe._last_decision = {}, {}, {}
    probe.pg = SimpleNamespace(insert_decision=insert_decision, insert_outcome=insert_outcome)
    probe.qdrant = None
    probe.config = SimpleNamespace(embedding=None, typesafe=None, llm=None, expand_llm=None, judge_llm=None)
    immediate(probe.match("question A"))
    immediate(probe.match("question B"))
    immediate(probe.report_batch(["same-memory"], False, detail="question A failed"))
    return {"api_calls": 0, "source_sha256": digest(path.read_bytes()),
            "scope": "Actual match/cache/report methods; pipeline, Hebbian persistence and DB mocked",
            "expected": {"decision_id": "decision-A", "context": "question A", "applicability": .2},
            "observed": {"decision_id": outcomes[0]["decision_id"], "context": updates[0]["context"],
                         "applicability": updates[0]["applicability"]},
            "cross_question_feedback_reproduced": outcomes[0]["decision_id"] == "decision-B"
            and updates[0]["context"] == "question B" and updates[0]["applicability"] == .9}


def reproduce_unverified_update(root: Path = ROOT) -> dict:
    path = root / "app/learning/capture.py"
    tree = ast.parse(path.read_text())
    selected = [n for n in tree.body if (
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in {"_merge_then", "_apply"}) or (
        isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in {"_PATCH_NOTE", "_PATCH_WORDS"} for t in n.targets))]
    memory = SimpleNamespace(id="same-memory", then="圆轨道速率与半径无关。", because="", tags=[],
                             confidence=.4, version=1, updated_at=None)
    calls = Counter()

    async def get_memory(eid):
        return memory

    async def save_memory(value):
        calls["save"] += 1

    async def log_event(value):
        pass

    async def harvest(**kw):
        calls["harvest"] += 1
        raise RuntimeError("Unexpected harvest in isolated UPDATE reproduction")

    async def learn(*a, **kw):
        pass

    namespace = {"re": re, "datetime": datetime, "timezone": timezone, "scrub_secrets": lambda text: text,
                 "settings": SimpleNamespace(get=lambda name: 1200), "_learn_from_session": learn,
                 "EventType": SimpleNamespace(CONSOLIDATION="consolidation"),
                 "EvolutionEvent": lambda **kw: kw}
    compile_nodes(selected, namespace, str(path))
    engine = SimpleNamespace(get_enzyme=get_memory, update_enzyme=save_memory, harvest=harvest,
                             pg=SimpleNamespace(log_event=log_event))
    bad_claim = "圆轨道速率与半径无关，所以任意半径的圆周期相同。"
    immediate(namespace["_apply"](engine, {"then": bad_claim},
                                  {"action": "UPDATE", "target_id": memory.id, "then": bad_claim}, defaultdict(int)))
    return {"api_calls": 0, "source_sha256": digest(path.read_bytes()),
            "scope": "Actual UPDATE/merge methods; persistence, secret scrub and exemplar side effects mocked",
            "known_false_claim": bad_claim, "counterexample": "T=2πr/v; fixed v implies T(2r)=2T(r)",
            "stored": memory.then == bad_claim, "confidence_before": .4, "confidence_after": memory.confidence,
            "harvest_quality_gate_calls": calls["harvest"], "save_calls": calls["save"]}


def decision_profile(decisions: list[dict], problems: dict, root: Path = ROOT) -> dict:
    path = root / "app/matching/selection.py"
    fn = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.FunctionDef) and n.name == "select")
    namespace = {}
    compile_nodes([fn], namespace, str(path))
    nonempty = [x for x in decisions if x["candidates"]]
    sizes, body_sizes = Counter(), Counter()
    p_bad = q_bad = missing = exact = analog = 0
    differences, truncated = [], []
    by_hash = {d.context_hash(p["problem"]): p for p in problems.values()}
    for row in nonempty:
        candidates, request = row["candidates"], row["request"]
        sizes[len(candidates)] += 1
        analog += "same" in request["questions"]
        problem = by_hash.get(row["context_hash"])
        if problem and request["state"] != problem["problem"] and problem["problem"].startswith(request["state"]):
            truncated.append({"question_id": problem["id"], "shown_chars": len(request["state"]),
                              "full_chars": len(problem["problem"]), "decision_id": row["id"]})
        for c in candidates:
            body_sizes[len(d.rule_body(row, c))] += 1
        if any(c.get("p") is None or c.get("q") is None for c in candidates):
            missing += 1
            continue
        answers, probabilities = row["answers"], row["answers"]["best"]["probabilities"]
        for c in candidates:
            p_bad += abs(c["p"] - answers[c["key"]]["noul"]) > 1e-8
            q_bad += abs(c["q"] - probabilities[c["key"]]) > 1e-8
        replay = namespace["select"]({c["id"]: c["p"] for c in candidates}, {c["id"]: c["q"] for c in candidates},
                                      probabilities["none"], {c["id"] for c in candidates if c["rescued"]},
                                      t=row["policy"]["rule"], limit=6)
        recorded = [r["id"] for r in row["injected"]]
        if replay == recorded:
            exact += 1
        else:
            same = answers.get("same", {}).get("probabilities", {})
            analogy_ids = {r["id"] for r in row["injected"] if r["reason"] == "analogy"}
            gate_ok = bool(analogy_ids) and "none" in same and all(
                c.get("analog_p") is not None and c["analog_p"] >= row["policy"]["rule"]["analogy_p"]
                and c.get("analog_q") is not None and c["analog_q"] >= row["policy"]["rule"]["analogy_q"]
                and c["analog_q"] > same["none"] for c in candidates if c["id"] in analogy_ids)
            differences.append({"decision_id": row["id"], "core_replay": replay, "recorded": row["injected"],
                                "analogy_requested": "same" in request["questions"], "analogy_gate_satisfied": gate_ok})
    return {"decisions": len(decisions), "nonempty_pools": len(nonempty), "pool_size_counts": dict(sizes),
            "candidate_records": sum(len(x["candidates"]) for x in nonempty),
            "p_raw_mapping_mismatches": p_bad, "q_raw_mapping_mismatches": q_bad,
            "missing_score_decisions": missing, "core_selection_exact_matches": exact,
            "noncore_differences": differences, "analogy_requested": analog,
            "analogy_policy_on": sum(x["policy"].get("analogy") == "on" for x in nonempty),
            "delivered_body_length_counts": dict(body_sizes), "truncated_questions": truncated,
            "selection_code_sha256": digest(path.read_bytes())}


def feedback_profile(sources: Path) -> dict:
    result = {}
    for path in sorted(sources.glob("*/decision_outcomes.jsonl")):
        reports = [x for x in read_rows(path) if x["source"].startswith("report_batch")]
        grouped = defaultdict(list)
        for row in reports:
            grouped[row["decision_id"], row["enzyme_id"]].append(row)
        repeated = [(key, rows) for key, rows in grouped.items() if key[0] and len(rows) > 1]
        result[path.parent.name] = {"report_rows": len(reports), "reused_decision_memory_keys": len(repeated),
                                   "keys_with_both_success_failure": sum(len({r["outcome"] for r in rows}) > 1 for _, rows in repeated)}
    return result


def build(record: Path, sources: Path, annotations: Path, review_path: Path) -> tuple[dict, list[dict]]:
    summary, ledger, pairs = d.diagnose(record, sources, annotations)
    problems = {p["id"]: p for p in json.loads((record / "hardbench/problems.json").read_text())}
    review = json.loads(review_path.read_text())
    decisions = {x["id"]: x for x in read_rows(sources / "ce_rsi_phys_self_v3/decision_log.jsonl")}
    cards = []
    for case in review["cases"]:
        decision = decisions[case["decision_id"]]
        candidate = next(c for c in decision["candidates"] if c["id"] == case["memory_id"])
        problem = problems[case["question_id"]]
        if (digest(problem["problem"].encode()) != case["problem_sha256"]
                or d.rule_digest(decision, candidate) != case["delivered_rule_sha256"]
                or d.context_hash(problem["problem"]) != decision["context_hash"]):
            raise ValueError(f"复审证据变化：{case['question_id']}")
        cards.append({"review": case, "problem": problem, "decision": decision,
                      "target": candidate, "score_facts": d.score_facts(decision, candidate)})
    math_decisions = {x["id"]: x for x in read_rows(sources / "ce_rsi_math_self/decision_log.jsonl")}
    math_reports = read_rows(sources / "ce_rsi_math_self/decision_outcomes.jsonl")
    did = "00c96d2245474f87ba7397ac59d6d120"
    memory_id = "61894955-8197-465d-b92b-bc350d57dc2b"
    history = read_rows(record / "hardbench/math/history/math.self.jsonl")
    witness_rows = [r for r in history if r["pos"] in (15, 16, 17)]
    feedback_witness = {"decision": math_decisions[did], "rows": witness_rows,
                        "problems": [{"id": r["id"], "problem": problems[r["id"]]["problem"]} for r in witness_rows],
                        "priming_decisions": [{"question_id": r["id"], "decision_id": rec["id"], "created_at": rec["created_at"]}
                                              for r in witness_rows for rec in math_decisions.values()
                                              if rec["context_hash"] == d.context_hash(problems[r["id"]]["problem"])
                                              and [i["id"] for i in rec["injected"]] == r["injected"]],
                        "outcomes": [r for r in math_reports if r["decision_id"] == did and r["enzyme_id"] == memory_id
                                     and r["source"].startswith("report_batch")]}
    phy_events = read_rows(sources / "ce_rsi_phys_self_v3/evolution_log.jsonl")
    faulty_mid = "81a53ec1-aa1c-4b92-bdaa-ba7c92462be0"
    proof_events = [r for r in phy_events if r["enzyme_id"] == faulty_mid and r["event_type"] in {"harvest", "consolidation", "hebbian"}]
    faulty_case = next(c for c in cards if c["review"]["question_id"] == "physelite-9959")
    proof_outcomes = [r for r in read_rows(sources / "ce_rsi_phys_self_v3/decision_outcomes.jsonl")
                      if r["decision_id"] == faulty_case["decision"]["id"] and r["enzyme_id"] == faulty_mid]
    return {"schema": "offline-root-cause-audit-v1", "api_calls": 0,
            "annotation_sha256": digest(annotations.read_bytes()), "case_review_sha256": digest(review_path.read_bytes()),
            "record_manifest_sha256": summary["record_manifest_sha256"], "source_manifest_sha256": summary["source_manifest_sha256"],
            "revised_summary": summary["groups"], "physics_mechanics": decision_profile(list(decisions.values()), problems),
            "feedback_profiles": feedback_profile(sources), "feedback_cache_reproduction": reproduce_feedback_cache(),
            "unverified_update_reproduction": reproduce_unverified_update(), "math_feedback_witness": feedback_witness,
            "faulty_memory_evolution_witness": proof_events, "faulty_memory_feedback_witness": proof_outcomes}, cards


def render(findings: dict, cards: list[dict]) -> str:
    p = findings["physics_mechanics"]
    lines = ["# 数学物理记忆链路复审：根因与证据", "", "2026-10-16；只用已保存记录与本地代码，API 调用 0。未修改线上代码、参数或数据库。", "",
             "最明确的新问题是反馈绑错题：记忆的判定 ID、情境和适用分被后一道题覆盖。"
             "另外存在复合题选取规则与内容更新验证的问题。现有记录仍不足以计算首段召回失败占比。", "",
             "## 首先纠正上轮归因", "",
             "原先6个适用记忆漏选线索，完整核对触发条件和实际子问后：3个保留为明确适用条目未选，"
             "2个只属方法提示/需要类比，1个依赖题面未给出的薄层近似。不再把两例低 p 直接叫作 Jev 判错。"
             "8次内容风险也不等同于8次 Jev 误判：相关性判断并非逐句事实验真。v1 原报告保留，v2 修订另存。", "",
             "## 1. 已复现：反馈会串到另一道题", "",
             "Engine 按记忆 UUID 保存最近一次 decision/context/p。RSI 先 prime 一批题，再收答案逐题 report；"
             "同一记忆在 A、B 两题命中后，反馈 A 时缓存已被 B 覆盖。实际执行当前 match/cache/report 方法的隔离复现：", "",
             "| 反馈 A 应读取 | 预期 | 实际 |", "|---|---|---|", "| 判定 ID | decision-A | decision-B |",
             "| 题面 | question A | question B |", "| 适用性 p | 0.2 | 0.9 |", "",
             "复现中模型、数据库和 Hebbian 持久化均为本地桩；执行的是实际缓存与 report 方法，无 API。"
             "错误绑定会影响结果记录；实际 Hebbian 路径还用这些缓存给失败加权、学习正反例，因此不能只当日志显示问题。", "",
             "历史具体证据：数学 pos15 `beyondaime-066` 对、pos16 `imo-bench-number_theory-022` 错、"
             "pos17 `imo-bench-algebra-047` 对，均使用同一记忆。三个 success/failure/success 全回填到 pos17 的判定"
             " `00c96d2245474f87ba7397ac59d6d120`，对应 outcome ID 30、33、34。", "",
             "| 实验库 | report 行数 | 复用同一判定+记忆的键数 | 同键同时出现成功/失败 |", "|---|---:|---:|---:|"]
    for bank, stats in findings["feedback_profiles"].items():
        lines.append(f"| {bank} | {stats['report_rows']} | {stats['reused_decision_memory_keys']} | {stats['keys_with_both_success_failure']} |")
    lines += ["", "重复键是范围排查线索，不当作全部误归因题数；上述三题已结合顺序、题面、注入和日志逐项确认。"
              "物理 self_v3 没看到此类重复，不能据此把物理所有问题也归给串题。", "",
              f"代码位置：[按记忆保存最后判定]({ROOT}/app/core/engine.py:170)、"
              f"[按记忆覆盖情境]({ROOT}/app/core/engine.py:238)、"
              f"[反馈读取最后判定]({ROOT}/app/core/engine.py:574)、"
              f"[先批量 prime 的调用方]({ROOT}/tests/eval/hardbench/rsi.py:477)。", "",
              "## 2. 已确认：全题单选概率压掉另一小问的有效条目", "",
              "q 问的是“哪条最有用”，不表示其他记忆没用。选取代码又用 q 竞争结果决定多条是否保留。"
              "physelite-416 的落地判断服务第(3)问，p=0.9677，却因 q=0.0539 < 0.1×0.8717 被补充门挡掉，"
              "最终只选了碰撞方法。4427、8427 的干涉/半波片分别服务另一小问，p≈0.72/0.70，卡在补充 p≥0.85。"
              "这些是适用条目被规则筛掉的证据；尚未证明放行后解题正确率会改善。", "",
              "| 原漏选线索 | 复审类别 | 结论 |", "|---|---|---|"]
    for card in cards:
        c = card["review"]
        if c["kind"] == "unselected":
            lines.append(f"| {c['question_id']} | {c['classification']} | {c['reason']} |")
    lines += ["", "## 3. Jev 的条件识别有个案错误，内容风险不能全部算给 Jev", "",
              "5808 明确问 t<t₀ 的滑动阶段，却选中“纯滚动时切向项抵消”的模板（p=0.857）；"
              "2726 是转动刚杆，却选中质点模板。后者 p 只有0.376，q=0.765，仍满足当前主选的低 p 门。"
              "这里既有场景条件识别不严，也有选择策略接受较弱绝对适用性的问题。", "",
              "| 已注入线索 | 复审分类 | 判断边界 |", "|---|---|---|"]
    for card in cards:
        c = card["review"]
        if c["kind"] == "injected":
            lines.append(f"| {c['question_id']} | {c['classification']} | {c['reason']} |")
    lines += ["", "## 4. 已复现：UPDATE 不经事实验证即可替换正文并加置信度", "",
              "当前 capture._apply 的已有记忆 UPDATE 分支没有再次经过 harvest 质量门，也没有外部验证。"
              "隔离执行该实际方法，把“速率与半径无关，所以周期相同”的错误推论作为 UPDATE，正文被接受，"
              "置信度从0.40变0.45。T=2πr/v 可直接反驳该推论。", "",
              "历史 P151 初始 harvest 没有“任意半径周期相同”，后续 UPDATE 后判断输入已包含此错句；"
              "9959 只问半圆轨道所需速度，整题被标为正确，随后 Hebbian event1556 仍给含错记忆 +0.10。"
              "这说明“整题答对”不能验证一条记忆里所有推广结论。旧记录缺完整更新前后正文，无法精确指定哪一次 UPDATE 引入错句。", "",
              "当前已增加 UPDATE 长度上限、补丁归并与 prev_then 留档；它们缓解旧版的长度/覆盖问题，"
              "但不等于给 UPDATE 增加了事实验证。本次没有重测当前版本的解题收益。", "",
              "## 5. 实际判断输入与名义配置有差距", "",
              f"- 物理 {p['nonempty_pools']}/{p['decisions']} 份记录有非空判断池，实际候选数分布 `{p['pool_size_counts']}`，中位数4。",
              f"- {p['analogy_policy_on']} 份 policy 标记 analogy=on，只有 {p['analogy_requested']} 份实际发出类比题；不能把开关开启当作类比已执行。",
              f"- {len(p['truncated_questions'])} 道题的实际 state 是完整题面的截短前缀；"
              f"{p['delivered_body_length_counts'].get(400,0)}/{p['candidate_records']} 份建议达到400字上限。",
              "- fit_pool 按 token 预算先裁候选，类比再用剩余预算；这能解释上述现象，但旧 policy 未保存有效预算和裁剪前池，不能唯一归因每个池外缺席。", "",
              "长规则的触发、适用条件、具体结论可能不在同一前缀；完整题目的后续小问也可能被 state 截断。"
              "此类输入缺失不能简单当成模型不理解，也不能从判断池缺席倒推首段没召回。", "",
              "## 机械检查与结论边界", "",
              f"- 核对 {p['candidate_records']} 条候选与原始 answers：p 映射不一致 {p['p_raw_mapping_mismatches']}，q 映射不一致 {p['q_raw_mapping_mismatches']}。",
              f"- 当前纯选取函数配合当时门槛，可精确还原 {p['core_selection_exact_matches']}/{p['nonempty_pools']} 次结果；"
              "剩余1次记录原因为 analogy，属于纯函数没有模拟的独立通道；未发现概率解析或字段串号的证据。",
              "- 首段 recall/rerank 全量池没有留存，仍不能给出总体召回损失与 Jev 损失的比例。",
              "- 先处理反馈绑定与错误内容的无验证增补，再用修订后的标签评估 Jev/选择规则；不要用当前有串题风险的 outcome 直接训练或选优。", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, default=DATA / "2026-10-15")
    parser.add_argument("--sources", type=Path, default=d.DEFAULT_SOURCES)
    parser.add_argument("--annotations", type=Path, default=d.ANNOTATIONS)
    parser.add_argument("--review", type=Path, default=REVIEW)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    findings, cards = build(args.record, args.sources, args.annotations, args.review)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "findings.json").write_text(json.dumps(findings, ensure_ascii=False, indent=2)+"\n")
    (args.output / "case_cards.jsonl").write_text("".join(json.dumps(c, ensure_ascii=False)+"\n" for c in cards))
    (args.output / "report.md").write_text(render(findings, cards))
    code = args.output / "method"
    code.mkdir(exist_ok=True)
    for name, path in (("forensics.py", Path(__file__)), ("case_review.json", args.review),
                       ("engine.py", ROOT / "app/core/engine.py"), ("capture.py", ROOT / "app/learning/capture.py"),
                       ("selection.py", ROOT / "app/matching/selection.py"), ("typesafe_client.py", ROOT / "app/typesafe_client.py"),
                       ("rsi.py", ROOT / "tests/eval/hardbench/rsi.py")):
        (code / name).write_bytes(path.read_bytes())
    files = [p for p in args.output.rglob("*") if p.is_file() and p.name != "manifest.json"]
    (args.output / "manifest.json").write_text(json.dumps({"api_calls": 0, "files": {
        str(p.relative_to(args.output)): {"sha256": digest(p.read_bytes())} for p in files}}, indent=2)+"\n")
    print(json.dumps({"api_calls": 0, "reviewed_cases": len(cards), "feedback_cache_reproduced":
                      findings["feedback_cache_reproduction"]["cross_question_feedback_reproduced"],
                      "unverified_update_stored": findings["unverified_update_reproduction"]["stored"],
                      "core_replay_exact": findings["physics_mechanics"]["core_selection_exact_matches"]}, indent=2))


if __name__ == "__main__":
    main()
