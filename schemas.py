#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API 请求和响应模型定义
定义了 /api/ask 接口的输入输出数据结构
"""

from typing import List, Optional
from pydantic import BaseModel, Field


class SourceDoc(BaseModel):
    """检索到的源文档信息"""
    doc_id: Optional[str] = Field(default="", description="文档唯一标识（业务侧 doc_id，可能缺失）")
    title: str = Field(default="", description="文档标题")
    content: str = Field(default="", description="文档内容片段")
    source_file: Optional[str] = Field(None, description="来源文件名")
    es_score: Optional[float] = Field(None, description="ES BM25分数（兼容旧字段名）")
    bm25_score: Optional[float] = Field(None, description="BM25原始分数")
    vector_score: Optional[float] = Field(None, description="向量相似度分数")
    has_vector: Optional[bool] = Field(True, description="是否使用了向量检索")
    rerank_score: Optional[float] = Field(None, description="重排模型分数")


class LatencyStats(BaseModel):
    """各阶段耗时统计（单位：毫秒）"""
    total_ms: float = Field(..., description="总耗时")
    retrieval_ms: float = Field(..., description="混合检索耗时")
    rerank_ms: float = Field(..., description="重排耗时")
    llm_ms: float = Field(..., description="LLM生成耗时")


class AskRequest(BaseModel):
    """
    问答接口请求模型
    
    示例请求:
    {
        "question": "雇主责任险和工伤保险有什么区别？",
        "top_k": 10,
        "rerank_top_k": 3,
        "index_name": "cs_knowledge_base",
        "session_id": "123e4567-e89b-12d3-a456-426614174000"
    }
    """
    question: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="用户问题"
    )
    top_k: int = Field(
        default=10,
        ge=1,
        le=50,
        description="混合检索召回数量"
    )
    rerank_top_k: int = Field(
        default=3,
        ge=1,
        le=10,
        description="重排后返回的文档数量"
    )
    index_name: str = Field(
        default="cs_knowledge_base",
        description="ES索引名称"
    )
    session_id: Optional[str] = Field(
        default=None,
        description="会话ID，传此参数时问答结果会写入会话历史"
    )


class AskResponse(BaseModel):
    """
    问答接口响应模型
    
    包含LLM生成的最终答案、检索到的源文档、以及各阶段耗时统计
    """
    question: str = Field(..., description="原始问题")
    answer: str = Field(..., description="LLM生成的最终答案")
    sources: List[SourceDoc] = Field(default=[], description="检索到的源文档列表")
    latency: LatencyStats = Field(..., description="各阶段耗时统计")
    retrieved_count: int = Field(..., description="混合召回的文档数量")
    reranked_count: int = Field(..., description="重排后的文档数量")
    status: str = Field(default="success", description="处理状态: success/error")
    error_message: Optional[str] = Field(None, description="错误信息（如有）")
    # ===== 缓存相关字段（任务 localrag-redis-cache） =====
    # HIT 表示走了 Redis 缓存毫秒级返回，MISS 表示完整链路
    cache_status: Optional[str] = Field(
        default=None, description="缓存命中状态：HIT / MISS / None（接口未接缓存时）"
    )
    response_time_ms: Optional[float] = Field(
        default=None, description="端到端响应耗时（毫秒），用于前端徽章展示"
    )
    cached_at: Optional[str] = Field(
        default=None, description="缓存写入时间，仅 HIT 时返回"
    )


class HealthCheckResponse(BaseModel):
    """
    健康检查响应模型
    
    返回各依赖服务的健康状态
    """
    status: str = Field(default="healthy", description="整体健康状态")
    timestamp: str = Field(..., description="检查时间戳（ISO格式）")
    version: str = Field(default="1.0.0", description="API版本")
    services: dict = Field(
        default={},
        description="各服务健康状态",
        example={
            "elasticsearch": {"status": "ok", "latency_ms": 12.5},
            "embedding": {"status": "ok", "latency_ms": 8.2},
            "reranker": {"status": "ok", "latency_ms": 15.1},
            "llm": {"status": "ok", "latency_ms": 22.3}
        }
    )


# ========== 会话管理模型 ==========


class SessionCreateResponse(BaseModel):
    """创建会话响应模型"""
    session_id: str = Field(..., description="会话ID")
    created_at: str = Field(..., description="创建时间")
    ttl_hours: int = Field(default=24, description="过期时间（小时）")


class SessionHistoryResponse(BaseModel):
    """会话历史响应模型"""
    session_id: str = Field(..., description="会话ID")
    history: List[dict] = Field(default=[], description="历史消息列表")
    total_messages: int = Field(..., description="消息总数")
    created_at: str = Field(..., description="创建时间")
    updated_at: str = Field(..., description="最后更新时间")


class SessionClearResponse(BaseModel):
    """清空会话响应模型"""
    session_id: str = Field(..., description="会话ID")
    cleared: bool = Field(..., description="是否清空成功")
    cleared_at: str = Field(..., description="清空时间")
    remaining_messages: int = Field(..., description="剩余消息数")


class SessionDeleteResponse(BaseModel):
    """删除会话响应模型"""
    session_id: str = Field(..., description="会话ID")
    deleted: bool = Field(..., description="是否删除成功")
    deleted_at: str = Field(..., description="删除时间")


class SessionListItem(BaseModel):
    """会话列表项模型"""
    id: str = Field(..., description="会话ID")
    title: str = Field(..., description="会话标题")
    created_at: str = Field(..., description="创建时间")
    updated_at: str = Field(..., description="最后更新时间")
    message_count: int = Field(..., description="消息数量")


class SessionListResponse(BaseModel):
    """会话列表响应模型"""
    total: int = Field(..., description="总会话数")
    sessions: List[SessionListItem] = Field(default=[], description="会话列表")
    limit: int = Field(..., description="每页限制")
    offset: int = Field(..., description="偏移量")
