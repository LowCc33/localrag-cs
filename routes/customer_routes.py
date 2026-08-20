# -*- coding: utf-8 -*-
"""
全屋定制AI客服 - API路由
职责：提供客服对话的HTTP接口（非流式 + SSE流式 + 清空会话）
架构位置：routes/customer_routes.py，由 api/app.py 挂到 /api/customer 前缀
特点：
    - 内存会话管理（最多100个，LRU淘汰，30分钟超时自动清理）
    - 非流式：直接返回完整答案
    - 流式：逐字模拟打字效果（SSE）
"""

import asyncio
import time
from collections import OrderedDict
from typing import Optional

import yaml

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

# 导入客服引擎（customer_service 在项目根目录）
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).parent.parent.absolute()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from customer_service.engine import CustomerServiceEngine

router = APIRouter()

# ==================== 会话管理 ====================
# 用 OrderedDict 实现简单的 LRU：
#   - 每次访问把会话移到末尾（最新）
#   - 超过 MAX_SESSIONS 时踢最前面的（最老）
#   - 定期清理超时会话

MAX_SESSIONS = 100            # 最大会话数
SESSION_TIMEOUT = 30 * 60     # 会话超时时间（秒）= 30分钟

# 会话字典：{ session_id: { "engine": engine, "last_active": timestamp } }
_sessions: "OrderedDict[str, dict]" = OrderedDict()


def _session_key(session_id: str, shop_id: str = None) -> str:
    """生成会话唯一key：session_id + shop_id 组合，不同商家互不干扰"""
    if shop_id:
        return f"{shop_id}:{session_id}"
    return f"default:{session_id}"


def _get_or_create_session(session_id: str, shop_id: str = None) -> CustomerServiceEngine:
    """
    获取或创建会话
    - 存在：移到末尾（标记为最新），检查是否超时
    - 不存在：新建，加入字典末尾
    - 超过最大数量：踢最老的
    """
    now = time.time()
    key = _session_key(session_id, shop_id)

    # 先清理一遍超时会话（顺便做定期清理，不用单独开线程）
    _cleanup_expired()

    if key in _sessions:
        # 移到末尾（标记最新）
        _sessions.move_to_end(key)
        sess = _sessions[key]
        sess["last_active"] = now
        return sess["engine"]
    else:
        # 新建会话（带上商家配置）
        engine = CustomerServiceEngine(shop_id=shop_id)
        _sessions[key] = {
            "engine": engine,
            "last_active": now,
            "shop_id": shop_id,
        }
        # 超过上限，踢最老的
        if len(_sessions) > MAX_SESSIONS:
            oldest_key = next(iter(_sessions))
            del _sessions[oldest_key]
        return engine


def _cleanup_expired():
    """清理超时会话（调用时顺便清理，不用单独线程）"""
    now = time.time()
    expired_keys = []
    for sid, sess in _sessions.items():
        if now - sess["last_active"] > SESSION_TIMEOUT:
            expired_keys.append(sid)
        else:
            # OrderedDict 是按插入顺序排的，遇到第一个没超时的，后面的都没超时
            # 不对，因为 move_to_end 会打乱，所以必须全遍历
            pass
    for sid in expired_keys:
        del _sessions[sid]


# ==================== 请求/响应模型 ====================

class AskRequest(BaseModel):
    """问答请求体"""
    question: str
    session_id: str = "default"
    shop_id: str = None  # 商家ID，可选，不传用默认配置


class ClearRequest(BaseModel):
    """清空会话请求体"""
    session_id: str = "default"
    shop_id: str = None  # 商家ID，可选


# ==================== 接口1：非流式问答 ====================

@router.post("/ask")
async def ask(req: AskRequest):
    """
    非流式问答接口
    请求：{ "question": "xxx", "session_id": "xxx", "shop_id": "xxx" }
    返回：{ "tag": "bargain/probe", "answer": "话术内容" }
    """
    engine = _get_or_create_session(req.session_id, req.shop_id)
    tag, answer = engine.reply(req.question)
    return JSONResponse(content={
        "tag": tag,
        "answer": answer,
    })


# ==================== 接口2：流式问答（SSE） ====================

@router.post("/ask/stream")
async def ask_stream(req: AskRequest):
    """
    流式问答接口（SSE）
    引擎返回完整句子后，模拟打字效果逐字输出
    事件类型：
        - message: 普通文本片段（逐字）
        - tag: 话术分类标签（在文本之前发送）
        - done: 结束
    """
    engine = _get_or_create_session(req.session_id, req.shop_id)
    tag, answer = engine.reply(req.question)

    async def generate():
        # 先发 tag 事件，让前端知道分类
        yield f"event: tag\ndata: {tag}\n\n"

        # 逐字输出，模拟打字效果
        # 每个字一个 token 事件，间隔 30ms 左右
        for char in answer:
            # 用 message 事件，data 是单个字
            yield f"data: {char}\n\n"
            await asyncio.sleep(0.03)

        # 结束事件
        yield "event: done\ndata: \n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用Nginx缓冲
        }
    )


# ==================== 接口3：清空会话 ====================

@router.post("/clear")
async def clear_session(req: ClearRequest):
    """
    清空指定会话的上下文
    请求：{ "session_id": "xxx", "shop_id": "xxx" }
    返回：{ "status": "ok" }
    """
    key = _session_key(req.session_id, req.shop_id)
    if key in _sessions:
        engine = _sessions[key]["engine"]
        engine.clear()
        # 更新最后活跃时间
        _sessions[key]["last_active"] = time.time()
        _sessions.move_to_end(key)
    return JSONResponse(content={"status": "ok"})


# ==================== 接口4：获取欢迎语和商家信息 ====================

@router.get("/welcome")
async def welcome(shop_id: str = None):
    """
    获取欢迎语和商家基本信息
    参数：shop_id（可选，不传用默认配置）
    返回：{ "shop_name": "...", "welcome_text": "...", "quick_questions": [...] }
    """
    # 读取商家配置（支持多商家）
    from customer_service.shop_config_loader import load_shop_config
    config = load_shop_config(shop_id)

    shop_name = config.get("shop_name", "佳美全屋定制")
    boss_name = config.get("boss_name", "王师傅")

    # 客服称呼：优先用配置里的customer_service_name，没有就用"小+老板姓"
    service_name = config.get("customer_service_name", "")
    if not service_name:
        boss_last_name = boss_name[0] if boss_name else "王"
        service_name = f"小{boss_last_name}"

    welcome_text = f"您好！我是{shop_name}的客服{service_name}，很高兴为您服务。请问您是想咨询柜子定制吗？😊"

    # 快捷问题
    quick_questions = [
        "多少钱一平？",
        "环保吗？",
        "工期多久？",
        "能优惠吗？",
    ]

    return JSONResponse(content={
        "shop_name": shop_name,
        "boss_name": boss_name,
        "service_name": service_name,
        "welcome_text": welcome_text,
        "quick_questions": quick_questions,
    })
