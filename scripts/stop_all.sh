#!/bin/bash
# 智能客服系统一键停止脚本
# 停止所有相关进程：llama.cpp服务、Python API服务

echo "========================================"
echo "  智能客服系统 - 一键停止所有服务"
echo "========================================"
echo ""

echo "【1/3】停止 llama.cpp 服务进程..."
LLAMA_PIDS=$(pgrep -f "llama-server" 2>/dev/null)
if [ -n "$LLAMA_PIDS" ]; then
    echo "  找到 llama.cpp 进程: $LLAMA_PIDS"
    kill -9 $LLAMA_PIDS 2>/dev/null
    echo "  ✅ 已停止 llama.cpp 服务"
else
    echo "  ℹ️  未找到 llama.cpp 服务"
fi
echo ""

echo "【2/3】停止 Python API 服务进程..."
API_PIDS=$(pgrep -f "uvicorn.*app.*8000\|python.*app.py\|python.*-c.*uvicorn.*app" 2>/dev/null)
if [ -n "$API_PIDS" ]; then
    echo "  找到 API 进程: $API_PIDS"
    kill -9 $API_PIDS 2>/dev/null
    echo "  ✅ 已停止 API 服务"
else
    echo "  ℹ️  未找到 API 服务"
fi
echo ""

# 额外检查：杀所有8000/8080/8081/8082端口的进程
echo "【3/3】检查并清理残留端口进程..."
for PORT in 8000 8080 8081 8082; do
    PORT_PIDS=$(lsof -ti :$PORT 2>/dev/null)
    if [ -n "$PORT_PIDS" ]; then
        echo "  端口 $PORT 有残留进程，正在清理..."
        kill -9 $PORT_PIDS 2>/dev/null
    fi
done
echo ""

echo "========================================"
echo "  ✅ 所有服务已停止"
echo "========================================"
echo ""
echo "  ℹ️  确认所有进程已停止："
echo "     ps aux | grep -E 'llama-server|python' | grep -v grep"
echo ""
