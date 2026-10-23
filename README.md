# Cognitive Enzyme

**A System 1 for AI agents: instead of hoping the model remembers to search, put the right experience into the context before the task starts.**

English | [简体中文](README.zh-CN.md)

Agents solve every task as if it were the first. The pitfall you hit last week, the bypass you found, the project convention that never made it into the docs: the next run starts from zero. Cognitive Enzyme stores such experience as small rules ("enzymes"), injects them automatically at the right moment, and gets more accurate as feedback arrives about whether each one actually helped.

Slides (Chinese): [Tencent Security Salon #8, Beijing — an Agentic vulnerability-research practice built on the "cognitive enzyme" self-evolving architecture](https://github.com/Yeti-791/Tsec-Salon/blob/main/%E8%85%BE%E8%AE%AF%E5%AE%89%E5%85%A8%E6%B2%99%E9%BE%99%E7%AC%AC8%E6%9C%9F%EF%BC%88%E5%8C%97%E4%BA%AC%E7%AB%99%EF%BC%89/%E5%9F%BA%E4%BA%8E%E2%80%9C%E8%AE%A4%E7%9F%A5%E9%85%B6%E2%80%9D%E8%87%AA%E8%BF%9B%E5%8C%96%E6%9E%B6%E6%9E%84%E7%9A%84%E6%BC%8F%E6%B4%9E%E6%8C%96%E6%8E%98Agentic%E5%AE%9E%E8%B7%B5-%E7%AC%A6%E8%8A%8A%E7%BA%A2.pdf)

![Architecture: prime, failure and capture hooks feed a recall, judge and inject pipeline and a learning loop, backed by PostgreSQL, Qdrant and model providers](docs/img/architecture.svg)

## Why

| Problem | What it looks like | What Cognitive Enzyme does |
|---|---|---|
| Tacit knowledge is never written down | The model does not know *how* to write a file through Jackson in a given setup, so it takes the generic path | Stores it as a rule with "when to use" and "what to do", and injects it **passively** before the task, so the model is not left to guess |
| Models give up early | On a dead end they declare "not exploitable"; one answer is enough for them | A rule can carry process constraints such as "try at least two more bypasses before giving up" |
| Active search gets skipped | The model forgets to search, or searches with the wrong words | Hooks retrieve before the prompt and again on a tool error, with no cooperation from the model |
| Skill piles grow without bound | Dozens of fixed skills slow the model and overfit it | Only the few rules activated by the current situation are injected, then pruned by feedback |

## How it works

Two ideas are borrowed:

- **Dual-process theory** (*Thinking, Fast and Slow*). System 1 is fast, automatic pattern matching; System 2 is slow and deliberate. An LLM's reasoning is System 2. This project adds the missing System 1: "this situation reminds me of that lesson".
- **Cue → Routine → Reward**, the structure of an enzyme. Each memory has a *when* (the triggering situation) and a *how* (the behavior). The learning signal is not the reward itself but the **difference between reward and expectation**, a dopamine-style error: help reinforces, harm weakens.

One cycle:

1. **prime (before the prompt).** Retrieve with the current situation: vector recall, BM25 lexical recall and matches against past situations; rerank; then an **applicability judge** scores each candidate with "probability this is directly useful for the request" and decides which memories to inject and how many. Injecting nothing is a valid outcome.
2. **failure (on a tool error).** Retrieve again with the raw error text and surface fixes that worked before.
3. **capture (session end or before compaction).** Extract "error → fix" experience from the session, pass it through a quality gate, dedupe it and file it under a domain.
4. **feedback and consolidation.** The agent reports whether an injection helped; memories are reinforced or weakened. A scheduled job merges duplicates, quarantines weak ones and produces unverified hypotheses (dream, mutation, crossover) that get a small exploration budget.

Unlike people, **there is no time decay**: an agent does not need to forget, only to keep what matters. Memories are quarantined by quality (zombies that keep failing, contradictions), not by age.

## Quick start

Requires Python 3.11+, Node.js 20.9+ and Docker (for PostgreSQL and Qdrant).

```bash
cp .env.example .env

# Option 1: full stack in Docker (set CE_GHCR_OWNER to the account or org that hosts the images)
CE_GHCR_OWNER=<your-account> bash scripts/deploy.sh

# Option 2: storage in containers, app on the host (development)
bash scripts/dev-up.sh
```

Open <http://localhost:13000>. The admin user is `admin`; set the password with `CE_ADMIN_PASSWORD`, otherwise a random one is generated on first start and printed in the server log. Then:

1. Change the password (account settings).
2. **Models → Providers and models**: add a provider and models. A chat model and an embedding model are required; rerank and judge are optional and degrade gracefully.
3. **Models → Bindings**: assign a model to each stage.
4. **Settings → Access and security**: create a static API token for the hooks.

API docs: <http://localhost:8420/docs>. Health: `/health`, `/readyz`.

## Connect an agent

| Agent | Mechanism | Install |
|---|---|---|
| Claude Code | hooks (prime / failure / capture) | `npx -y cognitive-enzyme-hook install --url http://<server>:8420 --token <API token>`; see [`hooks/npm/README.md`](hooks/npm/README.md) |
| pi | extension in `hooks/pi/` | `ln -s $PWD/hooks/pi ~/.pi/agent/extensions/cognitive-enzyme`; see [`hooks/pi/README.md`](hooks/pi/README.md) |
| Any agent | REST / MCP | `POST /api/prime` (with `source`, `cwd`, `project`); MCP at `:8421/mcp` exposes `prime`, `search`, `recall`, `report`, `harvest` |

Every integration is **fail-open**: if the backend is down, slow or errors, the hook silently does nothing and never blocks the agent.

## Evaluation: what we measured, and what it does not show

`tests/eval/` holds recall benchmarks and a framework for the harder question: *does an injected memory make the model answer correctly?* The numbers below come from one experiment on competition math and physics problems that a model had already failed twice closed-book (147 questions; 64 of them have an annotated correct memory, 83 question-memory pairs). Reproducing it needs the datasets, which are not shipped (see the scripts in `tests/eval/hardbench` and `tests/eval/diagnostic`).

**1. Recall: the right memory was mostly lost before the judge ever saw it.**

With the default settings the judge's request budget is spent on the first few candidates, so the judge sees a median of 4 candidates out of a nominal pool of 36. Of the 65 annotated memories that reached the candidate pool, only 7 were judged.

![Of 83 annotated correct memories, 65 reach the candidate pool; with default settings 7 are judged and 6 injected, judging the whole pool gives 50 and 26, and the full set of changes gives 52 and 31](docs/img/recall-funnel.svg)

Judging the whole pool (`judge.two_stage` with `judge.two_stage_compare=joint` and `judge.scan_pool=36`) raises "injected" from 6 to 26. Adding an RRF-built pool, no rank cut-off at rerank and wider recall (`judge.pool_order=rrf`, `ranking.rerank_keep=floor`, `recall.dense_limit=100`, `recall.cap=120`) reaches 31. The cost is more judge calls (13 to 17 per prime instead of 1) and more injected memories per question (0.58 before, 1.35 and 2.14 after).

**2. Answers: injecting more did not make the model answer better.**

For the same questions, a solver (MiniMax M3.1 Flash; the same model grades) answered closed-book and again with each injection setting.

![Questions that flipped against a closed-book answer: annotated memory rescued 1 and harmed 3; default settings 1 and 4; judging the whole pool 2 and 5; all changes 3 and 7](docs/img/answers.svg)

- Even handing the model the **annotated correct memory** rescued 1 question and harmed 3 (n = 63).
- Injecting more memories rescued slightly more and harmed more. No difference is statistically significant (McNemar p between 0.34 and 0.63).
- Closed-book answers are themselves noisy: in earlier runs of a different model (GLM-5.3), 56 of 353 physics questions (16%) flipped between two identical closed-book attempts. These counts sit inside that kind of noise.

What this supports: **recall can be made far more complete, but on these hard problems a retrieved memory is not the same as a useful one.** The memories we have for them are mostly method hints, and a hint does not carry a model past a problem it cannot solve. What it does not show: that injection does not help in general. This was one solver, one judge model, a hard problem set and small counts. Treat the recall change as ready to try and the answer-level effect as unproven.

Other caveats worth knowing:

- Recall is limited by several count-based cut-offs in the pipeline (rerank keep, judge pool, the judge's per-request token budget). Memories with long bodies hit them sooner. The relevant knobs are under `ranking.*`, `judge.*` and `recall.*`.
- The new recall options default to the previous behavior. Nothing changes until you turn them on.
- Datasets (competition problems, public corpora) are not distributed with this repository. Build them with the scripts and follow each dataset's license.

## Configuration lives in the console

About 100 parameters for models, recall, judging and learning are edited in **Settings → Runtime** and take effect on save. Environment variables are only for what is needed before the database is reachable: PostgreSQL, the first admin and the Qdrant address.

To add a parameter, add one entry to `SPECS_LIST` in [`app/settings.py`](app/settings.py); it appears in the console automatically and is read in code with `settings.get("<key>")`.

## Repository layout

| Path | Contents |
|---|---|
| `app/` | Backend: `matching/` recall and judging, `learning/` the learning loop (capture, consolidation, dreams), `distill/` decision records and teacher labels, `routers/` REST |
| `hooks/` | Claude Code hooks (Python and npm package) and the pi extension |
| `webui/` | Admin console (Next.js + shadcn/ui): memories, recall debugging, evolution events, model bindings, runtime settings, audit |
| `ops/gardener/` | Nightly gardener: a headless agent in a container that re-checks memories against the codebase |
| `scripts/` | Deployment, local development, evaluation and diagnostic scripts |
| `tests/` | Unit tests; `tests/eval/` holds the recall and effect benchmarks |

## Development

```bash
python -m pytest tests --ignore=tests/eval     # backend unit tests
node --test hooks/pi/                          # pi extension tests
cd webui && npm run typecheck && npm run lint  # frontend
```

## Security

- The initial admin password is random (or set by `CE_ADMIN_PASSWORD`); change it after first login. Static API tokens only open the data-plane endpoints and can be rotated in the console. The MCP server generates a token on first start and stays closed without one.
- Login is rate-limited (10 attempts per IP and account per 5 minutes); disabling a user or changing a password invalidates old JWTs immediately.
- `docker-compose.yml` mounts the Docker socket into the app for the nightly gardener. Access to that socket is root on the host, which is acceptable only for a single-user internal deployment. Delete the two mount lines if you do not need the gardener.
- `.env`, `webui/.env.local` and `~/.config/cognitive-enzyme/hook.env` are never committed.

## License

[GNU Affero General Public License v3.0 or later](LICENSE) (AGPL-3.0-or-later).

If you modify this software and offer it as a network service to others, you must make the complete modified source available to those users. The admin console uses only open-source components such as shadcn/ui (MIT) and lucide-react (ISC).
