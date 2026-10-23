# Cognitive Enzyme backend (REST :8420 + MCP :8421;CE_API_PORT / CE_MCP_PORT 可改)
FROM python:3.12-slim

WORKDIR /srv/cognitive-enzyme

# 园丁的 docker CLI 不装进镜像:compose 会把宿主机的 /usr/bin/docker 与
# /var/run/docker.sock 一起只读挂载进来(见 docker-compose.yml)——镜像保持
# 自包含之外的唯一宿主耦合都集中在 compose 一处,也避开了墙外静态包下载。

# 国内网络可传 --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir -i $PIP_INDEX_URL .

# 园丁六阶段手册(/api/gardener 运行时读取)
COPY ops/gardener ./ops/gardener

EXPOSE 8420 8421

HEALTHCHECK --interval=15s --timeout=3s --retries=10 \
  CMD python -c "import urllib.request; import os; urllib.request.urlopen('http://localhost:%s/readyz' % os.getenv('CE_API_PORT', '8420'))" || exit 1

CMD ["python", "-m", "app"]
