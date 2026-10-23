"""System 1 网关路由:其他 agent 按 TypeSafe 的 System One API 协议调用(POST {base}/systemone,base = /api/v1)。

逻辑见 app/distill/gateway.py;机器令牌可调用(数据面接口)。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.engine import Engine
from app.dependencies import get_engine
from app.distill import gateway

router = APIRouter(prefix="/api/v1", tags=["systemone"])


class SystemOneRequest(BaseModel):
    state: Any = ""
    questions: dict[str, Any]
    model: str = ""      # 指定判官(注册表里的名字);空 = 按 CE_SYSTEMONE_ROUTE 路由
    agent: str = ""      # 调用方标识,写进判定记录


class SystemOneFeedback(BaseModel):
    decision_id: str
    labels: dict[str, Any]   # {题号: true/false(noul) 或 选项键(choice)}
    source: str = "agent"


@router.post("/systemone")
async def systemone(req: SystemOneRequest, engine: Engine = Depends(get_engine)):
    if not req.questions:
        raise HTTPException(status_code=422, detail="questions 不能为空")
    try:
        return await gateway.serve(engine, req.state, req.questions, model=req.model, agent=req.agent)
    except gateway.NoJudgeAvailable as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/systemone/feedback")
async def systemone_feedback(req: SystemOneFeedback, engine: Engine = Depends(get_engine)):
    """回传真实结果:noul 题给 true/false,choice 题给正确选项的键。"""
    return {"recorded": await gateway.feedback(engine, req.decision_id, req.labels, req.source)}
