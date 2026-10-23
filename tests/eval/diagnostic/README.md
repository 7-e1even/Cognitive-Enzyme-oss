# 数学、物理全链路补采与记忆必要性对照

2026-10-16 用户已授权本次 API 补采和服务器测试。旧 `offline` 命令仍保持零网络。
本流程保存新实验，不覆盖历史答案、v1/v2 标注、数据库或线上参数。

```bash
rtk .venv-eval/bin/python -m tests.eval.diagnostic.protocol prepare
# 在独立服务器容器中，读取其已有模型配置并建 ce_diag_* 专用向量索引
rtk python -m tests.eval.diagnostic.collect --directory /evidence
# 检索补采仅做一次，正式模型组使用独立目录，不混用 pilot 的解题结果。
# 以下命令完全离线，默认读取 collection-2026-10-16/formal
rtk .venv-eval/bin/python -m tests.eval.diagnostic.report
rtk .venv-eval/bin/pytest -q tests/eval
```

输入覆盖已有 89 道数学、372 道物理题。使用成熟冻结记忆库，逐题排除本题和近重复题
曾 ADD/UPDATE 的全部记忆，以及保存了相同/近似题面正反例的记忆。最终库的部分直接
正标签经已知内容风险过滤后，只有未被来源排除的条目用于 gold 对照。未标记不等于负例。
未记录的记忆谱系仍可能存在泄漏；该限制随输入 manifest 保存。

`collect` 只读生产 PostgreSQL 的模型/参数配置，连接强制只读；不启动主应用、不迁移
schema、不使用 Engine 的日志/学习副作用。运行实际匹配函数，关系加成因快照缺数据
固定为空；领域描述缺失，使用冻结库领域名。显式关闭额外 LLM 回退、扩写和随机探索，
全部差异保存到 manifest。新数据不是原学习流的逐时刻复现。

每题保存：完整向量排名、近似与精确检索结果、BM25、经验样本、合并候选、条件过滤、
全量精排分数（线上仍只接收原 top_n）、打分、裁剪前判定池、实际 Jev 输入/输出、
选择及 hook 过滤结果、各阶段服务错误。embedding 的输入、向量和原始响应单独缓存。
另对未泄漏的 gold 做完整题面/规则的单条 noul 影子测量；它没有 choice/样本提示，
只能作为输入差异线索，不能把分差全部归因于截断。

`benefit` 以同一免费模型、同一预算分别闭卷、注入系统选择、注入已标正确记忆作答。
有可用 gold 的题闭卷抽两次，其余一次；系统未注入时复用闭卷答案。来源排除在解题
前完成。判分前持久化答案，判官失败保持未知，推理时间预算耗尽按答题失败计。
免费模型会话的实际模型和 cost 由现有 OpenCode runner 核验。判官在独立会话盲判，
与作答属于同一家族，并非独立人工真值；先运行等价、缺小问和单位换算校准。

当前 OpenCode CLI 的位置参数入口实测会重复消息。正式组通过 stdin 发送，读取本机
持久化用户消息核验恰好一份完整原文。参数重复的旧结果全部保留为 pilot，不计入正式
模型成绩。输入投递不一致属于评测故障，不属于模型答错。

本轮检索 461 题及 146 个完整输入影子探针已完成。解题 870 个计划请求中仅 65 个完成、
41 个预算超时，其余限流/服务失败或未调用，提示收益对照尚未补齐。`baseline-progress`
里的 complete 是已处理请求数，不代表成功数。以 `campaign-audit.json` / `report.md`
的分项状态为准，不能把缺失成绩当错，更不能声称已经确定最优召回门。

`service.py` 在 rate-limit/quota/401/402/403/429 时写共享停止标记，停止同一目录下新
API 请求。旧数据不覆盖。`recovery.py` 只离线准备新目录，复用已核验成功的调用和已记录
预算失败，保留原失败证据与哈希；`recovery-prepared/` 已准备本轮准确缺项（764 个解题
请求，以及未判分/输入审核缺口）。服务恢复后，在新目录按原模型/预算运行以下命令；
成功缓存会被复用，限流会再次停止，默认不安排自动重试：

```bash
# 优先逐题补齐 114 道有标记题的裸答/提示对照，不先耗尽所有无标记题的裸答预算。
rtk .venv-eval/bin/python -m tests.eval.diagnostic.benefit run --directory tests/eval/offline/data/collection-2026-10-16/recovery-prepared --gold-only --concurrency 1
# 上一步完成、服务可用时，再补其余题。
rtk .venv-eval/bin/python -m tests.eval.diagnostic.benefit run --directory tests/eval/offline/data/collection-2026-10-16/recovery-prepared --concurrency 1
```

后续补采也应优先做有正例的闭卷/提示对照，以获得信息量较高的需求标签。原始请求、
本机超时会话、逐题参考争议及全部阶段排名均已保存；离线分析无需再次调用 embedding、
rerank 或 Jev。新的分析代码另存哈希，正式组原执行代码快照保持原样。

## 全量候选保护修复对照

`compare_fix.py` 在全部 95 个延后判定正例和 16 个原已注入正例上，对照旧 two_stage
与“原候选保护 + 新扫描候选独立 choice”的修复版。修复版不改变召回、embedding、rerank、
选择阈值或记忆内容；它只把原候选和新增候选分开判定，并把两路结果写入同一条判定记录。
服务 503/422/超时单独归为未知，不算语义退步。运行结果保存在
`offline/data/collection-2026-10-16/two-stage-fix3/`，报告为 `report.md`。

