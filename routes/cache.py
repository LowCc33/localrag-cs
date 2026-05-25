#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
缓存管理接口（任务 localrag-redis-cache 步骤D）

提供两个轻量管理接口，供前端演示面板和运维手动调用：
- GET  /api/cache/stats   返回 HIT / MISS / 命中率 / 已缓存条数 等运行时统计
- POST /api/cache/flush   清空本项目命名空间下的所有缓存键 + 重置统计

设计说明：
- 所有逻辑下沉到 core/cache.py，本文件只做 HTTP 层薄封装
- 任何异常都被 core/cache.py 内部吞掉并降级，这里只需返回 dict 即可
"""

from fastapi import APIRouter

from core import cache as cache_service

# tags 用 "cache" 方便在 Swagger UI 里独立分组
router = APIRouter(prefix="/api/cache", tags=["cache"])


@router.get("/stats")
async def cache_stats():
    """
    查询当前缓存运行统计

    返回字段说明（与 core.cache.get_stats 保持一致）：
    - enabled:     CACHE_ENABLED 总开关
    - available:   Redis 是否当前可用（连得上）
    - hit / miss:  累计命中 / 未命中次数
    - total:       hit + miss
    - hit_rate:    命中率百分比（保留 2 位小数）
    - cached_keys: 当前已缓存的条目数（不含统计键）
    - ttl_seconds: 配置的 TTL（秒）
    """
    return cache_service.get_stats()


@router.post("/flush")
async def cache_flush():
    """
    清空缓存（仅本项目命名空间，不会影响同 Redis 库下其它业务键）

    使用场景：
    - 知识库重建后，旧答案已失效，手动清一遍
    - 演示时想从 0 开始展示命中率上升过程
    返回：
    - flushed:    本次清掉的条目数
    - stats_reset: 是否重置了 HIT/MISS 统计
    - available:   Redis 当前是否可用
    """
    return cache_service.flush_cache()
