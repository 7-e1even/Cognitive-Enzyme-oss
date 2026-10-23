# 评测入口

| 目录 | 评测对象 | 入口 |
|---|---|---|
| `offline/` | 保存已有记录，离线复算召回与日志覆盖 | [离线说明](offline/README.md) |
| `hardbench/` | 数学、物理竞赛题的解题与经验学习 | [难题评测](hardbench/README.md) |
| `corpus/`、`queries.json`、`run_eval.py` | 带正确记忆 ID 的合成召回查询，含 math/physics 等领域 | 本文下方 |
| `realbench/` | Firefox 缺陷、判例、安全等真实领域的记忆查询 | [真实领域评测](realbench/README.md) |
| `jev_gate/` | 判官、去重、反馈等专项评测 | [专项说明](jev_gate/README.md) |
| `distill/` | 记忆提炼与蒸馏对照 | [蒸馏评测](distill/README.md) |
| `reports/` | 历史召回报表，保留原始结果与配置指纹 | [报表索引](reports/README.md) |

**默认工作方式：保存记录，后续离线分析。2026-10-16 用户另行授权了本次 API 补采，入口见 [全链路诊断](diagnostic/README.md)。** 原有 offline 命令仍不调用 API。

```bash
rtk .venv-eval/bin/python -m tests.eval.offline.records report --record tests/eval/offline/data/2026-10-15
rtk .venv-eval/bin/python -m tests.eval.offline.diagnose
rtk .venv-eval/bin/python -m tests.eval.offline.forensics
```

数学/物理竞赛题与召回基准中的同名领域分别统计。旧竞赛题没有保存完整召回池和正确记忆标签时，不能把“未注入”解释为“漏召回”。

数学 `self` 与物理 `self_v3` 的内容标注及历史阶段诊断见 [标注说明](offline/annotations/README.md)。复算只读已保存的判断池和记忆版本，不调用 API；当前正例集合非穷尽，不能据此给出总体召回率。

默认使用经过完整触发条件与子问复审的 v2 标注。`offline.forensics` 另检查反馈串题、无验证 UPDATE、原始概率映射与选取规则；隔离复现全部使用本地桩，原始记录和 v1 报告不覆盖。

## 合成记忆召回基准

对 CE 记忆召回链路的端到端评测基准：250 条五领域语料（math/physics/logic/methodology/software，含易混淆近邻簇）+ 130 条 golden 查询（100 positive 已标 expected/acceptable + 30 silent）。评测跑在独立库（PG `cognitive_enzyme_eval` + Qdrant `enzymes_eval`），不碰生产数据。

## 历史在线流程（会调用 API，当前不运行）

1. `python -m tests.eval.bootstrap` — 冒烟（建/连独立 eval 库，读生产 app_settings 拿 embedding/rerank/LLM 配置）
2. `python -m tests.eval.import_corpus --wipe` — 导语料 + full_sync 向量
3. `python -X utf8 -u -m tests.eval.run_eval --name <n>` — 评测，报表在 `reports/`
4. `python -m tests.eval.run_eval --compare a b` — 两次运行指标 diff

语料重生成：删 `corpus/*.json` 后 `python -m tests.eval.generate_corpus`，再 `--queries`、导入后 `python -m tests.eval.annotate_queries`（增量落盘可续传）。

评测特性：逐段归因（miss 精确到 not_recalled / killed_conditions / rerank_failed / rerank_dropped / dosage_cut / hook_conf_filtered——rerank_failed 为 2026-07-07 新增：rerank 尝试了却失败回退 pattern 打分时单列，不再污染 dosage/hook 桶）、配置指纹续传（`--name` 同名重跑按 config_fp 隔离缓存，防 A/B 混配置）、错误率 >5% 自动中止。自 2026-07-07 起 outcomes 落盘逐查询明细（`scored`=[id,score,tier] 全量、`dosed`、`rerank_raw`=floor 过滤前的 rerank 分）——变k剂量/floor 重扫的离线模拟数据源，旧报表无此字段。

`run_eval.py` 的 `HOOK_MIN_CONF` 默认镜像 `hooks/ce_prime_hook.py` 的 `MIN_CONFIDENCE` 出厂值 0.25；如需临时改用其它阈值复跑，用 `CE_EVAL_HOOK_MIN_CONF` 覆盖，不要直接改代码默认值。

## 达标线与最终结果（2026-07-06，spec: docs/superpowers/specs/2026-07-06-recall-eval-tuning-design.md）

