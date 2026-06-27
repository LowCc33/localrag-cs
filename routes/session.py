#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
会话管理接口路由
提供会话相关的CRUD操作，支持多轮对话上下文管理
"""

import logging

# 导入API模型
from schemas import (
    SessionCreateResponse,
    SessionHistoryResponse,
    SessionClearResponse,
    SessionDeleteResponse
)

# 导入会话管理器
from session_manager import session_manager

from fastapi import APIRouter, HTTPException

# 配置日志
logger = logging.getLogger(__name__)

router = APIRouter(tags=["session"])


@router.post("/api/session/new", response_model=SessionCreateResponse)
async def create_session():
    """
    创建新会话
    
    返回新创建的会话ID，客户端应保存此ID用于后续对话
    
    示例响应:
    {
        "session_id": "123e4567-e89b-12d3-a456-426614174000",
        "created_at": "2024-05-30T21:22:00",
        "ttl_hours": 24
    }
    """
    try:
        # 创建新会话
        session_id = session_manager.create_session()
        
        # 获取会话数据以获取创建时间
        session_data = session_manager._get_session_data(session_id)
        if not session_data:
            raise HTTPException(status_code=500, detail="会话创建失败")
        
        return SessionCreateResponse(
            session_id=session_id,
            created_at=session_data.get("created_at", ""),
            ttl_hours=24  # 固定24小时过期
        )
        
    except Exception as e:
        logger.error(f"创建会话失败: {e}")
        raise HTTPException(status_code=500, detail=f"创建会话失败: {str(e)}")


@router.get("/api/session/{session_id}/history", response_model=SessionHistoryResponse)
async def get_session_history(session_id: str):
    """
    获取会话历史记录
    
    参数:
        session_id: 会话ID（UUID格式）
    
    返回:
        会话的历史对话记录，最多8轮（16条消息）
    
    示例响应:
    {
        "session_id": "123e4567-e89b-12d3-a456-426614174000",
        "history": [
            {"role": "user", "content": "什么是保险理赔？"},
            {"role": "assistant", "content": "保险理赔是..."}
        ],
        "total_messages": 2,
        "created_at": "2024-05-30T21:22:00",
        "updated_at": "2024-05-30T21:23:00"
    }
    """
    try:
        # 获取会话数据
        session_data = session_manager._get_session_data(session_id)
        if not session_data:
            raise HTTPException(status_code=404, detail="会话不存在或已过期")
        
        # 获取历史记录
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
    
    返回:
        清空操作的结果
    
    示例响应:
    {
        "session_id": "123e4567-e89b-12d3-a456-426614174000",
        "cleared": true,
        "cleared_at": "2024-05-30T21:24:00",
        "remaining_messages": 0
    }
    """
    try:
        # 检查会话是否存在
        session_data = session_manager._get_session_data(session_id)
        if not session_data:
            raise HTTPException(status_code=404, detail="会话不存在或已过期")
        
        # 获取清空前消息数（用于日志记录）
        before_count = len(session_data.get("history", []))
        logger.info(f"清空会话 {session_id} 历史，原有 {before_count} 条消息")
        
        # 清空历史记录
        success = session_manager.clear_session(session_id)
        if not success:
            raise HTTPException(status_code=500, detail="清空会话历史失败")
        
        # 获取清空后的会话数据
        session_data = session_manager._get_session_data(session_id)
        
        return SessionClearResponse(
            session_id=session_id,
            cleared=success,
            cleared_at=session_data.get("updated_at", "") if session_data else "",
            remaining_messages=len(session_data.get("history", [])) if session_data else 0
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
    
    返回:
        删除操作的结果
    
    示例响应:
    {
        "session_id": "123e4567-e89b-12d3-a456-426614174000",
        "deleted": true,
        "deleted_at": "2024-05-30T21:25:00"
    }
    """
    try:
        # 删除会话
        success = session_manager.delete_session(session_id)
        
        if not success:
            raise HTTPException(status_code=404, detail="会话不存在或删除失败")
        
        # 使用当前时间作为删除时间
        from datetime import datetime
        deleted_at = datetime.now().isoformat()
        
        return SessionDeleteResponse(
            session_id=session_id,
            deleted=success,
            deleted_at=deleted_at
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除会话失败: {e}")
        raise HTTPException(status_code=500, detail=f"删除会话失败: {str(e)}")