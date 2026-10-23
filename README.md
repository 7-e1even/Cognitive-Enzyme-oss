# Cognitive Enzyme · 认知酶

**给 AI 智能体的 System 1:不靠模型"想起来去搜",而是在任务开始前,把对的经验自动放进上下文。**

智能体(Claude Code、pi 或任何能调 REST/MCP 的 agent)每次解题都像第一次:上次踩过的坑、查到的绕过手法、
项目里没写进文档的约定,下次又得从头摸。认知酶把这些经验存成一条条小规则(酶),在恰当的时刻自动注入,
并随着"这条到底帮上忙没有"的反馈越用越准。

> 设计思路来自腾讯安全沙龙第 8 期(北京站)的分享《基于"认知酶"自进化架构的漏洞挖掘 Agentic 实践》(符芊红)。
> 本仓库是那套架构的通用实现,不限于安全场景。

## 为什么这样做

演讲里总结了 Agent 做复杂任务时反复出现的几类问题,认知酶是针对其中"知识"这一环的解法:

| 问题 | 表现 | 认知酶的做法 |
|---|---|---|
| 隐性知识没显化 | 模型不知道"fastjson 在 jackson 下怎么写文件",只会走常规路径 | 把这类经验存成带"何时用 / 怎么做"的规则,任务前**被动注入**,不让模型去猜 |
| 模型偷懒、早退 | 遇阻就下"无法利用"的结论,单一答案就满足 | 规则里可带"遇到 X 时至少再试两种绕过"这类流程约束 |
| 依赖模型主动搜索 | 主动检索会被跳过,或检索词不对 | 不靠模型"想起来":hook 在提问前、工具报错时自动检索并注入 |
| 技能越堆越多 | 大量固定 skills 拖慢模型、过拟合 | 只注入**当前情境激活**的少量规则,并按反馈裁剪 |

## 工作方式

认知酶借用了两个概念:

- **双过程理论**(《思考,快与慢》):System 1 快速、自动,靠模式匹配;System 2 缓慢、刻意。大模型的推理是 System 2,
  这里补的是 System 1——"看到这个情境就想起那条经验"。
- **Cue → Routine → Reward**(酶的催化结构):每条记忆有"何时用"(触发情境)和"怎么做"(行为),
  学习信号不是奖励本身,而是**奖励与预期的差异**(类多巴胺误差):有用就强化,被说反就削弱,变化才是进化的催化剂。

```text
 智能体 hooks / 扩展 ──prime / failure / capture──► REST :8420 ──► PostgreSQL(记忆·日志·配置)
 (Claude Code / pi / 任意智能体)                      │          └─► Qdrant(向量·BM25 索引)
                              MCP :8421 ◄── 模型主动检索/反馈      └─► 模型服务商(LLM/向量/精排/判官)
                              管理后台 :13000 ◄── 同源代理 /api/*
```

一轮循环:

1. **prime(提问前)**:用当前情境检索——向量召回 + BM25 词面召回 + 经验样本匹配,精排,再由**适用性判官**
   对每条候选打"对这个请求直接有用的概率",决定注入哪几条、注入几条(可以一条都不注)。
2. **failure(工具报错时)**:拿报错原文再检索一次,给出以前修好过的办法。
3. **capture(会话结束 / 压缩前)**:从会话里提炼"报错 → 修好"这类经验,过质量门、查重、归入领域后入库。
4. **反馈**:智能体 `report` 结果,有用的记忆被强化、被说反的被削弱;后台定时**巩固**——合并重复、淘汰低质量,
   并产生待验证的假设记忆(梦境、突变、交叉),以小概率进入"探索槽"接受检验。

和人不一样的一点:**不做遗忘和时效衰减**。Agent 不是人类,不需要遗忘,只要重点——靠记忆质量裁剪,而不是时间。

## 快速开始

需要 Python 3.11+、Node.js 20.9+、Docker(提供 PostgreSQL 与 Qdrant)。

```bash
cp .env.example .env

# 方式一:Docker 全栈(拉 CI 构建的镜像,CE_GHCR_OWNER 填镜像所在的账号/组织)
CE_GHCR_OWNER=<你的账号> bash scripts/deploy.sh

# 方式二:存储用容器,应用跑本机(开发)
bash scripts/dev-up.sh
```

打开 <http://localhost:13000>。管理员用户名默认 `admin`;密码用 `CE_ADMIN_PASSWORD` 指定,
未指定时首次启动会生成随机密码并打在服务端日志里。登录后:

