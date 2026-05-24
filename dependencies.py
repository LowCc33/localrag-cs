#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
依赖注入模块
管理所有客户端的单例初始化和依赖注入
使用 @lru_cache 确保每个客户端只初始化一次
"""

import time
import logging
from functools import lru_cache
from typing import Optional

from fastapi import Request, HTTPException

# 获取日志记录器（日志配置在app.py中统一完成）
logger = logging.getLogger(__name__)


# ============== 客户端单例管理 ==============

class ClientManager:
    """
    客户端管理器
    负责所有外部依赖客户端的初始化和生命周期管理
    """
    
    def __init__(self):
        self._es_client: Optional[object] = None
        self._encoder: Optional[object] = None
        self._reranker: Optional[object] = None
        self._llm: Optional[object] = None
        self._retriever: Optional[object] = None
        self._initialized = False
        self._init_errors = {}
    
    def initialize(self):
        """
        初始化所有客户端（在FastAPI startup事件中调用）
        单个客户端初始化失败不会阻止其他客户端初始化
        """
        if self._initialized:
            logger.info("客户端管理器已初始化，跳过")
            return
        
        logger.info("=" * 60)
        logger.info("🚀 开始初始化客户端管理器...")
        
        # 1. 初始化 ES 客户端
        try:
            logger.info("  📡 初始化 Elasticsearch 客户端...")
            from core.es_client import ElasticsearchClient
            self._es_client = ElasticsearchClient()
            # 测试连接
            self._es_client._ping_with_retry()
            logger.info("  ✅ ES 客户端初始化成功")
        except Exception as e:
            self._init_errors['elasticsearch'] = str(e)
            logger.warning(f"  ⚠️ ES 客户端初始化失败: {e}")
        
        # 2. 初始化 Embedding 编码器
        try:
            logger.info("  🔢 初始化 Embedding 编码器...")
            from core.embedding import EmbeddingClient
            self._encoder = EmbeddingClient()
            logger.info("  ✅ Embedding 编码器初始化成功")
        except Exception as e:
            self._init_errors['embedding'] = str(e)
            logger.warning(f"  ⚠️ Embedding 编码器初始化失败: {e}")
        
        # 3. 初始化 Reranker
        try:
            logger.info("  📊 初始化 Reranker...")
            from core.reranker import RerankerClient
            self._reranker = RerankerClient()
            logger.info("  ✅ Reranker 初始化成功")
        except Exception as e:
            self._init_errors['reranker'] = str(e)
            logger.warning(f"  ⚠️ Reranker 初始化失败: {e}")
        
        # 4. 初始化 LLM 客户端
        try:
            logger.info("  🤖 初始化 LLM 客户端...")
            from core.llm_client import LLMClient
            self._llm = LLMClient()
            logger.info("  ✅ LLM 客户端初始化成功")
        except Exception as e:
            self._init_errors['llm'] = str(e)
            logger.warning(f"  ⚠️ LLM 客户端初始化失败: {e}")
        
        # 5. 初始化 Hybrid Retriever（依赖 ES 和 Encoder）
        if self._es_client and self._encoder:
            try:
                logger.info("  🔍 初始化 Hybrid Retriever...")
                from core.retriever import HybridRetriever
                self._retriever = HybridRetriever(
                    es_client=self._es_client,
                    vector_encoder=self._encoder
                )
                logger.info("  ✅ Hybrid Retriever 初始化成功")
            except Exception as e:
                self._init_errors['retriever'] = str(e)
                logger.warning(f"  ⚠️ Hybrid Retriever 初始化失败: {e}")
        else:
            logger.warning("  ⚠️ 跳过 Hybrid Retriever 初始化（ES或Encoder不可用）")
        
        self._initialized = True
        logger.info("=" * 60)
        logger.info("✅ 客户端管理器初始化完成")
        logger.info(f"   成功: {len(self.get_healthy_services())} 个服务")
        logger.info(f"   失败: {len(self._init_errors)} 个服务")
        if self._init_errors:
            logger.warning(f"   失败列表: {list(self._init_errors.keys())}")
        logger.info("=" * 60)
    
    def get_client(self, name: str) -> Optional[object]:
        """获取指定客户端实例"""
        clients = {
            'es': self._es_client,
            'encoder': self._encoder,
            'reranker': self._reranker,
            'llm': self._llm,
            'retriever': self._retriever
        }
        return clients.get(name)
    
    def get_healthy_services(self) -> list:
        """获取健康的服务列表"""
        healthy = []
        if self._es_client:
            healthy.append('elasticsearch')
        if self._encoder:
            healthy.append('embedding')
        if self._reranker:
            healthy.append('reranker')
        if self._llm:
            healthy.append('llm')
        if self._retriever:
            healthy.append('retriever')
        return healthy
    
    def get_init_error(self, service: str) -> Optional[str]:
        """获取指定服务的初始化错误信息"""
        return self._init_errors.get(service)


# 全局客户端管理器实例
_client_manager = ClientManager()


def get_client_manager() -> ClientManager:
    """获取全局客户端管理器（用于依赖注入）"""
    return _client_manager


def initialize_clients():
    """在应用启动时初始化所有客户端"""
    _client_manager.initialize()


# ============== FastAPI 依赖函数 ==============

async def get_retriever(request: Request):
    """
    FastAPI 依赖：获取 Hybrid Retriever
    用于 /api/ask 路由
    """
    manager = get_client_manager()
    retriever = manager.get_client('retriever')
    
    if not retriever:
        error_msg = manager.get_init_error('retriever') or "Retriever 未初始化"
        raise HTTPException(
            status_code=503,
            detail={
                "error": "Service Unavailable",
                "message": "检索服务不可用",
                "detail": error_msg
            }
        )
    
    return retriever


async def get_reranker(request: Request):
    """FastAPI 依赖：获取 RerankerClient"""
    manager = get_client_manager()
    reranker = manager.get_client('reranker')
    
    if not reranker:
        error_msg = manager.get_init_error('reranker') or "Reranker 未初始化"
        raise HTTPException(
            status_code=503,
            detail={
                "error": "Service Unavailable", 
                "message": "重排服务不可用",
                "detail": error_msg
            }
        )
    
    return reranker


async def get_llm(request: Request):
    """FastAPI 依赖：获取 LLMClient"""
    manager = get_client_manager()
    llm = manager.get_client('llm')
    
    if not llm:
        error_msg = manager.get_init_error('llm') or "LLM 未初始化"
        raise HTTPException(
            status_code=503,
            detail={
                "error": "Service Unavailable",
                "message": "LLM服务不可用", 
                "detail": error_msg
            }
        )
    
    return llm


async def get_es_client(request: Request):
    """FastAPI 依赖：获取 ElasticsearchClient（用于健康检查）"""
    manager = get_client_manager()
    es = manager.get_client('es')
    
    if not es:
        error_msg = manager.get_init_error('es') or "ES 未初始化"
        raise HTTPException(
            status_code=503,
            detail={
                "error": "Service Unavailable",
                "message": "Elasticsearch 服务不可用",
                "detail": error_msg
            }
        )
    
    return es
