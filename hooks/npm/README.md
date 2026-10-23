# cognitive-enzyme-hook

A [Claude Code](https://claude.com/claude-code) `UserPromptSubmit` hook that turns
[Cognitive Enzyme](../../README.md) into a **System 1 push layer**: before every
prompt reaches the model, it calls the Cognitive Enzyme server's `/api/prime` and
injects the activated reflexes as context.

> MCP's `prime()` is a *pull* (the agent must decide to call it). This hook makes it a
> *push*: the right intuitions appear automatically, every turn.

**It never blocks your prompt.** If the server is down, unreachable, times out, or
returns an error, the hook silently exits without injecting anything.

## Install

One-liner — wires `~/.claude/settings.json` and installs the global bins if missing:

```bash
npx -y cognitive-enzyme-hook install
```

Options: `--project` (write `./.claude/settings.json` instead), `--url <CE_API_URL>`
and `--token <CE_API_TOKEN>` (written into `env`), `--no-npm` (skip the global bin
install). It is idempotent — existing equivalent hook entries (including
absolute-path setups) are detected and skipped, and the previous settings file is
backed up as `settings.json.bak`. Also available: `uninstall` and `status`.

Manual alternative:

```bash
npm i -g cognitive-enzyme-hook   # or from a clone: npm i -g ./hooks/npm
```

then add the configuration below yourself.

## Configure

Add to `~/.claude/settings.json` (or a project `.claude/settings.json`):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "ce-prime-hook", "timeout": 10 }] }
    ],
    "SessionEnd": [
      { "hooks": [{ "type": "command", "command": "ce-capture-hook", "timeout": 30 }] }
    ],
    "PreCompact": [
      { "hooks": [{ "type": "command", "command": "ce-capture-hook", "timeout": 30 }] }
    ],
    "PostToolUseFailure": [
      { "hooks": [{ "type": "command", "command": "ce-failure-hook", "timeout": 10 }] }
    ]
  },
  "env": {
    "CE_API_URL": "http://localhost:8420",
    "CE_API_TOKEN": "<your machine token>"
  }
}
```

`ce-capture-hook` extracts reusable memories from the finished session's
transcript via the server's `/api/capture` (System 2 write path); it fires on
`SessionEnd` and `PreCompact` and never blocks. The transcript digest also keeps
real tool outcomes — failed tool calls show up as `[tool_error]` lines and later
successful Bash commands as `[tool_ok]` lines, so "error → fixed" episodes can be
extracted into `kind:fix` memories.

`ce-failure-hook` fires on `PostToolUseFailure` (the moment a tool call actually
fails): it sends the error to `/api/prime` with `source="failure"` and injects
matching past fixes in the same ⟦CE:…⟧ block format. Same never-block behaviour;
`is_interrupt` and empty errors are skipped silently.

Then open `/hooks` once (or restart Claude Code) to load it.

> Zero-install alternative: use `"command": "npx", "args": ["-y", "cognitive-enzyme-hook"]`.
> Convenient, but `npx` resolves the package on **every** prompt — adds noticeable latency
> on the prompt critical path. Prefer the global install above.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `CE_API_URL` | `http://localhost:8420` | Cognitive Enzyme REST base URL |
| `CE_API_TOKEN` | — | Static machine token (required when the server has auth enabled; admin console → 设置 → 访问与安全) |
| `CE_PRIME_TIMEOUT` | `9` | Prime request timeout, seconds |
| `CE_PRIME_MAX` | `6` | Max reflexes injected per prompt (token control) |
| `CE_PRIME_MIN_CONF` | `0.25` | Minimum enzyme confidence to inject (exploratory items are exempt) |
| `CE_DOMAIN` | — | Restrict recall to one domain |
| `CE_PROJECT` | — | Explicit project id (otherwise derived from the working directory) |
| `CE_CAPTURE_TIMEOUT` | `5` | Capture request timeout, seconds (the endpoint returns immediately) |
| `CE_CAPTURE_MAX_CHARS` | `200000` | Transcript excerpt limit sent to capture (keeps the start and most of the end; the server clips again per its `learning.capture_chars` setting) |
| `CE_CAPTURE_TURNS` | `400` | Max turns in the capture excerpt (the first two plus the most recent) |
| `CE_FAILURE_TIMEOUT` | `9` | Failure-hook request timeout, seconds |
| `CE_FAILURE_MAX` | `3` | Max past-fix memories injected on a tool failure |

## Requirements

- Node.js ≥ 18 (uses built-in `fetch`)
- A running Cognitive Enzyme server (`python -m app`)

## License

AGPL-3.0-or-later
