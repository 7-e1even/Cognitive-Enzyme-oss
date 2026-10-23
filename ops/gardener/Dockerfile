# CC 容器载具:node + headless Claude Code + CE hooks(园丁一次性跑)。
# 构建(上下文=仓库根,要 COPY hooks/npm):
#   docker build -t ce-gardener -f ops/gardener/Dockerfile .
# 运行由后端 /api/gardener/run 或计划任务拼装。
FROM node:20-slim

# 国内网络可传 --build-arg NPM_REGISTRY=https://registry.npmmirror.com
ARG NPM_REGISTRY=https://registry.npmjs.org

# CE hook 装仓库内版本(与服务端协议同步演进,不等 npm 发布):
# prime 注入 + capture 提炼,让容器里的 CC 会话也喂学习闭环。
# hook 未配 CE_API_URL 或 CE 不可达时静默退出,不影响对话。
COPY hooks/npm /opt/ce-hook
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g --registry=$NPM_REGISTRY @anthropic-ai/claude-code /opt/ce-hook

# 模型/端点全部经环境变量注入(MonkeyCode claude.tmpl 思路),镜像本身无凭证。
ENV CLAUDE_CODE_ENABLE_TELEMETRY=0 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    API_TIMEOUT_MS=600000

WORKDIR /workspace
ENTRYPOINT ["claude"]