本次 92 道题有效试次中，旧版保留原已注入正例 13/16，修复版 16/16；压力题的旧版
候选只有 p、没有 q，修复版恢复 q≈0.733 并注入。修复版平均每题多约一次 HTTP choice
请求，判定 p50 约增加 0.9 秒。新增候选没有独立人工真值，因此不把所有新增注入算作正确。
在解题模型服务恢复前，不自动把修复版接入生产；需要继续做 system/closed-book 的实际
答题收益对照。

### 免费模型的修复前后解题对照

`prepare_fix_benefit.py` 已把 fix3 的 92 道题复制成 `legacy/` 和 `fixed/` 两个独立
评测目录。两边使用同一个 `benefit` 解题/判分 runner；closed-book、gold 和校准缓存只
复用已核验的完全相同请求，system 的注入列表分别来自 `legacy12`、`fixed12`。这一步
不调用检索或 Jev，也不改线上开关。`compare_fix_benefit.py` 只读两个目录，统计 closed-book
→ system 的救回/伤害以及 legacy → fixed 的答题状态转移。

本次启动时 DeepSeek V4.1 Flash Free 连续返回 `Rate limit exceeded. Please try again
later.`，因此 `fix3-benefit-20261016/` 当前只有缓存快照，没有有效 system 配对，不能
据此判断答题正确率。服务恢复后可在原目录续跑（先跑有标注题，再跑其余题）：

```bash
rtk .venv-eval/bin/python -m tests.eval.diagnostic.benefit run --directory tests/eval/offline/data/collection-2026-10-16/fix3-benefit-20261016/legacy --gold-only --arms all --concurrency 1
rtk .venv-eval/bin/python -m tests.eval.diagnostic.benefit run --directory tests/eval/offline/data/collection-2026-10-16/fix3-benefit-20261016/fixed --gold-only --arms all --concurrency 1
rtk .venv-eval/bin/python -m tests.eval.diagnostic.compare_fix_benefit --directory tests/eval/offline/data/collection-2026-10-16/fix3-benefit-20261016
```

只有同一题的 closed-book、legacy system、fixed system 都有有效判分时，才把 fixed 的
变化算作候选保护修复带来的答题变化；限流、超时、判官失败都保持未知。

### GLM / ZCode 入口检查

本机 ZCode CLI 已确认可调用 `magpie/zcode/GLM-5.3-Flash`：健康探针返回成功，持久化
请求中恰好有一份完整用户 prompt，未发生工具调用。ZCode 的 CLI 默认带 coding-agent
系统上下文；在复杂数学/物理题上出现长时间推理和 60--180 秒超时，因此这次只保存入口
可用性，不把它冒充成完整答题收益评测。`opencode-go/glm-5.3-flash` 也能返回，但探针
报告了非零 provider cost，且 GLM 判分校准出现 120 秒超时；它不是之前的免费模型口径。
入口诊断保存在 `offline/data/collection-2026-10-16/glm-entrypoint-health.json`。

全量对照中有两次 422 是保护集为空时的 `none` 单选输入；当前代码已加入空保护集守卫，
不会再发只有 `none` 的无效请求。旧实验响应仍原样保存，统计不回写。

模型自报 Confidence/Need_memory 单独保存，不拿自报当“本来就会”的标签。
闭卷两次均对、均错后 gold 救回、仍错、闭卷不稳定分别统计。置信度门槛只在固定
calibration 子集选取，随后在未参与选门槛的 holdout 子集评估。该门使用完整裸答，
节省的是记忆注入/后续检索，不能声称免去了裸答推理成本。

缓存请求、原始响应、输入与代码哈希都保存在 `offline/data/collection-2026-10-16/`。
不保存账号配置、Authorization 或 key。采集命令显式调用 API；report 不会自动补调。

## 现有分批判定的小范围对照

`two_stage.py` 冻结已采集候选与选择阈值，使用实际 `_start_judges`、
`_collect_judges`、`judge_two_stage` 和 `_apply_jev_dosage`，仅改变单批与扫描池
12/24/36。按领域及原池排名固定抽取 16 道损失题，加 4 道原已注入题；不依据
Jev 概率或解题成绩挑选。覆盖 95 组中的 17 组，不能直接外推全部 95 组成功比例。

```bash
# 零网络：固定样本、核验原始单批请求与选择能否精确重放。
rtk .venv-eval/bin/python -m tests.eval.diagnostic.two_stage prepare
rtk .venv-eval/bin/python -m tests.eval.diagnostic.two_stage validate
# 仅在具备原模型配置的隔离进程中显式运行；调用 Jev，不调用解题模型/检索。
rtk .venv-eval/bin/python -m tests.eval.diagnostic.two_stage run
# 只读此次原始响应，零网络。
rtk .venv-eval/bin/python -m tests.eval.diagnostic.two_stage_report
```

每个题目/方案发新请求，轮换执行顺序，实际 HTTP 次数含原生重试与失败。
计时覆盖整个 Jev 阶段、并发等待及 6 秒预算，包含独立 HTTP 客户端连接成本；
不将它宣称为完整 prime 延迟。逐条区分扫描范围外、扫描失败缺分、有分未选、最终注入。
原生产参数、数据库、记忆正文及旧正式组不写入。实验保存在
`offline/data/collection-2026-10-16/two-stage-small/`。
