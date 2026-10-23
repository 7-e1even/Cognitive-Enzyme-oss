#!/usr/bin/env bash
# 关闭 scripts/dev-up.sh 启动的本地环境:webui / 后端进程 + docker compose stop(保留数据卷)。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PIDDIR="$ROOT/.dev-pids"

log() { printf '\033[1;36m[dev-down]\033[0m %s\n' "$1"; }

# next dev 等会 fork 出子进程(如 next-server),只杀记录的 PID 会留下孤儿进程,
# 所以这里递归杀掉整棵子进程树,而不是按端口杀(端口上可能跑着别的项目)。
kill_tree() {
  local pid="$1" sig="$2" child
  for child in $(pgrep -P "$pid" 2>/dev/null); do
    kill_tree "$child" "$sig"
  done
  kill "-$sig" "$pid" 2>/dev/null
}

stop_pid() {
  local name="$1" pidfile="$PIDDIR/$1.pid"
  [[ -f "$pidfile" ]] || return 0
  local pid; pid="$(cat "$pidfile")"
  if kill -0 "$pid" 2>/dev/null; then
    kill_tree "$pid" TERM
    for _ in $(seq 1 10); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    kill -0 "$pid" 2>/dev/null && kill_tree "$pid" KILL
    log "已停止 $name (PID $pid 及其子进程)"
  fi
  rm -f "$pidfile"
}

stop_pid webui
stop_pid backend

log "停止 PostgreSQL + Qdrant 容器(docker compose stop,数据卷保留)..."
docker compose stop

log "完成"
