#!/usr/bin/env bash
# 服务器一键部署:GHCR 登录 → 拉镜像 → compose up -d → 园丁补标签 → 等 /readyz
# 用法(服务器只需本仓库,不需要构建):
#   git clone https://github.com/your-org/Cognitive-Enzyme.git && cd Cognitive-Enzyme
#   GHCR_TOKEN=<read:packages PAT> bash scripts/deploy.sh
# 已在服务器上 docker login ghcr.io 的话 GHCR_TOKEN 可省。
# 国内主机 ghcr.io 不通时:CE_GHCR=ghcr.nju.edu.cn bash scripts/deploy.sh
# (公共 ghcr 代理只能拉公开镜像,私有包过不了鉴权就得推国内 registry)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

REGISTRY="${CE_GHCR:-ghcr.io}"
OWNER="${CE_GHCR_OWNER:-your-org}"
API_PORT="${CE_API_PORT:-8420}"
WEBUI_PORT="${CE_WEBUI_PORT:-13000}"

COMPOSE=(docker compose)

log() { printf '\033[1;36m[deploy]\033[0m %s\n' "$1"; }
die() { printf '\033[1;31m[deploy]\033[0m %s\n' "$1" >&2; exit 1; }

# ---- 0. 前置检查 ----
command -v docker >/dev/null || die "未找到 docker"
docker info >/dev/null 2>&1 || die "Docker 未运行"
# !reset 需要 compose v2.24+
docker compose version >/dev/null 2>&1 || die "未找到 docker compose 插件(需 v2.24+)"

GENERATED_ADMIN_PW=""
if [[ ! -f .env ]]; then
  cp .env.example .env
  # 初始管理员密码随机生成并写入 .env:不再有 admin/admin 默认口令——
  # 8420/8421/13000 默认绑 0.0.0.0,服务起在公网时默认口令等于门户大开
  GENERATED_ADMIN_PW="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20)"
  printf '\nCE_ADMIN_PASSWORD=%s\n' "$GENERATED_ADMIN_PW" >> .env
  log "已从 .env.example 生成 .env(数据库密码用默认值,可按需修改)"
fi

# ---- 1. GHCR 登录(私有包必须)----
if [[ -n "${GHCR_TOKEN:-}" ]]; then
  echo "$GHCR_TOKEN" | docker login "$REGISTRY" -u "${GHCR_USER:-$OWNER}" --password-stdin >/dev/null
  log "已登录 $REGISTRY"
fi

# ---- 2. 拉镜像 + 起栈 ----
log "拉取镜像 $REGISTRY/$OWNER/* ..."
"${COMPOSE[@]}" pull \
  || die "拉取失败:私有包需先 docker login $REGISTRY(read:packages PAT 经 GHCR_TOKEN 传入)"

log "启动服务(PostgreSQL + Qdrant + app + webui + gardener 镜像)..."
"${COMPOSE[@]}" up -d

# ---- 3. 就绪等待(首次启动自动建表 + 迁移)----
log "等待后端就绪(首次启动要跑数据库迁移)..."
ready=""
for _ in $(seq 1 45); do
  if curl -sf "http://127.0.0.1:$API_PORT/readyz" >/dev/null 2>&1; then ready=1; break; fi
  sleep 2
done
[[ -n "$ready" ]] || die "后端 90s 未就绪,docker compose logs app 排查"

if [[ -n "$GENERATED_ADMIN_PW" ]]; then
  ADMIN_HINT="admin / $GENERATED_ADMIN_PW(已写入 .env 的 CE_ADMIN_PASSWORD;请登录后立即修改)"
else
  ADMIN_HINT="你在 .env 的 CE_ADMIN_PASSWORD 里设的密码(未设置则见 app 启动日志里的随机初始密码)"
fi

cat <<EOF

部署完成:
  管理后台   http://<服务器IP>:$WEBUI_PORT   ($ADMIN_HINT)
  REST API   :$API_PORT  (docs: /docs)   MCP   :8421/mcp(首次启动已自动生成令牌,后台可见)

后台里还差两步:
  1. 模型 → 服务商与模型:配「对话 + 向量」模型
  2. 设置 → 访问与安全:生成 API 令牌给 hook 用
EOF
