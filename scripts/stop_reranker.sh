#!/bin/bash
# 单独关闭 Reranker 重排模型服务
# 端口：8082

echo "========================================"
echo "  停止 Reranker 重排模型服务"
echo "========================================"
echo ""

# 方法1：根据端口查找进程
echo "【方法1】根据端口8082查找进程..."
PORT_PIDS=$(lsof -ti :8082 2>/dev/null)
if [ -n "$PORT_PIDS" ]; then
    echo "  找到端口8082进程: $PORT_PIDS"
    kill -9 $PORT_PIDS 2>/dev/null
    echo "  ✅ 已停止 Reranker 服务（端口方式）"
else
    echo "  ℹ️  端口8082未找到进程"
fi
echo ""

# 方法2：根据进程参数匹配（双重保险）
echo "【方法2】根据 --reranking 参数查找进程..."
RERANK_PIDS=$(pgrep -f "llama-server.*--reranking" 2>/dev/null)
if [ -n "$RERANK_PIDS" ]; then
    echo "  找到 Reranker 进程: $RERANK_PIDS"
    kill -9 $RERANK_PIDS 2>/dev/null
    echo "  ✅ 已停止 Reranker 服务（进程方式）"
else
    echo "  ℹ️  未找到带 --reranking 参数的进程"
fi
echo ""

# 验证是否成功停止
echo "【验证】检查服务状态..."
sleep 1
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "http://localhost:8082/health" 2>/dev/null)
if [ "$HTTP_CODE" != "200" ]; then
    echo "  ✅ Reranker 服务已成功停止"
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
echo "  🔄 重启 Reranker 服务："
echo "     请参考 start_all.sh 中的启动命令"
echo ""
