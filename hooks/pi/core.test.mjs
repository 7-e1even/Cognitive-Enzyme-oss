import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { buildDigest, formatBlock, makeConfig, parseEnvFile, seenIds, toolSummary } from "./core.mjs";
import { formatBlock as npmFormatBlock } from "../npm/common.mjs";

const here = dirname(fileURLToPath(import.meta.url));

function userMsg(text, id = "u1") {
  return { type: "message", id, message: { role: "user", content: text } };
}
function assistantMsg(blocks, id) {
  return { type: "message", id, message: { role: "assistant", content: blocks } };
}
function toolResult(toolCallId, toolName, text, isError, id) {
  return {
    type: "message",
    id,
    message: {
      role: "toolResult",
      toolCallId,
      toolName,
      isError,
      content: [{ type: "text", text }],
    },
  };
}

const ENTRIES = [
  userMsg("帮我跑下测试", "m1"),
  assistantMsg(
    [
      { type: "thinking", thinking: "先看看项目结构" },
      { type: "text", text: "我来跑 npm test" },
      { type: "toolCall", id: "c1", name: "bash", arguments: { command: "npm test" } },
    ],
    "m2",
  ),
  toolResult(
    "c1",
    "bash",
    "Command exited with code 1\nError: Cannot find module 'express'",
    true,
    "m3",
  ),
  assistantMsg(
    [{ type: "toolCall", id: "c2", name: "read", arguments: { file_path: "/tmp/x.js" } }],
    "m4",
  ),
  toolResult("c2", "read", "file body", false, "m5"),
  assistantMsg(
    [
      { type: "toolCall", id: "c3", name: "bash", arguments: { command: "npm install express && npm test" } },
      { type: "text", text: "装上依赖再试" },
    ],
    "m6",
  ),
  // failure 注入块追加在 toolResult 文本里:成功的 bash 只出 [tool_ok],块不进摘要
  toolResult("c3", "bash", "tests pass\n\n⟦CE:BEGIN⟧\n🟢 [90%] 装依赖  ‹enzyme_id=enz9›\n⟦CE:END⟧", false, "m7"),
  // prime 注入块 → SEEN 行
  {
    type: "custom_message",
    id: "cm1",
    customType: "cognitive-enzyme",
    content: "⟦CE:BEGIN⟧\n[认知酶]…\n🟢 [80%] 经验  ‹enzyme_id=enz1›\n⟦CE:END⟧",
    display: false,
  },
  // 报错文本里的注入块也要压成 SEEN
  toolResult("c4", "bash", "boom\n⟦CE:BEGIN⟧\n🟢 [70%] x  ‹enzyme_id=enz8›\n⟦CE:END⟧", true, "m8"),
  { type: "compaction", id: "cp1", summary: "之前我们修了依赖问题", firstKeptEntryId: "m7", tokensBefore: 123 },
];

test("buildDigest: 工具痕迹、SEEN 压缩、compaction 摘要", () => {
  const d = buildDigest(ENTRIES);
  assert.ok(d.includes("[user] 帮我跑下测试"));
  assert.ok(d.includes("[assistant] 我来跑 npm test"));
  assert.ok(!d.includes("先看看项目结构"), "thinking 不进摘要");
  assert.ok(d.includes("[tool_error] bash: npm test\nCommand exited with code 1"));
  assert.ok(d.includes("Cannot find module 'express'"));
  assert.ok(d.includes("[tool_ok] bash: npm install express && npm test"));
  assert.ok(!d.includes("file body"), "非 bash 成功结果不出现");
  assert.ok(!d.includes("enz9"), "成功 bash 的内嵌注入块不进摘要");
  assert.ok(d.includes("[user] ⟦CE:SEEN enz1⟧"), "custom_message 压成 SEEN");
  assert.ok(d.includes("⟦CE:SEEN enz8⟧"), "报错文本里的注入块压成 SEEN");
  assert.ok(d.includes("(之前对话的摘要) 之前我们修了依赖问题"));
});

test("buildDigest: turn 与字数上限", () => {
  const many = [];
  for (let i = 0; i < 10; i++) many.push(userMsg(`第${i}轮`, `u${i}`));
  const d3 = buildDigest(many, 3);
  const parts = d3.split("\n\n");
  assert.equal(parts.length, 3);
  assert.ok(parts[0].includes("第0轮"), "保留开头");
  assert.ok(parts[2].includes("第9轮"), "保留结尾");
  const long = buildDigest([userMsg("x".repeat(500), "a"), userMsg("y".repeat(500), "b")], 400, 200);
  assert.ok(long.includes("…(中间省略)…"));
  assert.ok(long.length <= 230);
});

