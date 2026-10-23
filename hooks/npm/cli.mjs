#!/usr/bin/env node
/**
 * cognitive-enzyme-hook CLI — 一键安装/卸载 Claude Code hooks。
 *
 *   npx -y cognitive-enzyme-hook install [--project] [--url <CE_API_URL>]
 *                                        [--token <CE_API_TOKEN>] [--no-npm]
 *   npx -y cognitive-enzyme-hook uninstall [--project]
 *   npx -y cognitive-enzyme-hook status
 *
 * install：往 ~/.claude/settings.json（默认）或 ./.claude/settings.json
 * （--project）合并三条 hook：UserPromptSubmit → ce-prime-hook，
 * SessionEnd / PreCompact → ce-capture-hook。幂等——已有等效条目
 * （含仓库内绝对路径写法、Python 版 hook）则跳过。修改前备份 .bak。
 * ce-prime-hook 不在 PATH 时自动 `npm i -g`（--no-npm 关闭）。
 *
 * 零依赖：仅用 Node 18+ 内置模块。
 */

import { copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

const PKG = "cognitive-enzyme-hook";

// pattern 同时识别 registry bin、仓库内 mjs 绝对路径、Python 版 hook，
// 避免与已手工配置的等效条目重复注入
const HOOK_EVENTS = [
  { event: "UserPromptSubmit", command: "ce-prime-hook", timeout: 10,
    pattern: /ce[-_]prime[-_]hook|hooks[\\/]npm[\\/]index\.mjs/ },
  { event: "SessionEnd", command: "ce-capture-hook", timeout: 30,
    pattern: /ce[-_]capture[-_]hook|hooks[\\/]npm[\\/]capture\.mjs/ },
  { event: "PreCompact", command: "ce-capture-hook", timeout: 30,
    pattern: /ce[-_]capture[-_]hook|hooks[\\/]npm[\\/]capture\.mjs/ },
  { event: "PostToolUseFailure", command: "ce-failure-hook", timeout: 10,
    pattern: /ce[-_]failure[-_]hook|hooks[\\/]npm[\\/]failure\.mjs/ },
];

function fail(msg) {
  console.error(`[${PKG}] ${msg}`);
  process.exit(1);
}

function settingsPath(project) {
  const base = project ? process.cwd() : homedir();
  return join(base, ".claude", "settings.json");
}

function readSettings(path) {
  if (!existsSync(path)) return {};
  const raw = readFileSync(path, "utf8").replace(/^﻿/, "");
  if (!raw.trim()) return {};
  try {
    return JSON.parse(raw);
  } catch (e) {
    fail(`${path} 不是合法 JSON（${e.message}）——不做修改，请先手工修复。`);
  }
}

function writeSettings(path, settings) {
  mkdirSync(dirname(path), { recursive: true });
  if (existsSync(path)) copyFileSync(path, `${path}.bak`);
  writeFileSync(path, JSON.stringify(settings, null, 2) + "\n");
}

function matchedHooks(settings, spec) {
  const groups = (settings.hooks || {})[spec.event] || [];
  return groups.flatMap((g) => (g.hooks || []))
    .filter((h) => spec.pattern.test(String(h.command || "")));
}

function binOnPath(name) {
  const probe = process.platform === "win32" ? `where ${name}` : `command -v ${name}`;
  return spawnSync(probe, { shell: true, stdio: "ignore" }).status === 0;
}

// ── install ────────────────────────────────────────────────────

function install(flags) {
  const path = settingsPath(flags.project);
  const settings = readSettings(path);

  const added = [];
  const skipped = [];
  for (const spec of HOOK_EVENTS) {
    const existing = matchedHooks(settings, spec);
    if (existing.length) {
      skipped.push(`${spec.event}（已有: ${existing[0].command}）`);
      continue;
    }
    settings.hooks ||= {};
    settings.hooks[spec.event] ||= [];
    settings.hooks[spec.event].push({
      hooks: [{ type: "command", command: spec.command, timeout: spec.timeout }],
    });
    added.push(spec.event);
  }

  if (flags.url || flags.token) {
    settings.env ||= {};
    if (flags.url) settings.env.CE_API_URL = flags.url;
    if (flags.token) {
      settings.env.CE_API_TOKEN = flags.token;
      // 令牌会明文落进 settings.json,且刚经过 shell 历史:提醒用户注意轮换
      console.log(`[${PKG}] 提示:--token 已明文写入 settings.json,并可能留在 shell 历史里;` +
        `共享机器上建议装完后手动把令牌移到 ~/.config/cognitive-enzyme/hook.env(权限 600),` +
        `或泄露风险高时在后台「设置 → 访问与安全」轮换。`);
    }
  }

  if (added.length || flags.url || flags.token) {
    writeSettings(path, settings);
    console.log(`[${PKG}] 已写入 ${path}${existsSync(`${path}.bak`) ? "（原文件备份为 .bak）" : ""}`);
  }
  for (const e of added) console.log(`  + ${e}`);
  for (const s of skipped) console.log(`  = 跳过 ${s}`);

  // hook 命令依赖全局 bin；不在 PATH 时代为安装
  if (!flags.noNpm && !binOnPath("ce-prime-hook")) {
    console.log(`[${PKG}] 未检测到全局 bin，执行 npm i -g ${PKG} ...`);
    const r = spawnSync(`npm i -g ${PKG}`, { shell: true, stdio: "inherit" });
    if (r.status !== 0) {
      console.error(`[${PKG}] 全局安装失败——请手动执行: npm i -g ${PKG}`);
    }
  }

  console.log(`[${PKG}] 完成。重启 Claude Code（或执行一次 /hooks）后生效。`);
  if (!flags.url) {
    console.log(`  提示: 服务不在 http://localhost:8420 时,用 --url 指定,或在 settings.json env 里配 CE_API_URL。`);
  }
  if (!flags.token) {
    console.log(`  提示: 服务端开了鉴权的话,用 --token 写入 CE_API_TOKEN。`);
  }
}

// ── uninstall ──────────────────────────────────────────────────

function uninstall(flags) {
  const path = settingsPath(flags.project);
  if (!existsSync(path)) fail(`${path} 不存在，无需卸载。`);
  const settings = readSettings(path);

  let removed = 0;
  for (const spec of HOOK_EVENTS) {
    const groups = (settings.hooks || {})[spec.event];
    if (!groups) continue;
    for (const g of groups) {
      const before = (g.hooks || []).length;
      g.hooks = (g.hooks || []).filter((h) => !spec.pattern.test(String(h.command || "")));
      removed += before - g.hooks.length;
    }
    const kept = groups.filter((g) => (g.hooks || []).length);
    if (kept.length) settings.hooks[spec.event] = kept;
    else delete settings.hooks[spec.event];
  }
  if (settings.hooks && Object.keys(settings.hooks).length === 0) delete settings.hooks;

  if (!removed) {
    console.log(`[${PKG}] ${path} 中没有 CE hooks，无需卸载。`);
    return;
  }
  writeSettings(path, settings);
  console.log(`[${PKG}] 已从 ${path} 移除 ${removed} 条 hook（原文件备份为 .bak）。`);
  if (settings.env?.CE_API_TOKEN || settings.env?.CE_API_URL) {
    console.log(`  提示: env 中的 CE_API_URL / CE_API_TOKEN 未动，不需要请手工删除。`);
  }
  console.log(`  全局 bin 如需一并移除: npm rm -g ${PKG}`);
}

// ── status ─────────────────────────────────────────────────────

function status() {
  for (const [label, project] of [["全局", false], ["项目", true]]) {
    const path = settingsPath(project);
    console.log(`${label} ${path}${existsSync(path) ? "" : "（不存在）"}`);
    if (!existsSync(path)) continue;
    const settings = readSettings(path);
    for (const spec of HOOK_EVENTS) {
      const hit = matchedHooks(settings, spec);
      console.log(`  ${hit.length ? "✔" : "✘"} ${spec.event}${hit.length ? ` → ${hit[0].command}` : ""}`);
    }
    if (settings.env?.CE_API_URL) console.log(`  env CE_API_URL = ${settings.env.CE_API_URL}`);
    if (settings.env?.CE_API_TOKEN) console.log(`  env CE_API_TOKEN = （已设置）`);
  }
  console.log(`bin ce-prime-hook ${binOnPath("ce-prime-hook") ? "✔ 在 PATH" : "✘ 不在 PATH（npm i -g " + PKG + "）"}`);
}

// ── main ───────────────────────────────────────────────────────

const argv = process.argv.slice(2);
const cmd = argv[0];
const flags = { project: false, noNpm: false, url: "", token: "" };
for (let i = 1; i < argv.length; i++) {
  const a = argv[i];
  if (a === "--project") flags.project = true;
  else if (a === "--no-npm") flags.noNpm = true;
  else if (a === "--url") flags.url = argv[++i] || fail("--url 需要一个值");
  else if (a === "--token") flags.token = argv[++i] || fail("--token 需要一个值");
  else fail(`未知参数: ${a}`);
}

if (cmd === "install") install(flags);
else if (cmd === "uninstall") uninstall(flags);
else if (cmd === "status") status();
else {
  console.log(`用法:
  npx -y ${PKG} install [--project] [--url <CE_API_URL>] [--token <CE_API_TOKEN>] [--no-npm]
  npx -y ${PKG} uninstall [--project]
  npx -y ${PKG} status

install    往 ~/.claude/settings.json（--project 则为 ./.claude/settings.json）
           合并 prime/capture 三条 hook,幂等,修改前备份 .bak;
           全局 bin 缺失时自动 npm i -g（--no-npm 关闭）
uninstall  移除 CE 相关 hook 条目
status     查看全局/项目 hook 安装状态`);
  process.exit(cmd ? 1 : 0);
}
