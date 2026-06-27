#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
会话管理接口路由

提供会话的CRUD操作，支持多轮对话上下文管理。
所有数据持久化到 SQLite 数据库（data/sessions.db）。
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

# 导入API模型
from schemas import (
    SessionCreateResponse,
    SessionHistoryResponse,
    SessionClearResponse,
    SessionDeleteResponse,
    SessionListResponse,
    SessionListItem
)

# 导入会话管理器
from session_manager import session_manager

# 配置日志
logger = logging.getLogger(__name__)

router = APIRouter(tags=["session"])


@router.post("/api/session/new", response_model=SessionCreateResponse)
async def create_session():
    """
    创建新会话

    返回新创建的会话ID，客户端应保存此ID用于后续对话
    """
    try:
        session_id = session_manager.create_session()
        session_data = session_manager.get_session(session_id)
        if not session_data:
            raise HTTPException(status_code=500, detail="会话创建失败")

        return SessionCreateResponse(
            session_id=session_id,
            created_at=session_data.get("created_at", ""),
            ttl_hours=24
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"创建会话失败: {e}")
        raise HTTPException(status_code=500, detail=f"创建会话失败: {str(e)}")


@router.get("/api/sessions", response_model=SessionListResponse)
async def list_sessions(
    limit: int = Query(50, description="每页数量", ge=1, le=200),
    offset: int = Query(0, description="偏移量", ge=0)
):
    """
    获取会话列表（按更新时间倒序）

    返回所有会话的简要信息，不包含完整消息内容。
    """
    try:
        result = session_manager.list_sessions(limit=limit, offset=offset)
        # 转换为 Pydantic 模型
        result["sessions"] = [SessionListItem(**s) for s in result["sessions"]]
        return SessionListResponse(**result)
    except Exception as e:
        logger.error(f"获取会话列表失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取会话列表失败: {str(e)}")


@router.get("/api/session/{session_id}/history", response_model=SessionHistoryResponse)
async def get_session_history(session_id: str):
    """
    获取会话历史记录

    参数:
        session_id: 会话ID（UUID格式）

    返回:
        会话的历史对话记录，最多8轮（16条消息）
    """
    try:
        session_data = session_manager.get_session(session_id)
        if not session_data:
            raise HTTPException(status_code=404, detail="会话不存在或已过期")

        history = session_manager.get_history(session_id)

        return SessionHistoryResponse(
            session_id=session_id,
            history=history,
            total_messages=len(history),
            created_at=session_data.get("created_at", ""),
            updated_at=session_data.get("updated_at", "")
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取会话历史失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取会话历史失败: {str(e)}")


@router.post("/api/session/{session_id}/clear", response_model=SessionClearResponse)
async def clear_session_history(session_id: str):
    """
    清空会话历史记录

    参数:
        session_id: 会话ID（UUID格式）
    """
    try:
        session_data = session_manager.get_session(session_id)
        if not session_data:
            raise HTTPException(status_code=404, detail="会话不存在或已过期")

        before_count = session_manager.get_message_count(session_id)
        logger.info(f"清空会话 {session_id} 历史，原有 {before_count} 条消息")

        success = session_manager.clear_session(session_id)
        if not success:
            raise HTTPException(status_code=500, detail="清空会话历史失败")

        now = datetime.now().isoformat()

        return SessionClearResponse(
            session_id=session_id,
            cleared=success,
            cleared_at=now,
            remaining_messages=0
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"清空会话历史失败: {e}")
        raise HTTPException(status_code=500, detail=f"清空会话历史失败: {str(e)}")


@router.delete("/api/session/{session_id}", response_model=SessionDeleteResponse)
async def delete_session(session_id: str):
    """
    删除整个会话

    参数:
        session_id: 会话ID（UUID格式）
    """
    try:
        success = session_manager.delete_session(session_id)
        if not success:
            raise HTTPException(status_code=404, detail="会话不存在或删除失败")

        now = datetime.now().isoformat()

        return SessionDeleteResponse(
            session_id=session_id,
            deleted=success,
            deleted_at=now
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除会话失败: {e}")
        raise HTTPException(status_code=500, detail=f"删除会话失败: {str(e)}")