1. **改密码**(账户设置);
2. **模型 → 服务商与模型**:加一个服务商和模型。对话模型、向量模型是必需的;精排、判官可选,缺了自动降级、不报错;
3. **模型 → 模型绑定**:给各环节指定模型;
4. **设置 → 访问与安全**:生成静态 API 令牌给 hook 用。

接口文档:<http://localhost:8420/docs>;健康检查:`/health`、`/readyz`。

## 接入智能体

| 智能体 | 方式 | 安装 |
|---|---|---|
| Claude Code | hook(prime / failure / capture) | `npx -y cognitive-enzyme-hook install --url http://<服务器>:8420 --token <API 令牌>`,细节见 [`hooks/npm/README.md`](hooks/npm/README.md) |
| pi | 扩展 `hooks/pi/` | `ln -s $PWD/hooks/pi ~/.pi/agent/extensions/cognitive-enzyme`,细节见 [`hooks/pi/README.md`](hooks/pi/README.md) |
| 任意 agent | REST / MCP | `POST /api/prime`(带 `source`、`cwd`、`project`);MCP 在 `:8421/mcp` 提供 `prime` / `search` / `recall` / `report` / `harvest` |

所有接入方式都是 **fail-open**:后端不可用、超时、报错一律静默放过,绝不阻塞智能体。

## 配置都在后台

模型、召回、判定、学习相关的全部参数(约 100 项)都在管理后台「设置 → 运行参数」里改,保存即生效;
环境变量只用来在数据库连上之前启动(PostgreSQL、首个管理员、Qdrant 地址)。

新增一个参数只需在 [`app/settings.py`](app/settings.py) 的 `SPECS_LIST` 加一条,后台自动出现,代码里 `settings.get("<key>")` 读取。

## 目录结构

| 目录 | 内容 |
|---|---|
| `app/` | 后端:`matching/` 召回与判定,`learning/` 学习闭环(capture / 巩固 / 梦境),`distill/` 判定记录与老师标注,`routers/` REST |
| `hooks/` | Claude Code hook(Python 版与 npm 包)和 pi 扩展 |
| `webui/` | 管理后台(Next.js + shadcn/ui):记忆库、召回调试、进化事件、模型绑定、运行参数、审计 |
| `ops/gardener/` | 夜间园丁:容器里跑 headless agent,对照代码库复核记忆 |
| `scripts/` | 部署、本地开发、评测与排查脚本 |
| `tests/` | 单测;`tests/eval/` 是召回与效果评测基准(需要自行构建数据,见其中 README) |

## 评测与已知局限

`tests/eval/` 里有召回评测、真实公开数据基准和"记忆注入后能不能答对"的对照实验框架。几点要如实说:

- 注入的记忆**不保证**让模型答对:对"本来就会"的题没有用武之地,对难题则取决于记忆内容本身是否真的对症。
- 召回链路上有多道按条数硬截断的关卡(精排保留数、判定池、判官单次请求的 token 预算),在记忆较长时目标记忆可能在到达判官之前就被截掉;
  相关的可调参数见 `ranking.*`、`judge.*`、`recall.*`。
- 评测数据(竞赛题集、公开数据集)不随仓库分发,请用对应脚本自行构建,并遵守各数据集的许可。

## 开发

```bash
python -m pytest tests --ignore=tests/eval     # 后端单测
node --test hooks/pi/                          # pi 扩展测试
cd webui && npm run typecheck && npm run lint  # 前端
```

## 安全

- 管理员初始密码随机生成(或用 `CE_ADMIN_PASSWORD` 指定),登录后立刻改掉;静态 API 令牌只放行数据面接口,泄露后在后台轮换。MCP 首次启动自动生成令牌,无令牌不开放。
- 登录有限速(同 IP + 账号 5 分钟 10 次),停用用户或改密后旧 JWT 立即失效。
- `docker-compose.yml` 里把 Docker socket 挂给了 app(用于夜间园丁)。拿到 socket 等于拿到宿主机 root,只适合单用户内网;不需要园丁就删掉那两行挂载。
- `.env`、`webui/.env.local`、`~/.config/cognitive-enzyme/hook.env` 均不入库。

## 致谢

演讲分享中致谢:Unam4、柯字辈、java-chains。

## 许可

[GNU Affero General Public License v3.0 或更高版本](LICENSE)(AGPL-3.0-or-later)。

如果你把本软件修改后作为网络服务提供给他人使用,必须向这些用户提供你修改后的完整源代码。
管理后台只使用 shadcn/ui(MIT)与 lucide-react(ISC)等开源组件。
