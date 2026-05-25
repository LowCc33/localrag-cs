#!/usr/bin/env bash
# =====================================================================
# 启动用户态 Redis（LocalRAG-CS 热点缓存）
# 部署目录：~/redis/   不依赖 sudo、不走 systemctl
# 用法：bash scripts/start_redis.sh
# =====================================================================
set -e

# Redis 安装目录（用户态部署）
REDIS_HOME="$HOME/redis"
REDIS_BIN="$REDIS_HOME/bin/redis-server"
REDIS_CLI="$REDIS_HOME/bin/redis-cli"
REDIS_CONF="$REDIS_HOME/etc/redis.conf"

# 启动前检查二进制和配置文件是否就绪
if [[ ! -x "$REDIS_BIN" ]]; then
    echo "❌ 未找到 redis-server 可执行文件：$REDIS_BIN"
    echo "   请先按 README.md 中『用户态 Redis 部署』章节编译安装"
    exit 1
fi
if [[ ! -f "$REDIS_CONF" ]]; then
    echo "❌ 未找到 Redis 配置文件：$REDIS_CONF"
    exit 1
fi

# 如果已经在跑，直接返回（幂等启动，避免重复拉起）
if "$REDIS_CLI" -h 127.0.0.1 -p 6379 ping >/dev/null 2>&1; then
    echo "✅ Redis 已在运行（127.0.0.1:6379），无需重复启动"
    exit 0
fi

# 启动 Redis（daemonize=yes 在配置文件里，已是后台运行）
"$REDIS_BIN" "$REDIS_CONF"

# 等待 Redis 就绪（最多等 5 秒，避免脚本退出过快）
for i in {1..10}; do
    if "$REDIS_CLI" -h 127.0.0.1 -p 6379 ping >/dev/null 2>&1; then
        echo "✅ Redis 启动成功：$($REDIS_CLI ping) (127.0.0.1:6379)"
        echo "   PID 文件：$REDIS_HOME/redis.pid"
        echo "   日志文件：$REDIS_HOME/log/redis.log"
        exit 0
    fi
    sleep 0.5
done

echo "❌ Redis 启动超时，请查看日志：$REDIS_HOME/log/redis.log"
exit 1
