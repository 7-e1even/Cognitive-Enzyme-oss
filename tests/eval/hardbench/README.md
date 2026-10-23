# hardbench:在模型自己不会的难题上测自我改进

当前先保存记录、离线分析召回链路。数学/物理的题目、旧答案、注入与重测记录见[离线入口](../offline/README.md)；下文在线命令会调用 API，当前不继续运行。

目录约定：代码放本目录；题集与缓存放 `data/`；历史经验流放 `data/rsi/{math,phys}.*`；物理重测的冻结输入保留在 `data/rsi/retest_self_v3/`，正式运行放其 `runs/<运行名>/`，模型接入试跑放 `archive/model-probes/`。

realbench 的静态题 GLM-5.3 不注入就能答对 94–95%（theorem、biomed），记忆没有用武之地，也测不出自我改进。这里收集前沿模型也答不好的竞赛级、专家级难题，全部来自公开的人写数据，并且都带标准答案。

## 数据

`python -m tests.eval.hardbench.build` 生成 `data/problems.json`（不入库）。

| 来源 | 领域 | 题数 | 判分 |
|---|---|---|---|
| BeyondAIME（字节 Seed，2025，CC0） | 奥数 | 100 | 整数，精确比对 |
| OlymMATH en-hard（人大，2025，MIT） | 奥数 | 100 | 能算出数值就比数值，否则由大模型判等价 |
| IMO-AnswerBench（DeepMind IMO-Bench，2025，CC-BY-4.0） | 奥数 | 400 | 同上 |
| AMO-Bench（美团，2025，MIT） | 奥数（超 IMO 难度） | 50 | 同上；带官方解答 |
| PhysElite（2026-04） | 物理竞赛 | 英文、无图、难度 5–7，共 372 | 大模型判等价；带官方解答 |
| MedXpertQA Text（清华，2025，MIT） | 医学专家级 | 2450 | 十选一，精确比对 |
| FrontierScience 公开集（OpenAI，2026，Apache-2.0） | 物理、化学、生物 | 160 | 大模型判等价 |

- PhysElite 的原始文本有转义损坏：`\frac` 变成了换页符加 `rac`，`\text` 变成了制表符加 `ext`，构建时还原。整包 944MB，只通过行接口取文字字段。
- 判等价由同一个 agent 完成（pi + GLM-5.3，思考 low）。它只比较最终答案行和标准答案，不重新解题。

## 闭卷难度：`run.py`

agent 使用 pi 的非交互 JSON 模式：
- 模型 `zai-coding-cn/glm-5.3`，思考 high；
- 不给工具，不加载扩展、技能和上下文文件；
- 密钥取自 pi 已配置的服务商，只放在子进程的环境变量里。

## 经验流：`rsi.py`

同一批题按固定顺序做两遍，看系统从前面的题里积累的经验能不能让后面的题做对更多。

| 组 | 做法 |
|---|---|
| base | 闭卷，不注入 |
| self | 只从自己做对（最终答案经核对正确）的题里提炼经验：没有外部老师，奖励可验证 |
| teacher | 在 self 的基础上，做错的题也学：给出标准答案（有官方解答的连解答一起），提炼"错在哪" |
| seed | 预先装入专家方法论（`seed.py` 从文档导入），做题时注入，但不反馈、不学习：静态对照 |
| seed_self | 从同一个方法论种子起步，再像 self 一样从做对的题里学 |

记忆组每道题都走产品原有的流程：
1. prime 注入：规则与 hook 相同，至多 6 条，渲染一字不差。
2. 作答并判分。
3. 按产品协议把核对结果 report 给注入过的记忆。
4. 把"题目 + 解答 + 核对结果"当作一段会话交给产品的 capture 提炼。
   - capture 现在提炼两类经验：领域方法，和通用方法论（标 `scope:general`，指怎么拆解、怎么排查、怎么验证）。
   - 提炼出的经验先查库里有没有相似的：没有就走 `engine.harvest`（质量门、领域归类）；有就由 capture 裁决新增、补充还是强化。
   - 同一条方法论在很多题上被反复印证，会积累经验样本和置信度。
5. 碰过的记忆都把这道题记为经验样本。

方法论种子：`app/learning/ingest.py` 是产品的文档导入功能，流程是切块 → 提炼"在什么情况下、具体怎么做" → 查库 → 质量门，口号和故事不提炼。
- `seed.py --name genius --file "天才是怎样思考？.docx:天才是怎样思考"` 建好种子库 `ce_rsi_seed_genius`。
- seed 组开跑时用这个库做模板复制一份，然后重建向量索引。

### 方法论提示通道（记忆组开跑约 6 小时后加入，所有记忆组已按新设置重启）

诊断时发现，物理 self 组做了 21 道题，一次都没注入过。召回没问题，相关经验精排分数能到 0.83。卡住的是静默门：判官对解题请求常给出 none≈0.66–0.74 的概率，超过静默门 0.5。于是"用极限情形自检"这类方法论，即使适用概率有 0.4–0.5，也全被拦下。

这套阈值是为具体知识标定的（缺陷、判例），推错这类知识会误导模型。方法论是提示，推错了模型可以自己忽略。所以新增一个运行参数，默认关闭，这次实验的记忆组打开：

| 参数 | 值 | 作用 |
|---|---|---|
| `selection.hint_max` | 2 | 通用方法论（`scope:general`）不受静默门约束，每次最多注入这么多条 |
| `selection.hint_p` | 0.4 | 入选需要的逐条适用概率 |

