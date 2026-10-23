# cognitive-enzyme — pi 扩展

给 [pi](https://github.com/earendil-works/pi)(`@earendil-works/pi-coding-agent`)
装上 Cognitive Enzyme 的记忆层,与 Claude Code hooks(`hooks/ce_*.py`、
`hooks/npm/*`)行为一致:

| 时机 | 行为 |
|---|---|
| `before_agent_start` | POST `/api/prime`,命中的酶以 `⟦CE:BEGIN⟧…⟦CE:END⟧` 块作为 `custom_message`(customType `cognitive-enzyme`,`display: false`)注入上下文 |
| `tool_result`(`isError`) | POST `/api/prime`(`source="failure"`),命中的"以前遇到类似报错的经验"追加在报错后面 |
| `session_before_compact` / `session_shutdown` | 把分支摘要(含 `[tool_error]`/`[tool_ok]` 工具痕迹,注入块压成 `⟦CE:SEEN …⟧`)POST `/api/capture`,增量捕获 |

所有网络请求带超时,任何错误(后端没起、超时、报错)都静默放过——**绝不阻塞
pi**。后端返回的酶先按 `CE_PRIME_MIN_CONF` 过滤(`exploratory` 豁免)。

## 安装

```bash
ln -s /path/to/Cognitive-Enzyme/hooks/pi ~/.pi/agent/extensions/cognitive-enzyme
```

或临时加载:`pi -e /path/to/Cognitive-Enzyme/hooks/pi/index.ts`
(`-ne` 只禁发现,显式 `-e` 仍会加载;评测 harness 用 `-ne`,不影响实验)。

## 配置文件

环境变量之外,扩展启动时还会读 `~/.config/cognitive-enzyme/hook.env`
(`KEY=VALUE` 每行一条,`#` 注释,值可带引号)。**进程环境变量优先于文件**。
后端开启鉴权时把静态机器令牌放这里(文件建议 `chmod 600`):

```bash
mkdir -p ~/.config/cognitive-enzyme
printf 'CE_API_TOKEN=<在 Flow 设置页生成或用 PUT /api/admin/security/tokens/api 设置的令牌>\n' \
  > ~/.config/cognitive-enzyme/hook.env
chmod 600 ~/.config/cognitive-enzyme/hook.env
```

## env

| 变量 | 默认 | 说明 |
|---|---|---|
| `CE_API_URL` | `http://localhost:8420` | CE REST 基址 |
| `CE_API_TOKEN` | 空 | 静态机器令牌(服务端开鉴权时必填) |
| `CE_PRIME_TIMEOUT` | `9` | prime 请求超时(秒) |
| `CE_PRIME_MAX` | `6` | prompt 注入条数上限 |
| `CE_PRIME_MIN_CONF` | `0.25` | 置信度下限(exploratory 豁免) |
| `CE_FAILURE_MAX` | `3` | 报错时注入条数上限 |
| `CE_FAILURE_TIMEOUT` | `9` | 报错判定请求超时(秒) |
| `CE_CAPTURE_TIMEOUT` | `5` | capture 请求超时(秒) |
| `CE_DOMAIN` | 空 | 限定召回领域 |
| `CE_PROJECT` | 空 | 显式项目标识(缺省服务端从 cwd 推导) |
| `CE_PI_DISABLE` | — | 设 `1` 时整个扩展不注册 |

## 文件

- `index.ts` — 扩展本体(事件注册、网络、增量捕获状态)
- `core.mjs` — 纯函数:`toolSummary` / `formatBlock` / `seenIds` / `buildDigest`
- `core.test.mjs` — `node --test hooks/pi/`(digest、seenIds、与 npm hook
  输出逐字一致)