test("seenIds: 只取最近一次 compaction 之后的注入", () => {
  const entries = [
    {
      type: "custom_message",
      id: "old",
      customType: "cognitive-enzyme",
      content: "⟦CE:BEGIN⟧\nx  ‹enzyme_id=before1›\n⟦CE:END⟧",
      display: false,
    },
    { type: "compaction", id: "cp", summary: "s", firstKeptEntryId: "x", tokensBefore: 1 },
    {
      type: "custom_message",
      id: "new",
      customType: "cognitive-enzyme",
      content: "⟦CE:BEGIN⟧\nx  ‹enzyme_id=after1›  ‹enzyme_id=after1›\n⟦CE:END⟧",
      display: false,
    },
    toolResult("c9", "bash", "err\n⟦CE:BEGIN⟧\nx  ‹enzyme_id=after2›\n⟦CE:END⟧", true, "m9"),
  ];
  assert.deepEqual(seenIds(entries).sort(), ["after1", "after2"]);
  assert.deepEqual(seenIds(ENTRIES), [], "ENTRIES 以 compaction 结尾,之后无注入");
  const noCompact = [
    {
      type: "custom_message",
      id: "cm",
      customType: "cognitive-enzyme",
      content: "⟦CE:BEGIN⟧\nx  ‹enzyme_id=enzA›\n⟦CE:END⟧",
      display: false,
    },
    toolResult("c8", "bash", "err\n⟦CE:BEGIN⟧\nx  ‹enzyme_id=enzB›\n⟦CE:END⟧", true, "m8"),
  ];
  assert.deepEqual(seenIds(noCompact).sort(), ["enzA", "enzB"], "无 compaction 时全量");
  assert.deepEqual(seenIds([]), []);
});

test("toolSummary: command > file_path > path > JSON", () => {
  assert.equal(toolSummary({ command: "npm\n\ttest" }), "npm test");
  assert.equal(toolSummary({ file_path: "/a/b.js" }), "/a/b.js");
  assert.equal(toolSummary({ path: "/a/c.js" }), "/a/c.js");
  assert.equal(toolSummary({ url: "http://x" }), '{"url":"http://x"}');
  assert.equal(toolSummary({ command: "y".repeat(300) }).length, 200);
  assert.equal(toolSummary(null), "{}");
});

test("parseEnvFile: 注释/空行/引号", () => {
  const text = [
    "# 注释行",
    "",
    "CE_API_TOKEN=abc123",
    'CE_API_URL="http://x:9000/"',
    "CE_DOMAIN='py'",
    "  # 缩进注释",
    "CE_PRIME_MAX = 5",
    "NOEQUALS",
    "=novalue",
  ].join("\n");
  assert.deepEqual(parseEnvFile(text), {
    CE_API_TOKEN: "abc123",
    CE_API_URL: "http://x:9000/",
    CE_DOMAIN: "py",
    CE_PRIME_MAX: "5",
  });
  assert.deepEqual(parseEnvFile(""), {});
  assert.deepEqual(parseEnvFile(undefined), {});
});

test("makeConfig: env > 文件 > 默认", () => {
  const cfg = makeConfig({ A: "file-a", B: "file-b" }, { A: "env-a" });
  assert.equal(cfg("A", "d"), "env-a");
  assert.equal(cfg("B", "d"), "file-b");
  assert.equal(cfg("C", "d"), "d");
  assert.equal(makeConfig({}, { X: "" })("X", "d"), "", "env 显式空串也算设置");
  assert.equal(makeConfig()("K", "v"), "v", "无文件无 env → 默认");
});

test("formatBlock: 与 hooks/npm/common.mjs 逐字一致", () => {
  const matches = [
    { id: "e1", confidence: 0.9, severity: "critical", suggestion: "先检查依赖", reason: "同症状" },
    { id: "e2", confidence: 0.5, severity: "info", suggestion: "假设经验", exploratory: true },
    { id: "e3", confidence: 0.4, severity: "info", suggestion: "先做数量级检查", scope: "general" },
  ];
  const header = "[认知酶 · System 1 直觉] 以下反射被当前上下文激活，供参考（无关可忽略）：";
  const rendered = formatBlock(matches, header);
  assert.equal(rendered, npmFormatBlock(matches, header));
  assert.match(rendered, /🧭 \[方法论提示 40%\] 先做数量级检查/);
  assert.match(rendered, /标 🧭 者为通用方法论提示：可尝试、可忽略，不是事实断言。/);
});
