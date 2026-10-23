"""Pydantic request models for REST API."""

from pydantic import BaseModel, Field


class PrimeRequest(BaseModel):
    context: str = Field(max_length=200_000)
    domain: str = ""
    max_results: int = Field(default=15, ge=1, le=50)
    session_id: str = ""     # 会话内注入不应期的键;空则不启用抑制
    cwd: str = ""            # 工作区路径 → 扫描技术栈特征 + 推项目标识
    project: str = ""        # 显式项目标识(容器内 cwd 无信息量,hook 用 CE_PROJECT 传);优先于 cwd 推导
    seen_ids: list[str] = Field(default=[], max_length=500)  # 客户端从当前上下文(最近一次压缩之后)解析出的已注入酶 id,不再重复注入
    source: str = "prime"    # 触发来源:prime | failure(PostToolUseFailure 报错时注入,其余值归一为 prime)


class ReportRequest(BaseModel):
    enzyme_id: str
    success: bool
    detail: str = ""
    unexpected: bool = False


class ReportBatchRequest(BaseModel):
    enzyme_ids: list[str]   # ranked order as returned by prime()
    success: bool
    detail: str = ""


class HarvestRequest(BaseModel):
    insight: str
    context: str
    domain: str = ""
    tags: list[str] = []


class ContributeRequest(BaseModel):
    when: str
    then: str
    because: str
    domain: str = ""
    tags: list[str] = []
    references: list[str] = []
    severity: str = "info"


class RecallRequest(BaseModel):
    """情景回想:显式线索碎片(cues)或自由文本(query)二选一,都给则合并。"""
    cues: list[str] = []
    query: str = ""
    domain: str = ""
    limit: int = 5


class CaptureRequest(BaseModel):
    transcript: str = Field(max_length=2_000_000)  # 会话对话记录(纯文本摘要即可);hook 已截首尾,这里兜底防滥用
    domain: str = ""
    session_id: str = ""
    source: str = ""         # 触发来源:stop | sessionend | precompact
    cwd: str = ""            # 会话工作区 → 推项目标识,新生酶打 project: 标签
    project: str = ""        # 显式项目标识,优先于 cwd 推导
