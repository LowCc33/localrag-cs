"""
Agent 接口路由

提供 POST /api/agent/ask（非流式）和 POST /api/agent/ask/stream（流式 SSE）接口
支持 Agent 模式（DeepSeek 规划 + 工具调用）和降级到原有 RAG 流程

流式 SSE 事件协议：
    event: reasoning  data: {"text": "..."}     思考过程（内心独白）
    event: trace_step data: {"step": {...}}      工具调用步骤
    event: token      data: {"text": "..."}      最终回答逐字输出
    event: done       data: {"status":"...", "trace":[...]}  结束
    event: error      data: {"message":"..."}    异常
"""

import json
import logging
import asyncio
from typing import Optional
from pydantic import BaseModel, Field
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

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


def _format_sse(event: str, data: dict) -> str:
    """格式化 SSE 报文"""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


@router.post("/api/agent/ask")
async def agent_ask(request: AgentAskRequest):
    """
    Agent 问答接口（非流式）

    使用 DeepSeek-V4-Flash 进行意图理解和工具调用规划
    支持自动降级到原有 RAG 流程
    """
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
        agent = Agent()
        result = agent.run(request.question.strip())

        response = {
            "answer": result.get("answer", ""),
            "agent_trace": result.get("agent_trace", []),
            "status": result.get("status", "success"),
        }

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


@router.post("/api/agent/ask/stream")
async def agent_ask_stream(request: AgentAskRequest):
    """
    Agent 问答接口（流式 SSE 版）

    与 /api/agent/ask 行为一致，但改为 SSE 流式推送：
    - reasoning 事件：Agent 思考过程（内心独白）
    - trace_step 事件：工具调用步骤
    - token 事件：最终回答逐字输出
    - done 事件：结束标记
    - error 事件：异常
    """
    if not config.AGENT_ENABLED:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "Service Unavailable",
                "message": "Agent 模式未启用",
            }
        )

    async def event_generator():
        try:
            agent = Agent()
            sync_gen = agent.run_stream(request.question.strip())

            loop = asyncio.get_event_loop()
            sentinel = object()

            while True:
                event = await loop.run_in_executor(
                    None,
                    lambda: next(sync_gen, sentinel)
                )
                if event is sentinel:
                    break

                event_type = event.get("type", "")

                if event_type == "reasoning":
                    yield _format_sse("reasoning", {"text": event["text"]})
                elif event_type == "trace_step":
                    yield _format_sse("trace_step", {"step": event["step"]})
                elif event_type == "token":
                    yield _format_sse("token", {"text": event["text"]})
                elif event_type == "done":
                    yield _format_sse("done", {
                        "status": event.get("status", "success"),
                        "trace": event.get("trace", []),
                    })
                elif event_type == "error":
                    yield _format_sse("error", {"message": event.get("message", "未知错误")})
                    yield _format_sse("done", {"status": "error", "trace": event.get("trace", [])})

        except Exception as e:
            logger.error(f"Agent 流式接口异常: {e}", exc_info=True)
            yield _format_sse("error", {"message": f"处理失败: {str(e)}"})
            yield _format_sse("done", {"status": "error", "trace": []})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
