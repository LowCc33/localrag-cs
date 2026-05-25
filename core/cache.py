#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LocalRAG-CS 热点查询缓存模块（任务：localrag-redis-cache）

【核心职责】
- 对外提供 get_cache / set_cache / get_stats / flush_cache 四个能力
- 把"问完整 RAG 链路"的最终答案缓存到 Redis，命中即毫秒返回
- 所有 Redis 访问异常一律静默降级（返回 None / 不抛异常），不阻塞业务

【架构位置】
- 上游调用方：routes/ask.py（同步 /api/ask 与流式 /api/ask/stream）
- 下游依赖：用户态 Redis（~/redis/，由 scripts/start_redis.sh 启动）
- 配置入口：config.py 中 CACHE_* 系列常量（禁止在本文件硬编码）

【设计要点】
1. Key 归一化：去掉所有标点+空白字符，转小写，md5 后加前缀
   —— 让"电脑卡？"和"电脑卡，怎么办"在归一化后命中同一个缓存
2. Value 结构：JSON（answer / sources / cached_at / original_query）
3. TTL：从 config.CACHE_TTL_SECONDS 读，默认 24h
4. 统计：HIT / MISS 计数器存在 Redis Hash，跨进程共享，重启/flush 不丢
5. 降级：单例 Redis 客户端构造失败或运行时异常，全部 return 默认值
"""

import json
import hashlib
import logging
import threading
from datetime import datetime
from typing import Optional, Dict, Any, List

import redis

import config

logger = logging.getLogger(__name__)

# ============================================================
# 内部状态：Redis 单例 + 线程锁
# ============================================================
# 用模块级单例避免每次请求都建连接（redis-py 自带连接池，但单例更省）
_redis_client: Optional[redis.Redis] = None
_client_lock = threading.Lock()
# 是否打过 "Redis 不可用" 的告警日志，避免刷屏（同一异常只打一次）
_warned_unavailable = False


# ============================================================
# 内部工具：归一化 + Key 生成
# ============================================================
# 中英文标点完整集合（任务方案要求"中英文标点都去"）
# 写死成常量字符串，比每次 import string + 中文集合更直观
_PUNCT_CHARS = (
    # 英文标点
    r'''!"#$%&'()*+,-./:;<=>?@[\]^_`{|}~'''
    # 中文标点（覆盖常见 16 个）
    "，。！？；：、（）【】「」《》"
    "“”‘’……—·\u3000"
)
_PUNCT_TABLE = str.maketrans('', '', _PUNCT_CHARS)


def _normalize_query(query: str) -> str:
    """
    归一化用户原始 query，用于生成稳定的缓存 key

    规则（与任务方案严格一致）：
    1. 去除所有中英文标点
    2. 去除所有空白字符（空格 / tab / 换行 / 全角空格）
    3. 转小写（兼容英文混合场景，中文本身无大小写）

    示例：
        "电脑卡怎么办？"   → "电脑卡怎么办"
        "电脑卡，怎么办"   → "电脑卡怎么办"
        " HELLO  world! " → "helloworld"
    """
    if not query:
        return ""
    # 1. 去标点
    text = query.translate(_PUNCT_TABLE)
    # 2. 去所有空白（包括全角空格 \u3000，已在标点表里）
    text = "".join(text.split())
    # 3. 转小写
    return text.lower()


def _build_key(query: str) -> str:
    """
    把归一化后的 query 转成完整 Redis key：
        前缀 + md5(归一化后的字符串)

    使用 md5 而不是 sha256：缓存 key 不需要密码学强度，md5 短且快
    """
    normalized = _normalize_query(query)
    digest = hashlib.md5(normalized.encode("utf-8")).hexdigest()
    return f"{config.CACHE_KEY_PREFIX}{digest}"


# ============================================================
# Redis 客户端单例（带降级）
# ============================================================
def _get_client() -> Optional[redis.Redis]:
    """
    获取 Redis 客户端单例

    特性：
    - 第一次调用时按 config.CACHE_* 构造
    - 任何异常（连接拒绝、超时、配置错误）一律 return None，调用方继续走原流程
    - 用模块锁保证多线程下只构造一次
    - "不可用"告警只打一次，避免日志刷屏
    """
    global _redis_client, _warned_unavailable

    # 缓存总开关关闭：直接返回 None，等同没装 Redis
    if not config.CACHE_ENABLED:
        return None

    if _redis_client is not None:
        return _redis_client

    with _client_lock:
        # 双重检查，避免锁等待期间被其他线程已经初始化
        if _redis_client is not None:
            return _redis_client
        try:
            client = redis.Redis(
                host=config.CACHE_REDIS_HOST,
                port=config.CACHE_REDIS_PORT,
                db=config.CACHE_REDIS_DB,
                password=config.CACHE_REDIS_PASSWORD,
                socket_timeout=config.CACHE_REDIS_TIMEOUT,
                socket_connect_timeout=config.CACHE_REDIS_TIMEOUT,
                decode_responses=True,  # 直接返回 str，避免每次手动 decode
            )
            # 建连之后立即 ping 一次，验证可达
            client.ping()
            _redis_client = client
            logger.info(
                "Redis 缓存已连接：%s:%s db=%s",
                config.CACHE_REDIS_HOST,
                config.CACHE_REDIS_PORT,
                config.CACHE_REDIS_DB,
            )
            return _redis_client
        except Exception as exc:
            if not _warned_unavailable:
                logger.warning(
                    "Redis 缓存不可用，将自动降级到无缓存模式：%s", exc
                )
                _warned_unavailable = True
            return None


# ============================================================
# 对外接口
# ============================================================
def get_cache(query: str) -> Optional[Dict[str, Any]]:
    """
    查缓存：命中返回 dict（含 answer/sources/cached_at/original_query），未命中或异常返回 None

    入参：
        query: 用户原始查询字符串（未归一化）
    返回：
        - 命中：{"answer": str, "sources": list, "cached_at": str, "original_query": str}
        - 未命中 / Redis 不可用：None

    注意：本函数会同步累加命中/未命中计数器（写 Redis Hash），但失败不会抛
    """
    if not query:
        return None
    client = _get_client()
    if client is None:
        return None
    try:
        key = _build_key(query)
        raw = client.get(key)
        if raw is None:
            # MISS：累加 miss 计数器，便于 /api/cache/stats 输出
            _safe_incr(client, "miss")
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # 缓存值损坏（不太可能但兜底）：当作 MISS，并删除脏数据
            logger.warning("缓存值反序列化失败，删除并按 MISS 处理：key=%s", key)
            try:
                client.delete(key)
            except Exception:
                pass
            _safe_incr(client, "miss")
            return None
        # HIT：累加 hit 计数器
        _safe_incr(client, "hit")
        return data
    except Exception as exc:
        # Redis 运行时异常（如断连）：当 MISS 处理，不阻塞主流程
        logger.warning("缓存读取异常，按 MISS 处理：%s", exc)
        return None


def set_cache(
    query: str,
    answer: str,
    sources: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """
    写缓存：把一次完整 RAG 链路的最终答案存进 Redis

    入参：
        query:   用户原始查询（未归一化，会在内部归一化生成 key）
        answer:  LLM 生成的最终答案字符串
        sources: 引用的源文档列表（可选）

    返回：
        True 写入成功；False 跳过/失败（不会抛异常）
    """
    if not query or not answer:
        return False
    client = _get_client()
    if client is None:
        return False
    try:
        key = _build_key(query)
        payload = {
            "answer": answer,
            "sources": sources or [],
            # 用本地时间格式化，方便演示界面直接展示
            "cached_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "original_query": query,
        }
        # ex 单位是秒，对应 config.CACHE_TTL_SECONDS（默认 24h）
        client.set(
            key,
            json.dumps(payload, ensure_ascii=False),
            ex=config.CACHE_TTL_SECONDS,
        )
        return True
    except Exception as exc:
        logger.warning("缓存写入失败：%s", exc)
        return False


def get_stats() -> Dict[str, Any]:
    """
    返回当前缓存运行统计，供 /api/cache/stats 接口使用

    返回字段：
        - enabled: 缓存总开关状态（config.CACHE_ENABLED）
        - available: Redis 当前是否可用（连得上）
        - hit: 累计命中次数
        - miss: 累计未命中次数
        - total: hit + miss
        - hit_rate: 命中率（百分比，保留 2 位小数）；total=0 时返回 0
        - cached_keys: 当前缓存中已存在的答案条数（DBSIZE）
        - ttl_seconds: 配置的 TTL

    任何异常都返回降级版结构，不抛
    """
    stats = {
        "enabled": config.CACHE_ENABLED,
        "available": False,
        "hit": 0,
        "miss": 0,
        "total": 0,
        "hit_rate": 0.0,
        "cached_keys": 0,
        "ttl_seconds": config.CACHE_TTL_SECONDS,
    }
    client = _get_client()
    if client is None:
        return stats
    try:
        h = client.hgetall(config.CACHE_STATS_KEY)
        hit = int(h.get("hit", 0) or 0)
        miss = int(h.get("miss", 0) or 0)
        total = hit + miss
        # 只统计本项目前缀的 key 数（用 SCAN，避免 KEYS 阻塞）
        # 注意要排除 CACHE_STATS_KEY 这个 Hash，否则会被算进去
        cached_keys = 0
        stats_key = config.CACHE_STATS_KEY
        for k in client.scan_iter(match=f"{config.CACHE_KEY_PREFIX}*", count=200):
            if k == stats_key:
                continue
            cached_keys += 1
        stats.update({
            "available": True,
            "hit": hit,
            "miss": miss,
            "total": total,
            "hit_rate": round(hit * 100.0 / total, 2) if total > 0 else 0.0,
            "cached_keys": cached_keys,
        })
    except Exception as exc:
        logger.warning("读取缓存统计失败：%s", exc)
    return stats


def flush_cache() -> Dict[str, Any]:
    """
    清空本项目所有缓存（只清 CACHE_KEY_PREFIX 命名空间，不动其他业务数据）
    同时重置 HIT / MISS 统计计数器

    返回：
        {"flushed": int, "stats_reset": bool, "available": bool}
    """
    result = {"flushed": 0, "stats_reset": False, "available": False}
    client = _get_client()
    if client is None:
        return result
    try:
        # 用 SCAN+DEL 替代 FLUSHDB，避免误删同库其他数据
        # 同样要排除 CACHE_STATS_KEY，否则会被纳入 flushed 计数
        stats_key = config.CACHE_STATS_KEY
        keys = [
            k for k in client.scan_iter(
                match=f"{config.CACHE_KEY_PREFIX}*", count=500
            )
            if k != stats_key
        ]
        if keys:
            client.delete(*keys)
        client.delete(config.CACHE_STATS_KEY)
        result.update({
            "flushed": len(keys),
            "stats_reset": True,
            "available": True,
        })
    except Exception as exc:
        logger.warning("清空缓存失败：%s", exc)
    return result


def _safe_incr(client: redis.Redis, field: str) -> None:
    """统计计数器自增的兜底封装（失败不影响主流程）"""
    try:
        client.hincrby(config.CACHE_STATS_KEY, field, 1)
    except Exception:
        pass
