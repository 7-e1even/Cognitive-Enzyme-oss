# 离线记录与召回复算

先保存，再复算。下面两条命令只读本地文件，不连接模型、数据库或网络。

```bash
rtk .venv-eval/bin/python -m tests.eval.offline.records freeze --record tests/eval/offline/data/2026-10-15
rtk .venv-eval/bin/python -m tests.eval.offline.records report --record tests/eval/offline/data/2026-10-15
```

`freeze` 不覆盖已有目录；同名快照再次分析只运行 `report`。新快照换一个目录名。报告每次校验原始文件的 SHA-256，原始答案、原始判分、修正后的判分分别保留。

## 保存内容

```text
offline/data/<记录名>/
├── manifest.json                 # 原路径、文件长度、哈希和已知缺口
├── hardbench/
│   ├── problems.json             # 题目与参考答案
│   ├── math/history/             # 数学的原答案、注入 ID、判分和学习记录
│   └── physics/
│       ├── history/              # 物理的原答案、注入 ID、判分和学习记录
│       └── retest/               # 冻结注入、最终记忆快照、正式重测、试跑归档
├── recall/
│   ├── corpus/                   # 已标注召回基准的记忆正文
│   ├── queries.json              # 查询及 expected/acceptable 记忆 ID
│   └── history/                  # 各历史配置的候选分数、注入和阶段归因
├── checks/                       # 已完成的判分器校准记录
├── code/                         # 保存当时的评测代码，供追溯
├── report.json                   # 纯离线复算结果
└── report.md
```

数据保存在本机，`data/` 不进入 Git。不会复制 `.env`、账号配置或 API key。

## 读数边界

- 数学、物理竞赛题放在 `hardbench`。旧记录只有“最终注入”而没有正确记忆标签和完整召回池时，召回率显示未知；不把未注入算成漏召回。
- `recall` 中的 math/physics 是另一套已标注的记忆查询，不代表竞赛题解题效果。默认报告只展示这两个领域，不混入 Firefox 等领域。
- 原文件存在多个配置指纹时分别统计；同一指纹、同一查询只取最后一条。服务错误和精排失败另列。
- `recalled_ids` 缺失是未知，记录为 `[]` 才是空池。旧阶段归因与完整 trace 的重算结果分别标记证据来源。
- 可以用已保存的候选、精排分数和判官输出分析后续筛选。改查询表达、embedding 或 reranker 后，如果所需输出从未缓存，旧记录无法生成新结果；需要本地模型或另行授权的数据采集。
- 更换注入后的解题收益不能从旧答案推算；当前阶段只分析召回链路，不重复调用解题和判分 API。

## 后续采集格式

以后确实需要补采时，除题目、完整记忆版本、模型与参数、排除的本题来源记忆外，至少记录以下字段。采集属于另一个显式操作，`freeze`/`report` 不会触发。

```text
expected_memory_ids / acceptable_memory_ids / 标注来源
recalled_ids / condition_killed_ids / rerank_raw / reranked_ids
scored / jev / jev_none / jev_status / dosed_ids / injected_ids
每阶段错误与超时 / 配置指纹 / 使用的记忆快照哈希
```

竞赛题的“正确记忆”需要按适用性标注，不能直接拿模型自己注入的 ID 当标准答案。当前旧文件缺少的部分已经在报告中列出。

## 数学、物理题的正确记忆标注

[标注说明](annotations/README.md)与 [v2 标注](annotations/math_physics.v2.json)针对数学 `self`、物理 `self_v3` 的现有经验流。已初筛 89 道数学题、372 道物理题；其余计划数学题未运行。正例是部分集合，未确认的题保留弃权状态，不生成“无相关记忆”标签。

v2 按完整触发条件和实际所问子问收紧了 v1 的三条漏选线索：两条只算间接提示/需要类比，一条依赖未给出的薄层近似。明确适用而未选的案例从6例改为3例。v1 原文件、原报告和哈希保留，不覆盖旧结论的记录。

本机已额外保存实验库的只读导出，包括记忆正文、`decision_log`、`evolution_log` 和结果。后续复算只读这些文件，不需要连接 PostgreSQL：

```bash
rtk .venv-eval/bin/python -m tests.eval.offline.diagnose
rtk .venv-eval/bin/python -m tests.eval.offline.forensics
rtk .venv-eval/bin/python -m pytest tests/eval -q
```

默认输入是 `data/2026-10-15/` 原记录、`data/math-physics-labels-2026-10-15/sources/` 导出和版本化标注。可用 `--record`、`--sources`、`--annotations` 显式指定输入。两份原始 manifest、记忆库、题面、记忆正文与历史版本见证全部校验哈希；不改写原快照。`offline/data/` 是证据存档，pytest 不收集其中复制的旧测试文件。

当前输出位于 `data/math-physics-labels-2026-10-16/`：

| 文件 | 内容 |
|---|---|
| `report.md`、`summary.json` | 覆盖、阶段计数、具体反例与结论边界 |
| `question_labels.jsonl` | 全部计划题的审核状态；最终库正例与历史可用正例分开 |
| `pair_diagnoses.jsonl` | 题目—正例候选的时序、实际交付正文、版本、分数和归因 |
| `problematic_injections.jsonl` | 已注入且已确认含错误断言或不适用的历史条目 |
| `method/`、`analysis_manifest.json` | 本次实际使用的标注和诊断代码副本，以及产物校验哈希 |
| `forensics/report.md`、`findings.json` | 反馈串题与无验证 UPDATE 的离线复现、历史具体证据、机械检查 |
| `forensics/case_cards.jsonl` | 14条复审线索的题面、完整请求/答案、分数和人工可核对理由 |

只读导出的表、行数、字节数与 SHA-256 继续保存在旧目录 `data/math-physics-labels-2026-10-15/sources/manifest.json`，不复制或改写源数据。

`forensics` 用 AST 载入当前代码的纯选择函数，以及 match/cache/report 与 UPDATE 方法，在数据库、模型和持久化均为桩的隔离环境复现；不会实例化真实应用引擎或调用模型客户端。输出同时保存所执行代码副本与校验和。重复的判定/记忆反馈键只是排查范围线索，不当作所有串题的准确题数；报告中的具体三题已逐项对齐。

这里恢复的是**进入判断器的候选池**，并非完整首段召回池。有 `recall_rank` 的适用条目没被选中，可以确认它已经过召回；候选池外缺席的条目仍不能区分未召回、精排淘汰或池截断。不同/截短的历史正文必须逐份复核，不能用最终版本或一个关键词自动当作当时的正确记忆。
