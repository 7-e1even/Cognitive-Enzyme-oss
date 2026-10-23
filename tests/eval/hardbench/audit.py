"""Offline audit of saved scores, retrieval attribution and memory snapshots.

No model, database or network calls. Historical scores are only corrected for
missing grades here; replaying the updated grading rubric is a separate run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

from tests.eval.hardbench.grading import failure_kind, outcome
from tests.eval.hardbench.retest import load_primes, load_solves, run_dir
from tests.eval.hardbench.run import DATA
from tests.eval.hardbench.rsi import OUT, ROOT
from tests.eval.realbench.uplift_eval import mcnemar_p


def read_rows(path: Path) -> dict[str, dict]:
    return {r['id']: r for r in (json.loads(line) for line in path.read_text().splitlines() if line.strip())}


def paired(base: dict, arm: dict, ids: list[str]) -> dict:
    pairs = [(outcome(base.get(i)), outcome(arm.get(i))) for i in ids]
    valid = [(a, b) for a, b in pairs if a is not None and b is not None]
    n = len(valid)
    wins, losses = sum(not a and b for a, b in valid), sum(a and not b for a, b in valid)
    return {'n': n, 'expected': len(ids), 'base_correct': sum(a for a, _ in valid),
            'arm_correct': sum(b for _, b in valid), 'wins': wins, 'losses': losses,
            'delta_pp': 100 * (wins-losses) / n if n else None, 'mcnemar_p': mcnemar_p(wins, losses) if n else None}


def question_flags(p: dict) -> list[str]:
    text = p.get('problem', '').strip()
    flags = []
    if not text:
        flags.append('empty_question')
    if len(text) < 100:
        flags.append('short_question_review')
    if re.search(r'(?i)\b(?:shown|see|refer|given)\b.{0,35}\b(?:figure|diagram|fig\.)', text):
        flags.append('figure_reference_review')
    if re.search(r'(?i)\b(?:analyze|solve|answer)\s+problem\s+\d', text):
        flags.append('missing_other_problem_review')
    labels = lambda s: set(re.findall(r'(?<!\w)\(([1-9])\)', s))
    asked, reference = labels(text), labels(p.get('answer', ''))
    if asked and reference - asked:
        flags.append('extra_reference_parts_review')
    return flags


def audit(run_name: str) -> dict:
    meta_path = OUT / 'phys.meta.json'
    meta = json.loads(meta_path.read_text())
    ids = meta['order']
    problems = {p['id']: p for p in json.loads((DATA / 'problems.json').read_text())}
    flags = {i: question_flags(problems[i]) for i in ids}
    unflagged = [i for i in ids if not flags[i]]
    sources, scores = {}, {}
    base = read_rows(OUT / 'phys.base.jsonl')
    for name in ('base', 'base2', 'self_v3', 'teacher_v3'):
        path = OUT / f'phys.{name}.jsonl'
        rows = read_rows(path)
        sources[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
        counts = Counter(failure_kind(rows.get(i)) for i in ids)
        scores[name] = {'coverage': dict(counts), 'correct': sum(outcome(r) is True for r in rows.values()),
                        'pred_at_500_chars': sum(r.get('graded_by') == 'judge' and len(r.get('pred') or '') == 500
                                                for r in rows.values()),
                        'paired_vs_base': paired(base, rows, ids),
                        'unflagged_sensitivity': paired(base, rows, unflagged)}

    retrieval_path = ROOT / 'tests/eval/reports/real_api_lex.outcomes.jsonl'
    rows = [json.loads(line) for line in retrieval_path.read_text().splitlines() if line.strip()]
    stages, old_stages, by_domain = Counter(), Counter(), defaultdict(Counter)
    relabeled, acceptable_substitutes = 0, 0
    for row in rows:
        if row.get('kind') != 'positive' or row.get('error'):
            continue
        corrected = dict(row['attribution'])
        judged = {x[0] for x in row.get('jev', [])} if row.get('jev_status') == 'ok' else set()
        for memory, stage in corrected.items():
            if stage == 'rerank_dropped' and memory in judged:
                corrected[memory] = 'jev_cut'
                relabeled += 1
        old_stages.update(row['attribution'].values())
        stages.update(corrected.values())
        by_domain[row['domain']].update(corrected.values())
        acceptable_substitutes += bool(set(row.get('acceptable', [])) & set(row.get('injected', [])))
    misses = sum(n for stage, n in stages.items() if stage != 'hit')
    retrieval = {'source': str(retrieval_path.relative_to(ROOT)), 'unit': 'expected_memory',
                 'sha256': hashlib.sha256(retrieval_path.read_bytes()).hexdigest(),
                 'stages': dict(stages), 'misses': misses,
                 'legacy_stages': dict(old_stages), 'rescued_then_rejected': relabeled,
                 'acceptable_substitute_queries': acceptable_substitutes,
                 'miss_share': {s: n/misses for s, n in stages.items() if s != 'hit'},
                 'by_domain': {d: dict(c) for d, c in sorted(by_domain.items())}}

    primes = load_primes(SimpleNamespace(lib='self_v3'))
    modes = {}
    for mode in ('single', 'two_stage'):
        memories = [m for r in primes for m in r[mode]['memories']]
        modes[mode] = {'injected_questions': sum(bool(r[mode]['memories']) for r in primes),
                       'injection_events': len(memories), 'unique_memories': len({m['id'] for m in memories}),
                       'body_over_400_events': sum(len(m['then']) > 400 for m in memories),
                       'reasons': dict(Counter(m.get('reason') for m in memories)),
                       'missing_recall_trace': sum('recalled_ids' not in r[mode].get('trace', {}) for r in primes)}
    selected = [r['id'] for r in primes if r['two_stage']['memories']]

    def retest_result(directory: Path) -> dict:
        saved = load_solves(directory / 'solves.jsonl')
        arms = {a: {i: saved[a, i] for aa, i in saved if aa == a} for a in ('mem', 'ctl')}
        return {'coverage': {a: dict(Counter(failure_kind(arms[a].get(i)) for i in selected)) for a in arms},
                'paired': paired(arms['ctl'], arms['mem'], selected),
                'unflagged_sensitivity': paired(arms['ctl'], arms['mem'], [i for i in selected if not flags[i]]),
                'budget_exhausted': {a: sum(r.get('stop') in ('length', 'time_budget') for r in arms[a].values()) for a in arms}}

    snapshot = OUT / 'retest_self_v3/library.snapshot.json'
    bank = json.loads(snapshot.read_text()) if snapshot.exists() else []
    inventory = {'available': snapshot.exists(), 'memories': len(bank),
                 'body_over_400': sum(len(m['then']) > 400 for m in bank),
                 'body_over_1000': sum(len(m['then']) > 1000 for m in bank),
                 'with_references': sum(bool(m.get('references')) for m in bank),
                 'general': sum('scope:general' in m.get('tags', []) for m in bank),
                 'sha256': hashlib.sha256(snapshot.read_bytes()).hexdigest() if bank else None}
    lessons = [l for r in read_rows(OUT / 'phys.self_v3.jsonl').values() for l in r.get('lessons', [])]
    inventory['capture_decisions'] = dict(Counter(l.get('decision') for l in lessons))
    return {'protocol': 'offline-audit-v1', 'historical_scores': scores, 'sources': sources,
            'question_flags': {i: f for i, f in flags.items() if f}, 'unflagged_questions': len(unflagged),
            'retrieval': retrieval, 'memory_inventory': inventory,
            'frozen': {'eligible_questions': len(primes), 'modes': modes},
            'legacy_retest': retest_result(OUT / 'retest_self_v3'),
            'legacy_regraded': retest_result(run_dir('self_v3', 'legacy_regrade_deepseek_v3')),
            'new_run': {'name': run_name, **retest_result(run_dir('self_v3', run_name))}}


def render(data: dict) -> str:
    fmt_p = lambda x: f'{x:.4g}' if x is not None else '—'
    lines = ['# 评测审计', '',
             '历史正确率这里只修复“判分失败算错”的口径；没有按新判分提示重新判历史答案，也没有重建旧学习轨迹。', '',
             '| 历史组 | 有效判分 | 未判定 | 配对 base 正确 | 本组正确 | 变对 / 变错 | p | 500 字边界的判分输入 |',
             '|---|---|---|---|---|---|---|---|']
    for arm, stats in data['historical_scores'].items():
        p, c = stats['paired_vs_base'], stats['coverage']
        lines.append(f"| {arm} | {c.get('graded',0)} | {c.get('ungraded',0)} | {p['base_correct']}/{p['n']} | "
                     f"{p['arm_correct']}/{p['n']} | {p['wins']} / {p['losses']} | {fmt_p(p['mcnemar_p'])} | {stats['pred_at_500_chars']} |")
    r = data['retrieval']
    lines += ['', f"realbench 按正确记忆条目标注计数（修正救援分支归因）：{r['stages']}；共漏 {r['misses']} 条。",
              f"旧归因 {r['legacy_stages']} 中，{r['rescued_then_rejected']} 条虽没过精排，却已经被 Jev 救援池判断，归入选择阶段。保留原始日志。", '',
              '| 领域 | 命中 | 未召回 | Jev 淘汰 | 精排淘汰 |', '|---|---|---|---|---|']
    for domain, counts in r['by_domain'].items():
        lines.append('| ' + domain + ' | ' + ' | '.join(str(counts.get(k,0)) for k in ('hit','not_recalled','jev_cut','rerank_dropped')) + ' |')
    lines += ['', '召回与选择的这些归因来自带真实目标记忆标注的 realbench，不能直接外推到物理方法论库。', '',
              f"冻结错题池：{data['frozen']['eligible_questions']} 道。"]
    for mode, stats in data['frozen']['modes'].items():
        lines.append(f"- {mode}：注入 {stats['injected_questions']} 题、{stats['injection_events']} 次；"
                     f"缺少召回池日志 {stats['missing_recall_trace']} 题。")
    for label, stats in (('旧模型重测（历史口径）',data['legacy_retest']),
                         ('旧答案重新判分（DeepSeek；仅已保存答案）',data['legacy_regraded']),
                         (f"新模型重测（{data['new_run']['name']}）",data['new_run'])):
        p = stats['paired']
        lines += ['', f"{label}：有效配对 {p['n']}/{p['expected']}；闭卷 {p['base_correct']}，记忆 {p['arm_correct']}；"
                  f"仅记忆对 {p['wins']}、仅闭卷对 {p['losses']}，p={fmt_p(p['mcnemar_p'])}。", f"覆盖：{stats['coverage']}。"]
        q = stats['unflagged_sensitivity']
        lines += [f"题面未触发质量警报的敏感性分析：有效配对 {q['n']}/{q['expected']}；闭卷 {q['base_correct']}，"
                  f"记忆 {q['arm_correct']}；p={fmt_p(q['mcnemar_p'])}。这只是筛查子集，仍需要题面人工复核。"]
    inv = data['memory_inventory']
    lines += ['', f"库快照 {inv['memories']} 条；then 超 400 字 {inv['body_over_400']} 条，超 1000 字 {inv['body_over_1000']} 条；"
              f"带 references {inv['with_references']} 条。长度与来源缺失是审计线索，不能据此断言内容有害。", '',
              '题面质量警报只供复核：短题和提到图不自动等于坏题。例：physelite-587 题面为空；'
              'physelite-11509 要求根据三个波长求厚度，却没有给出三个波长；physelite-2680 只问第 1 小问，参考包含 7 问。', '',
              '结论边界：物理旧日志没有正确记忆 ID 标注，也缺召回池，无法逐题确定“没召回”还是“没选中”。'
              '有注入却答错也不能单凭一次输出归因于内容。新模型结果与历史 GLM 结果分开，未完成配对不计增益。', '']
    return '\n'.join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-name', default='deepseek_flash_free')
    parser.add_argument('--output-dir', default=str(OUT / 'audit_corrected'))
    args = parser.parse_args()
    data = audit(args.run_name)
    directory = Path(args.output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'audit.json').write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    text = render(data)
    (directory / 'report.md').write_text(text)
    print(text)


if __name__ == '__main__':
    main()
