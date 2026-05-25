#!/usr/bin/env bash
# =====================================================================
# 优雅停止用户态 Redis（LocalRAG-CS 热点缓存）
# 用法：bash scripts/stop_redis.sh
# 优先用 redis-cli SHUTDOWN（会触发一次 RDB 快照，数据不丢失）
# =====================================================================
set -e

REDIS_HOME="$HOME/redis"
REDIS_CLI="$REDIS_HOME/bin/redis-cli"
PID_FILE="$REDIS_HOME/redis.pid"

# 没在跑就直接退出
if ! "$REDIS_CLI" -h 127.0.0.1 -p 6379 ping >/dev/null 2>&1; then
    echo "ℹ️  Redis 未在运行（127.0.0.1:6379）"
    exit 0
fi

echo "🛑 正在优雅停止 Redis..."

# 优先走 SHUTDOWN（会自动持久化 + 关闭监听）
if "$REDIS_CLI" -h 127.0.0.1 -p 6379 shutdown nosave 2>/dev/null; then
    echo "✅ Redis 已停止"
    exit 0
fi

# SHUTDOWN 失败时降级到 kill PID（兜底）
if [[ -f "$PID_FILE" ]]; then
    PID=$(cat "$PID_FILE")
    if kill -TERM "$PID" 2>/dev/null; then
        sleep 1
        echo "✅ Redis 已通过 kill 停止（PID=$PID）"
        exit 0
    fi
fi

echo "❌ Redis 停止失败，请手动检查"
exit 1
