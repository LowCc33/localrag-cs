"""
Agent 接口路由

提供 POST /api/agent/ask 接口
支持 Agent 模式（DeepSeek 规划 + 工具调用）和降级到原有 RAG 流程

接口说明:
    POST /api/agent/ask
    请求体: {"question": "用户问题", "kb_id": "知识库ID（可选）"}
    返回: {"answer": "最终回答", "agent_trace": [...], "status": "success"|"fallback"|"error"}
"""

import logging
from typing import Optional
from pydantic import BaseModel, Field
from fastapi import APIRouter, HTTPException

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import config

from agent.agent import Agent

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent"])


class AgentAskRequest(BaseModel):
    """Agent 问答接口请求体"""
    question: str = Field(..., description="用户问题", min_length=1, max_length=1000)
    kb_id: Optional[str] = Field(None, description="知识库ID（可选，当前版本暂未使用）")


@router.post("/api/agent/ask")
async def agent_ask(request: AgentAskRequest):
    """
    Agent 问答接口

    使用 DeepSeek-V4-Flash 进行意图理解和工具调用规划
    支持自动降级到原有 RAG 流程

    ## 参数说明
    - **question**: 用户问题（必填，1-1000字符）
    - **kb_id**: 知识库ID（可选，当前版本暂未使用）

    ## 返回说明
    - **answer**: 最终回答
    - **agent_trace**: Agent 思考过程记录（每步的工具调用和结果）
    - **status**: 状态（success/fallback/error）
    - **error**: 错误信息（仅 status=error 时存在）

    ## 降级策略
    - DeepSeek API 超时/报错 → 自动降级到原有 RAG 流程
    - 检索工具失败 → 重试1次，再失败返回"检索服务异常"
    - 生成工具失败 → 重试1次，再失败返回"生成服务异常"
    - Agent 超过3轮循环 → 强制终止，返回当前已收集的信息
    """
    # 检查 Agent 是否启用
    if not config.AGENT_ENABLED:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "Service Unavailable",
                "message": "Agent 模式未启用",
                "detail": "请设置 AGENT_ENABLED=True 后重试",
            }
        )

    try:
        # 创建 Agent 实例并执行
        agent = Agent()
        result = agent.run(request.question.strip())

        # 构建响应
        response = {
            "answer": result.get("answer", ""),
            "agent_trace": result.get("agent_trace", []),
            "status": result.get("status", "success"),
        }

        # 如果有错误信息，加到响应里
        if result.get("error"):
            response["error"] = result["error"]

        return response

    except Exception as e:
        logger.error(f"Agent 接口异常: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Internal Server Error",
                "message": "处理请求时发生错误",
                "detail": str(e),
            }
        )
