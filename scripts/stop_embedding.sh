#!/bin/bash
# 单独停止 Embedding 向量模型服务
# 端口：8081

echo "========================================"
echo "  停止 Embedding 向量模型服务"
echo "========================================"
echo ""

# 方法1：根据端口查找进程
echo "【方法1】根据端口8081查找进程..."
PORT_PIDS=$(lsof -ti :8081 2>/dev/null)
if [ -n "$PORT_PIDS" ]; then
    echo "  找到端口8081进程: $PORT_PIDS"
    kill -9 $PORT_PIDS 2>/dev/null
    echo "  ✅ 已停止 Embedding 服务（端口方式）"
else
    echo "  ℹ️  端口8081未找到进程"
fi
echo ""

# 方法2：根据进程参数匹配（双重保险）
echo "【方法2】根据 --embedding 参数查找进程..."
EMBED_PIDS=$(pgrep -f "llama-server.*--embedding" 2>/dev/null)
if [ -n "$EMBED_PIDS" ]; then
    echo "  找到 Embedding 进程: $EMBED_PIDS"
    kill -9 $EMBED_PIDS 2>/dev/null
    echo "  ✅ 已停止 Embedding 服务（进程方式）"
else
    echo "  ℹ️  未找到带 --embedding 参数的进程"
fi
echo ""

# 验证是否成功停止
echo "【验证】检查服务状态..."
sleep 1
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "http://localhost:8081/health" 2>/dev/null)
if [ "$HTTP_CODE" != "200" ]; then
    echo "  ✅ Embedding 服务已成功停止"
else
    echo "  ⚠️  警告：服务可能仍在运行，请手动检查"
fi
echo ""

echo "========================================"
echo "  完成"
echo "========================================"
echo ""
echo "  📝 查看所有 llama 进程："
echo "     ps aux | grep llama-server | grep -v grep"
echo ""
echo "  🔄 重启 Embedding 服务："
echo "     请参考 start_all.sh 中的启动命令"
echo ""
