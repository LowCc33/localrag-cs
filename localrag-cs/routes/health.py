#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
健康检查路由
提供 GET /api/health 接口，检查各依赖服务的健康状态
"""

import time
from datetime import datetime
from fastapi import APIRouter, HTTPException

# 导入依赖
from dependencies import get_client_manager
from schemas import HealthCheckResponse

router = APIRouter(tags=["health"])


@router.get("/api/health", response_model=HealthCheckResponse)
async def health_check():
    """
    健康检查接口
    
    检查所有依赖服务的健康状态：
    - Elasticsearch 连接状态
    - Embedding 服务状态
    - Reranker 服务状态
    - LLM 服务状态
    
    返回示例:
    {
        "status": "healthy",
        "timestamp": "2026-05-10T06:45:00+08:00",
        "version": "1.0.0",
        "services": {
            "elasticsearch": {"status": "ok", "latency_ms": 12.5},
            "embedding": {"status": "ok", "latency_ms": 8.2},
            "reranker": {"status": "ok", "latency_ms": 15.1},
            "llm": {"status": "ok", "latency_ms": 22.3}
        }
    }
    """
    manager = get_client_manager()
    
    # 服务名称映射
    service_names = {
        'es': 'elasticsearch',
        'encoder': 'embedding',
        'reranker': 'reranker',
        'llm': 'llm',
        'retriever': 'retriever'
    }
    
    # 检查各服务健康状态
    services_status = {}
    overall_status = "healthy"
    
    for client_name, service_name in service_names.items():
        start_time = time.perf_counter()
        try:
            # 获取客户端并测试连接
            client = manager.get_client(client_name)
            
            if client is None:
                # 客户端未初始化，尝试获取错误信息
                error = manager.get_init_error(client_name)
                services_status[service_name] = {
                    "status": "error",
                    "error": error or f"{service_name} 客户端未初始化"
                }
                overall_status = "degraded"
            else:
                # 测试客户端连接
                _test_client_connection(client_name, client)
                latency_ms = (time.perf_counter() - start_time) * 1000
                services_status[service_name] = {
                    "status": "ok",
                    "latency_ms": round(latency_ms, 2)
                }
                
        except Exception as e:
            services_status[service_name] = {
                "status": "error",
                "error": str(e)
            }
            overall_status = "degraded"
    
    # 如果所有服务都错误，返回503状态
    if all(s.get("status") == "error" for s in services_status.values()):
        overall_status = "unhealthy"
        raise HTTPException(
            status_code=503,
            detail={
                "status": overall_status,
                "timestamp": datetime.now().isoformat(),
                "services": services_status,
                "message": "所有依赖服务均不可用"
            }
        )
    
    # 构建响应
    response = {
        "status": overall_status,
        "timestamp": datetime.now().isoformat(),
        "version": "1.0.0",
        "services": services_status
    }
    
    return response


def _test_client_connection(client_name: str, client: object):
    """
    测试客户端连接是否正常
    
    Args:
        client_name: 客户端名称
        client: 客户端实例
        
    Raises:
        Exception: 连接测试失败时抛出异常
    """
    if client_name == 'es':
        # 测试 ES 连接
        if hasattr(client, 'ping'):
            if not client.ping():
                raise Exception("ES ping 失败")
        elif hasattr(client, 'client') and hasattr(client.client, 'ping'):
            if not client.client.ping():
                raise Exception("ES ping 失败")
    
    elif client_name == 'encoder':
        # 测试 Embedding 编码器
        if hasattr(client, 'encode'):
            # 尝试编码一个测试句子
            test_result = client.encode("测试")
            if test_result is None or len(test_result) == 0:
                raise Exception("Embedding 编码测试失败")
    
    elif client_name == 'reranker':
        # 测试 Reranker
        if hasattr(client, 'rerank'):
            # 尝试对测试数据进行重排（query, documents列表）
            test_query = "测试问题"
            test_docs = ["测试文档内容"]
            test_result = client.rerank(test_query, test_docs)
            if test_result is None:
                raise Exception("Reranker 测试失败")
    
    elif client_name == 'llm':
        # 测试 LLM
        if hasattr(client, 'generate'):
            # 尝试生成一个简短回复（context, query参数）
            test_result = client.generate(context="测试上下文", query="你好")
            if test_result is None or len(test_result) == 0:
                raise Exception("LLM 测试失败")
