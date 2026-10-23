#!/usr/bin/env bash
# 本地一键启动:PostgreSQL + Qdrant(docker compose)→ 后端(:8420/:8421)→ 管理后台(:13000)
# 对应 README「方式二:存储用 Docker,应用跑在本机」。配套 scripts/dev-down.sh 一键关闭。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PIDDIR="$ROOT/.dev-pids"
LOGDIR="$ROOT/logs"
mkdir -p "$PIDDIR" "$LOGDIR"

BACKEND_PORT="${CE_API_PORT:-8420}"
MCP_PORT="${CE_MCP_PORT:-8421}"
WEBUI_PORT="${CE_WEBUI_PORT:-13000}"

log() { printf '\033[1;36m[dev-up]\033[0m %s\n' "$1"; }
die() { printf '\033[1;31m[dev-up]\033[0m %s\n' "$1" >&2; exit 1; }

is_running() {
  local pidfile="$1"
  [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null
}

# ---- 0. 前置检查 ----
command -v docker >/dev/null || die "未找到 docker,请先安装 Docker Desktop"
docker info >/dev/null 2>&1 || die "Docker 未运行,请先启动 Docker Desktop"

if [[ ! -f .env ]]; then
  cp .env.example .env
  log "已从 .env.example 生成 .env(数据库密码 / 首个管理员用默认值,可按需修改)"
fi

# ---- 1. PostgreSQL + Qdrant ----
log "启动 PostgreSQL + Qdrant(docker compose up -d postgres qdrant)..."
docker compose up -d postgres qdrant

log "等待 PostgreSQL healthy..."
for _ in $(seq 1 30); do
  cid="$(docker compose ps -q postgres)"
  [[ -n "$cid" ]] && [[ "$(docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null)" == "healthy" ]] && break
  sleep 1
done
[[ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q postgres)" 2>/dev/null)" == "healthy" ]] \
  || die "PostgreSQL 未在 30s 内就绪,请用 docker compose logs postgres 排查"

# ---- 2. 选择 Python(项目虚拟环境)----
PYTHON=""
for cand in .venv/bin/python .venv-eval/bin/python; do
  if [[ -x "$cand" ]]; then PYTHON="$ROOT/$cand"; fi
done
if [[ -z "$PYTHON" ]]; then
  log "未找到虚拟环境,创建 .venv 并安装依赖..."
  python3 -m venv .venv
  .venv/bin/python -m pip install -q -e ".[dev]"
  PYTHON="$ROOT/.venv/bin/python"
fi

# ---- 3. 后端(REST :$BACKEND_PORT / MCP :$MCP_PORT)----
if is_running "$PIDDIR/backend.pid"; then
  log "后端已在运行(PID $(cat "$PIDDIR/backend.pid")),跳过"
else
  log "启动后端 (python -m app)..."
  CE_API_PORT="$BACKEND_PORT" CE_MCP_PORT="$MCP_PORT" nohup "$PYTHON" -m app >"$LOGDIR/backend.log" 2>&1 &
  echo $! > "$PIDDIR/backend.pid"
fi

# ---- 4. 管理后台(webui,:$WEBUI_PORT)----
if [[ ! -d webui/node_modules ]]; then
  log "webui 依赖未安装,执行 npm install..."
  (cd webui && npm install)
fi

if is_running "$PIDDIR/webui.pid"; then
  log "webui 已在运行(PID $(cat "$PIDDIR/webui.pid")),跳过"
else
  log "启动 webui (next dev --port $WEBUI_PORT)..."
  # 用 pushd 而不是 ( cd x && cmd & ) 子壳组合:后者里 && 和 & 混用时,
  # bash 会把整个 "cd && cmd" 列表当一个后台任务派生子壳,$! 拿到的是子壳 PID
  # 而不是 next dev 本身,导致后续按 PID 关闭时找错进程树。
  pushd webui >/dev/null
  CE_BACKEND_URL="http://127.0.0.1:$BACKEND_PORT" \
    nohup node_modules/.bin/next dev --port "$WEBUI_PORT" >"$LOGDIR/webui.log" 2>&1 &
  echo $! > "$PIDDIR/webui.pid"
  popd >/dev/null
fi

# ---- 5. 健康检查 ----
log "等待后端 /health..."
for _ in $(seq 1 30); do
  curl -sf "http://127.0.0.1:$BACKEND_PORT/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -sf "http://127.0.0.1:$BACKEND_PORT/health" >/dev/null 2>&1 \
  || log "警告:后端 /health 未在 30s 内响应,查看 logs/backend.log"

log "等待 webui..."
for _ in $(seq 1 30); do
  curl -sf "http://127.0.0.1:$WEBUI_PORT" >/dev/null 2>&1 && break
  sleep 1
done

cat <<EOF

启动完成:
  管理后台   http://localhost:$WEBUI_PORT
  后端 REST  http://localhost:$BACKEND_PORT (docs: /docs)
  后端 MCP   http://localhost:$MCP_PORT/mcp

日志: logs/backend.log  logs/webui.log
关闭: scripts/dev-down.sh
EOF
