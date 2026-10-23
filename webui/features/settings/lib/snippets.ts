// 接入页的命令与配置片段:全部由「服务器主机 + 令牌」生成,不写死任何地址。

export interface Endpoints {
  host: string
  apiUrl: string
  mcpUrl: string
}

/** 后端默认端口;实际端口由后端在 /api/admin/security 里报告(CE_API_PORT / CE_MCP_PORT)。 */
export const DEFAULT_PORTS = { api: 8420, mcp: 8421 }

export function endpointsFor(host: string, ports: { api: number; mcp: number } = DEFAULT_PORTS): Endpoints {
  const clean = host.trim().replace(/^https?:\/\//, "").replace(/\/.*$/, "") || "localhost"
  // IPv6 字面量要带方括号
  const hostPart = clean.includes(":") && !clean.startsWith("[") ? `[${clean}]` : clean
  return { host: clean, apiUrl: `http://${hostPart}:${ports.api}`, mcpUrl: `http://${hostPart}:${ports.mcp}/mcp` }
}

export function isLocalHost(host: string) {
  return ["localhost", "127.0.0.1", "::1", "[::1]"].includes(host)
}

// ── Claude Code hooks ─────────────────────────────────────────

export function hookInstallCommand(apiUrl: string, token: string, project = false) {
  return [
    "npx -y cognitive-enzyme-hook install \\",
    `  --url ${apiUrl} \\`,
    `  --token ${token}${project ? " \\" : ""}`,
    ...(project ? ["  --project"] : []),
  ].join("\n")
}

export function hookSettingsJson(apiUrl: string, token: string) {
  return JSON.stringify(
    {
      hooks: {
        UserPromptSubmit: [{ hooks: [{ type: "command", command: "ce-prime-hook", timeout: 10 }] }],
        SessionEnd: [{ hooks: [{ type: "command", command: "ce-capture-hook", timeout: 30 }] }],
        PreCompact: [{ hooks: [{ type: "command", command: "ce-capture-hook", timeout: 30 }] }],
      },
      env: { CE_API_URL: apiUrl, CE_API_TOKEN: token },
    },
    null,
    2,
  )
}

export const HOOK_GLOBAL_INSTALL = "npm i -g cognitive-enzyme-hook"

// ── MCP 客户端 ────────────────────────────────────────────────

export function claudeCodeMcpCommand(mcpUrl: string, token: string | null) {
  const base = `claude mcp add --transport http cognitive-enzyme ${mcpUrl}`
  return token ? `${base} \\\n  -H "Authorization: Bearer ${token}"` : base
}

/**
 * Claude Desktop 的配置文件只认本地命令,远程 HTTP 服务经 mcp-remote 转接。
 * 令牌放在 env 里再引用:把带空格的 "Bearer xxx" 直接写进 args,Windows 上会被拆开。
 * mcp-remote 默认只允许 HTTPS,本机以外的 http 地址要加 --allow-http。
 */
export function claudeDesktopConfig(mcpUrl: string, token: string | null, local: boolean) {
  const args = ["-y", "mcp-remote", mcpUrl]
  if (!local) args.push("--allow-http")
  if (token) args.push("--header", "Authorization:${CE_MCP_AUTH}")
  const server: Record<string, unknown> = { command: "npx", args }
  if (token) server.env = { CE_MCP_AUTH: `Bearer ${token}` }
  return JSON.stringify({ mcpServers: { "cognitive-enzyme": server } }, null, 2)
}

export function cursorConfig(mcpUrl: string, token: string | null) {
  const server: Record<string, unknown> = { url: mcpUrl }
  if (token) server.headers = { Authorization: `Bearer ${token}` }
  return JSON.stringify({ mcpServers: { "cognitive-enzyme": server } }, null, 2)
}

// ── System 1 网关 ─────────────────────────────────────────────

export function systemOneCurl(apiUrl: string, token: string | null) {
  const body = {
    // 整个请求体放在 shell 的单引号里,示例文本不能再含单引号
    state: "用户的 Python 脚本读取 CSV 时报 UnicodeDecodeError(utf-8 解码失败,字节 0xd6)",
    questions: {
      q1: {
        type: "noul",
        instructions: "解决这个问题需要处理文件编码吗?",
        criteria: { true: "需要指定或转换文件编码", false: "与文件编码无关" },
      },
    },
  }
  return [
    `curl -X POST ${apiUrl}/api/v1/systemone \\`,
    ...(token ? [`  -H "Authorization: Bearer ${token}" \\`] : []),
    '  -H "Content-Type: application/json" \\',
    `  -d '${JSON.stringify(body, null, 2).replace(/\n/g, "\n  ")}'`,
  ].join("\n")
}

export const SYSTEMONE_RESPONSE = JSON.stringify(
  {
    model: "jev",
    answers: { q1: { noul: 0.94 } },
    decision_id: "5f0c2d7e9a1b4c3d8e6f7a8b9c0d1e2f",
    routing: { path: [{ judge: "jev", ok: true, confident: true, latency_s: 0.412 }] },
  },
  null,
  2,
)

export function feedbackCurl(apiUrl: string, token: string | null) {
  const body = { decision_id: "5f0c2d7e9a1b4c3d8e6f7a8b9c0d1e2f", labels: { q1: true } }
  return [
    `curl -X POST ${apiUrl}/api/v1/systemone/feedback \\`,
    ...(token ? [`  -H "Authorization: Bearer ${token}" \\`] : []),
    '  -H "Content-Type: application/json" \\',
    `  -d '${JSON.stringify(body)}'`,
  ].join("\n")
}