- 0.4 取在已有两个阈值之间（逐条下限 0.3、单选 0.5）。它是按设计选定的，没有用测试题的对错来调。
- 旧设置下的结果移到了 `data/rsi/nohint_archive/`。
- 基线组不注入记忆，不受影响，继续跑。

### 防过拟合

- **开跑前定死**：认知酶全部用默认参数，只关掉探索槽（与 realbench 经验流一致）。提示词通用，不针对这批题调。
- **经验必须通用**：提示词禁止写本题的数字、对象和答案。程序会再过滤一遍，以下三种一律丢弃，丢弃数写进报表：
  - 含本题标准答案；
  - 含题面里 3 位以上的数字；
  - 连续 8 个词照抄题面。
- **不挑题**：整个集合按固定种子打乱后全部做，不按模型会不会来筛。
- **配对比较**：base 与记忆组做同一批题，逐题比较（McNemar 检验）。另外单独报告三组子集：后半程、去掉近重复题、有注入的题。
- **近重复**：与流里前面某道题的词 3-gram Jaccard 相似度 ≥0.5 的题算近重复。PhysElite 的 372 道里有 65 道：同一题干换了问法。
- **因果顺序**：几道题一批并发作答，一批做完才学习。

各组的记忆写进独立的库 `ce_rsi_<tag>_<arm>` 和独立的向量集合，互不干扰。模型绑定只读取测试平台库。

```bash
.venv-eval/bin/python -m tests.eval.hardbench.rsi --tag phys --sets physelite --arm base --concurrency 2
.venv-eval/bin/python -m tests.eval.hardbench.rsi --tag phys --sets physelite --arm self --concurrency 3
.venv-eval/bin/python -m tests.eval.hardbench.rsi --tag phys --report
```

智谱 Coding Plan 同时在飞的请求超过 5 个左右就会返回 1302 限流。
- 所有调用 GLM 的地方都共用一组跨进程的全局槽位：pi 作答、判等价、提炼入库、种子导入。
- 槽位用文件锁实现，个数由 `CE_GLM_SLOTS` 设置，默认 5。
- 所以几条经验流可以同时跑；各组的 `--concurrency` 只决定本组一批做几道题。

## 评测口径修正（2026-10-15）

以下只修改评测代码。产品的学习、召回和记忆结构不变。

- `grading.outcome` 把判官失败、服务错误、真实答错分开；旧记录里的 `correct=false, judge_ok=false` 也读作未判定。学习与反馈跳过未判定结果。
- 判分使用完整最终答案。旧代码的 500 字截断会让多问答案丢掉后半段；判分提示也已改为只比较题面实际要求的小问。
- 判官只接受明确的最后一行标签。每个答案先落盘，再判分；补判复用原答案。
- 结果按同题且两臂都有效的配对比较，并单列缺失、服务错误和未判定。工具调用违规算模型作答失败，不自动重抽一次答案。
- 每次运行保存模型、预算、判分提示、冻结题集和注入的哈希。不同条件必须使用不同运行名。
- 历史 `primes.jsonl` 没存召回池，无法补推出每道物理题的召回/选择归因。后续 `prime` 会保存逐段 trace。

### 免费 DeepSeek 重测

默认使用 `opencode/deepseek-v4.1-flash-free`，high，每次模型请求的输出上限 32000，整个作答上限 600 秒；判分使用同一免费型号的 low 档。
OpenCode 可以在长度截断后续写，用量按完整作答累计；达到作答时间上限计失败，不另抽一次答案。判官超时仍计未判定，可复用原答案补判。
在 OpenCode 桌面端登录一次即可。macOS 会自动寻找桌面安装包中的 CLI，也可用 `--opencode-bin` 指定。

OpenCode v2 的官方运行方式和免费提供商集成需要保留。评测使用空临时目录和非交互权限规则；工具请求不会获批，出现工具调用的答案计失败。运行后核对实际模型、档位、费用和持久化完成状态，不依赖 v1 的 `step_finish` 事件。

```bash
rtk .venv-eval/bin/python -m tests.eval.hardbench.retest --stage solve --concurrency 1 --max-wait 60
rtk .venv-eval/bin/python -m tests.eval.hardbench.retest --stage grade
rtk .venv-eval/bin/python -m tests.eval.hardbench.retest --report
```

已有的 GLM 答案可以用免费判官重新判分，另存新目录，原始文件不改：

```bash
rtk .venv-eval/bin/python -m tests.eval.hardbench.retest --stage grade \
  --answers-from tests/eval/hardbench/data/rsi/retest_self_v3/solves.jsonl \
  --run-name legacy_regrade_deepseek --concurrency 2
```

这只是补判已保存的答案，不会把新判官冒充为原作答模型。新模型重做的结果与原 GLM 结果分开报告。
错题池仍由历史 GLM 的记录选出，只能说明这个条件子集上的表现，不能当作免费模型的全题集正确率。
本轮新作答和旧答案补判都尚未完成，已保存记录；当前按用户要求停止 API 调用，不能把部分配对当作完整结果。

### 离线审计

```bash
rtk .venv-eval/bin/python -m tests.eval.hardbench.audit
rtk .venv-eval/bin/python -m pytest tests/eval/test_hardbench_protocol.py tests/eval/test_metrics.py -q
```

审计不调用模型或数据库。`data/rsi/audit_corrected/` 保存原始输入哈希、覆盖率、配对结果、题面警报和 realbench 归因。
已确认的题面问题包括：`physelite-587` 为空；`physelite-11509` 没给出求解所需的三个波长；`physelite-2680` 只问第 1 小问而参考包含 7 问。
短题或提到图仅列为复核警报，不能自动断定无效。历史学习轨迹在旧判分下已经发生，修报表不等于重新训练了那些记忆。
