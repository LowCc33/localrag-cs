# -*- coding: utf-8 -*-
"""
客户管理API路由
提供客户档案的CRUD接口、列表查询、标记加微信等
架构位置：routes/customer_admin_routes.py
挂在 /api/customer-admin 前缀下
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from customer_service.customer import customer_db

logger = logging.getLogger(__name__)
router = APIRouter()


# ==================== 请求/响应模型 ====================

class CreateCustomerRequest(BaseModel):
    """创建客户请求"""
    session_id: str
    shop_id: Optional[str] = None
    source_channel: Optional[str] = None


class UpdateCustomerRequest(BaseModel):
    """更新客户请求"""
    customer_type: Optional[str] = None
    scenes: Optional[str] = None
    area: Optional[str] = None
    preference: Optional[str] = None
    pricing_method: Optional[str] = None
    wechat_nick: Optional[str] = None
    phone: Optional[str] = None
    community: Optional[str] = None
    decoration_progress: Optional[str] = None
    has_measurement: Optional[int] = None
    intent_level: Optional[str] = None
    wechat_status: Optional[str] = None
    source_channel: Optional[str] = None
    remark: Optional[str] = None


class MarkWechatRequest(BaseModel):
    """标记加微信请求"""
    wechat_nick: str


# ==================== 接口1：创建客户 ====================

@router.post("")
async def create_customer(req: CreateCustomerRequest):
    """
    创建客户档案（会话开始时自动调用）
    自动生成唯一暗号
    """
    try:
        customer = customer_db.create_customer(
            session_id=req.session_id,
            shop_id=req.shop_id,
            source_channel=req.source_channel,
        )
        return {
            "code": 0,
            "message": "创建成功",
            "data": customer,
        }
    except Exception as e:
        logger.error(f"创建客户失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== 接口2：获取客户 ====================

@router.get("/{session_id}")
async def get_customer(session_id: str):
    """按会话ID获取客户信息"""
    customer = customer_db.get_customer(session_id)
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    return {
        "code": 0,
        "message": "成功",
        "data": customer,
    }


# ==================== 接口3：更新客户 ====================

@router.put("/{session_id}")
async def update_customer(session_id: str, req: UpdateCustomerRequest):
    """更新客户信息"""
    update_data = {k: v for k, v in req.dict().items() if v is not None}
    customer = customer_db.update_customer(session_id, **update_data)
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    return {
        "code": 0,
        "message": "更新成功",
        "data": customer,
    }


# ==================== 接口4：标记已加微信 ====================

@router.put("/{session_id}/wechat")
async def mark_wechat_added(session_id: str, req: MarkWechatRequest):
    """标记已加微信，填写微信昵称"""
    customer = customer_db.mark_wechat_added(
        session_id=session_id,
        wechat_nick=req.wechat_nick,
    )
    if not customer:
        raise HTTPException(status_code=404, detail="客户不存在")
    return {
        "code": 0,
        "message": "标记成功",
        "data": customer,
    }


# ==================== 接口5：客户列表 ====================

@router.get("/list")
async def list_customers(
    page: int = 1,
    page_size: int = 20,
    wechat_status: Optional[str] = None,
    intent_level: Optional[str] = None,
    search: Optional[str] = None,
    sort_by: str = "created_at",
    sort_order: str = "desc",
):
    """
    客户列表（分页 + 筛选 + 搜索）
    - search: 搜索关键词（暗号/微信昵称/手机号/小区）
    - wechat_status: 加微信状态筛选（none/added/lost）
    - intent_level: 意向等级筛选（A/B/C）
    """
    result = customer_db.list_customers(
        page=page,
        page_size=page_size,
        wechat_status=wechat_status,
        intent_level=intent_level,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return {
        "code": 0,
        "message": "成功",
        "data": result,
    }


# ==================== 接口6：按暗号查找客户 ====================

@router.get("/code/{secret_code}")
async def get_by_secret_code(secret_code: str):
    """按暗号查找客户（销售用微信备注的暗号搜客户）"""
    customer = customer_db.get_customer_by_secret_code(secret_code)
    if not customer:
        raise HTTPException(status_code=404, detail="未找到对应客户")
    return {
        "code": 0,
        "message": "成功",
        "data": customer,
    }
