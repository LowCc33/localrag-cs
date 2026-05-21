#!/bin/bash
# 单独停止 LLM 生成模型服务
# 端口：8080

echo "========================================"
echo "  停止 LLM 生成模型服务"
echo "========================================"
echo ""

# 方法1：根据端口查找进程
echo "【方法1】根据端口8080查找进程..."
PORT_PIDS=$(lsof -ti :8080 2>/dev/null)
if [ -n "$PORT_PIDS" ]; then
    echo "  找到端口8080进程: $PORT_PIDS"
    kill -9 $PORT_PIDS 2>/dev/null
    echo "  ✅ 已停止 LLM 服务（端口方式）"
else
    echo "  ℹ️  端口8080未找到进程"
fi
echo ""

# 方法2：排除法查找进程（排除rerank和embedding，剩下的就是LLM）
echo "【方法2】排除 rerank/embedding 查找 LLM 进程..."
ALL_LLAMA_PIDS=$(pgrep -f "llama-server" 2>/dev/null)
RERANK_PIDS=$(pgrep -f "llama-server.*--reranking" 2>/dev/null)
EMBED_PIDS=$(pgrep -f "llama-server.*--embedding" 2>/dev/null)

# 构建排除列表
EXCLUDE_PIDS="$RERANK_PIDS $EMBED_PIDS"

# 找出不在排除列表中的llama-server进程
LLM_PIDS=""
for pid in $ALL_LLAMA_PIDS; do
    if ! echo " $EXCLUDE_PIDS " | grep -q " $pid "; then
        LLM_PIDS="$LLM_PIDS $pid"
    fi
done

if [ -n "$LLM_PIDS" ]; then
    echo "  找到 LLM 进程: $LLM_PIDS"
    kill -9 $LLM_PIDS 2>/dev/null
    echo "  ✅ 已停止 LLM 服务（进程方式）"
else
    echo "  ℹ️  未找到 LLM 生成服务进程"
fi
echo ""

# 验证是否成功停止
echo "【验证】检查服务状态..."
sleep 1
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "http://localhost:8080/health" 2>/dev/null)
if [ "$HTTP_CODE" != "200" ]; then
    echo "  ✅ LLM 服务已成功停止"
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
echo "  🔄 重启 LLM 服务："
echo "     请参考 start_all.sh 中的启动命令"
echo ""