| 指标 | 目标 | 基线 | 调优后 | 判定 |
|---|---|---|---|---|
| Recall@6 | ≥90% | 61.0% | **82.0%** | 未达（-8pt） |
| Precision | ≥80% | 24.4% | **42.5%** | 未达（-37.5pt，结构性） |
| 静默率 | ≥90% | 0% | **100%** | ✅ |
| MRR | 报告 | 0.432 | **0.619** | — |

已提交的调优默认（21 个变体点两轮扫描验证为 P/R 前沿端点）：
hook `MIN_CONFIDENCE=0.25`、scorer `W_RELEVANCE=0.55`、相关性下限门 `CE_RELEVANCE_FLOOR=0.01`（静默率 0→100 的关键）、`CE_CONDITIONS_MODE=soft`（条件误杀 11→0）。

**报表 provenance 注意**：`reports/final.json`、`reports/combined.json`、`reports/knobD2.json` 是在旋钮快照记录逻辑滞后于代码默认值的窗口期跑出的——三份报表里的 `config.scorer_w_relevance` 字段都显示 0.35，但当时实际跑测用的运行时默认已是 0.55（`final.json` 还早于 `dosage_budget`/`conditions_boost_stage` 快照键被加入 `config_snapshot` 之前，故不含这两个键）。这是快照记录口径滞后，不是真的用 0.35 跑出的指标——上表指标数字本身有效，只是这三份报表 JSON 里的 `scorer_w_relevance` 字段不可信。不要手改这几份 JSON；commit 02186f5 起的报表快照口径已修正、可信。

## 旋钮（env，均有守卫解析）

`CE_EVAL_HOOK_MIN_CONF` / `CE_W_RELEVANCE` 等五权重 / `CE_RELEVANCE_FLOOR` / `CE_CONDITIONS_MODE`(soft|hard) / `CE_CONDITIONS_BOOST`(+`_STAGE` pattern|rerank) / `CE_DOSAGE_BUDGET` / `CE_RERANK_TOPN_MULT` / `CE_SEARCH_QUERY_MODE`(full|context) / `CE_RERANK_DOC_MODE`(patterns|plain) / `CE_EXPLORATION_RATE`

## 剩余缺口的结构性归因（调参已证明无解，需架构级工作；2026-07-07 对抗验证修正）

- **Precision 80%**：固定注入 ≤6 条 vs 相关池中位数 2~3 条 → 排序完美的数学天花板 ~68.5%，实测前沿封顶 50.3%。主路径是**变 k 剂量**（按分数分布自适应砍尾：63/100 条查询满额注入 5 条，砍到相关池中位数分母近乎减半，precision 有 ~60-70% 的结构空间）。实施约束：变 k 必须位于 relevance floor **下游**、floor 本身不动（gB6/gB8 里 floor 旁路时静默率崩到 46.7%/56.7% 是现成警示）。
- **变 k 对 Recall 无解（2026-07-07 验证推翻旧结论）**：9 条 dosage_cut miss 全部 injected=5、golden 排名≥6；hook 硬顶 6 之下变 k 每查询只有 +1 空位，round-G budget=6 实测只救回 1~3/9——这批 miss 本质是 Stage 4 排序问题，不是截断策略问题。旧版"变 k 救回 dosage_cut 大部分"的说法作废。
- **Recall 90%**：18 miss = dosage_cut 9 + rerank_dropped 7 + not_recalled 2。rerank_dropped 集中于"具体情境查询 × 抽象原则记忆"的间接语义匹配（bge-reranker-v2-m3 弱区）——**精排换代**（如 Qwen3-Reranker，SiliconFlow 同端点、`rerank_model` 一处配置即换）是第一优先；但 `CE_RELEVANCE_FLOOR=0.01` 定标于 bge 的两极分化分布，换模型后沿用会漏杀导致静默率崩塌，**必须复刻 sweepF 系列重扫 floor**，顺带盯 tier 阈值(0.7/0.3)漂移。向量召回近饱和（not_recalled 仅 2），混合检索收益 ≤2pt，优先级最低。
- methodology 域全程最弱（70%），架构验证时单独盯。

注意：并行跑多个评测会触发 SiliconFlow rerank 429 限流——2026-07-07 起 rerank 对瞬时失败(429/5xx/超时)重试一次、失败回退时归因单列为 rerank_failed（不再让静默率静默虚低），但需要精确数字时仍应串行跑。
