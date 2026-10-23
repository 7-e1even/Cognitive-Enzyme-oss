"""Docker Engine HTTP API 共享层 — 园丁的一次性 CC 容器运行器。

为什么直连 API 而不是 docker CLI:本机(尤其 Windows 开发机)常常没有 docker,
而目标 docker 主机在远端;Engine API 只是普通 HTTP(本机 socket 走 UDS,远程走
tcp://),httpx 就够。由此园丁的「容器模式」在任何后端宿主上都可用。

一次性 CC 容器的运行协议(run_container_stream):
- create → start → logs(follow,流式解帧) → wait → 强制删除(finally 兜底);
- 提示词作为**命令行参数**传入(claude -p 接受位置参数)——Engine API 的
  attach-stdin 需要连接劫持(hijack),httpx 做不了,也没必要;
- 凭证经容器 Env 注入(在 create 的 JSON body 里,不落命令行/ps)。
"""

from __future__ import annotations

import json
import logging

import httpx

logger = logging.getLogger(__name__)

DEFAULT_UDS = "/var/run/docker.sock"


def client_for(docker_host: str) -> httpx.AsyncClient:
    """按地址构造 Engine API 客户端。留空/unix:// 走 UDS,tcp:// 走 HTTP。

    trust_env=False:Engine API 是内网/本机管理面,绝不能被 HTTP(S)_PROXY
    环境变量劫持到系统代理上(代理一般也转发不了 docker 主机)。
    """
    timeout = httpx.Timeout(15.0, connect=5.0)
    if not docker_host or docker_host.startswith("unix://"):
        uds = docker_host[len("unix://"):] or DEFAULT_UDS
        transport = httpx.AsyncHTTPTransport(uds=uds)
        return httpx.AsyncClient(transport=transport, base_url="http://docker",
                                 timeout=timeout, trust_env=False)
    base = ("http://" + docker_host[len("tcp://"):] if docker_host.startswith("tcp://")
            else docker_host)
    return httpx.AsyncClient(base_url=base.rstrip("/"), timeout=timeout,
                             trust_env=False)


def engine_error(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("message", resp.text))
    except (json.JSONDecodeError, ValueError):
        return resp.text


class LogDemuxer:
    """docker 日志 multiplex 流的**增量**解帧器(帧可能跨 chunk 撕裂)。

    帧头 8 字节: [stream_type(0/1/2), 0, 0, 0, size(4B big-endian)]。
    首个字节不合帧格式则整条流按原样文本处理(tty 容器)。
    """

    def __init__(self):
        self.buf = b""
        self.raw_mode = False

    def feed(self, chunk: bytes) -> str:
        self.buf += chunk
        if self.raw_mode:
            out, self.buf = self.buf, b""
            return out.decode("utf-8", errors="replace")
        parts: list[bytes] = []
        while len(self.buf) >= 8:
            if self.buf[0] not in (0, 1, 2) or self.buf[1:4] != b"\x00\x00\x00":
                self.raw_mode = True
                parts.append(self.buf)
                self.buf = b""
                break
            size = int.from_bytes(self.buf[4:8], "big")
            if len(self.buf) < 8 + size:
                break  # 帧未收全,等下一个 chunk
            parts.append(self.buf[8:8 + size])
            self.buf = self.buf[8 + size:]
        return b"".join(parts).decode("utf-8", errors="replace")

    def flush(self) -> str:
        """流结束后取出残余:被截断帧的已到载荷照常吐出,不完整帧头丢弃。"""
        if self.raw_mode:
            rest = self.buf
        elif len(self.buf) > 8:
            rest = self.buf[8:]
        else:
            rest = b""
        self.buf = b""
        return rest.decode("utf-8", errors="replace")


async def remove_container(docker_host: str, name: str) -> None:
    """强制删除容器,不存在/主机不可达也不报错(清残留/终止运行都走这里)。"""
    try:
        async with client_for(docker_host) as client:
            await client.delete(f"/containers/{name}", params={"force": "true"})
    except Exception:  # noqa: BLE001 — UDS 在 Windows 上抛 AttributeError 而非 httpx 异常
        logger.warning("remove_container %s failed", name, exc_info=True)


class ContainerRunError(Exception):
    """create/start 阶段的失败(镜像不存在、主机连不上等),给上层转成用户可读错误。"""


async def run_container_stream(
    docker_host: str,
    *,
    image: str,
    cmd: list[str],
    env: dict[str, str],
    binds: list[str],
    name: str,
    workdir: str = "/workspace",
    extra_hosts: list[str] | None = None,
    result: dict | None = None,
):
    """跑一个一次性容器,流式产出解码后的输出文本片段;结束后强制删除。

    传入 result dict 可拿到退出码(result["exit_code"],流正常走完才有)。
    取消(客户端断开/stop)时 finally 兜底删容器,远端不残留。
    """
    async with client_for(docker_host) as client:
        # 清同名残留,避免 name 冲突
        await client.delete(f"/containers/{name}", params={"force": "true"})

        host_config: dict = {"Binds": binds}
        if extra_hosts:
            host_config["ExtraHosts"] = extra_hosts
        body = {
            "Image": image,
            "Cmd": cmd,
            "Env": [f"{k}={v}" for k, v in env.items() if v],
            "WorkingDir": workdir,
            "HostConfig": host_config,
        }
        resp = await client.post("/containers/create", params={"name": name}, json=body)
        if resp.status_code != 201:
            raise ContainerRunError(f"创建容器失败: {engine_error(resp)}")
        cid = resp.json()["Id"]

        try:
            resp = await client.post(f"/containers/{cid}/start")
            if resp.status_code not in (204, 304):
                raise ContainerRunError(f"启动容器失败: {engine_error(resp)}")

            demux = LogDemuxer()
            async with client.stream(
                "GET", f"/containers/{cid}/logs",
                params={"follow": "true", "stdout": "true", "stderr": "true"},
                timeout=httpx.Timeout(None, connect=5.0),  # follow 流不设读超时
            ) as log_resp:
                async for chunk in log_resp.aiter_bytes():
                    text = demux.feed(chunk)
                    if text:
                        yield text
            tail = demux.flush()
            if tail:
                yield tail

            if result is not None:
                wait_resp = await client.post(f"/containers/{cid}/wait")
                if wait_resp.status_code == 200:
                    result["exit_code"] = wait_resp.json().get("StatusCode")
        finally:
            try:
                await client.delete(f"/containers/{cid}", params={"force": "true"})
            except Exception:  # noqa: BLE001 — 与 remove_container 同理:UDS 平台差异不只抛 httpx 异常
                logger.warning("cleanup container %s failed", name, exc_info=True)
