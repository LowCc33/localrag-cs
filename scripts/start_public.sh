#!/bin/bash
# ==============================================
# 一键启动：ngrok + LocalRAG-CS 公网暴露
# 用法：./start_public.sh
# ==============================================
# 功能：
# 1. 检查 ngrok 是否已在运行
# 2. 如果没运行，启动 ngrok（后台），转发到 LocalRAG-CS 端口
# 3. 启动 LocalRAG-CS（如果没在运行）
# 4. 打印公网访问地址
# ==============================================

echo "========================================"
echo "  🌐 LocalRAG-CS 公网暴露启动脚本"
echo "========================================"
echo ""

# 获取脚本所在目录的绝对路径
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_ROOT/logs"
mkdir -p "$LOG_DIR"

# ========== 加载配置 ==========
# 从 config.py 读取 PUBLIC_URL 和 NGROK_TARGET_PORT
# 如果没有，使用默认值
PUBLIC_URL="${PUBLIC_URL:-https://skedaddle-morphine-shamrock.ngrok-free.dev}"
NGROK_PORT="${NGROK_PORT:-8080}"
API_PORT="${API_PORT:-8000}"
NGROK_PATH="$(which ngrok 2>/dev/null || echo '/usr/local/bin/ngrok')"

echo "  公网地址: $PUBLIC_URL"
echo "  ngrok端口: $NGROK_PORT"
echo "  API端口:   $API_PORT"
echo "  ngrok路径: $NGROK_PATH"
echo ""

# ========== 1. 检查 ngrok 是否已在运行 ==========
echo "【1/4】检查 ngrok 状态..."

NGROK_RUNNING=false
if curl -s --max-time 2 http://127.0.0.1:4040/api/tunnels > /dev/null 2>&1; then
    echo "  ✅ ngrok 已在运行"
    NGROK_RUNNING=true
else
    echo "  ⏳ ngrok 未运行，准备启动..."
fi
echo ""

# ========== 2. 启动 ngrok ==========
echo "【2/4】启动 ngrok 隧道..."

if [ "$NGROK_RUNNING" = false ]; then
    if [ ! -f "$NGROK_PATH" ]; then
        echo "  ❌ 找不到 ngrok 可执行文件: $NGROK_PATH"
        echo "  💡 请先安装 ngrok: https://ngrok.com/download"
        exit 1
    fi

    # 启动 ngrok，转发到 API 端口
    nohup "$NGROK_PATH" http "$API_PORT" --log=stdout > "$LOG_DIR/ngrok.log" 2>&1 &
    NGROK_PID=$!
    echo "  ✅ ngrok 进程已启动（PID: $NGROK_PID）"

    # 等待 ngrok 就绪（最多等 10 秒）
    echo -n "  等待 ngrok 就绪..."
    for i in $(seq 1 10); do
        if curl -s --max-time 2 http://127.0.0.1:4040/api/tunnels > /dev/null 2>&1; then
            echo " ✅ 就绪"
            NGROK_READY=true
            break
        fi
        sleep 1
        echo -n "."
    done

    if [ "$NGROK_READY" != true ]; then
        echo " ❌ ngrok 启动超时"
        echo "  查看日志: tail -f $LOG_DIR/ngrok.log"
    fi
else
    echo "  ✅ ngrok 已在运行，跳过启动"
fi
echo ""

# ========== 3. 检查/启动 LocalRAG-CS ==========
echo "【3/4】检查 LocalRAG-CS API 服务..."

API_RUNNING=false
if curl -s --max-time 2 http://localhost:"$API_PORT"/ping > /dev/null 2>&1; then
    echo "  ✅ LocalRAG-CS 已在运行（端口 $API_PORT）"
    API_RUNNING=true
else
    echo "  ⏳ LocalRAG-CS 未运行，准备启动..."
fi

if [ "$API_RUNNING" = false ]; then
    # 检查虚拟环境
    if [ -d "/home/zbs/localrag-cs1/venv" ]; then
        VENV_PYTHON="/home/zbs/localrag-cs1/venv/bin/python"
        echo "  使用 localrag-cs1 的虚拟环境"
    elif [ -d "$PROJECT_ROOT/venv" ]; then
        VENV_PYTHON="$PROJECT_ROOT/venv/bin/python"
        echo "  使用项目内虚拟环境"
    else
        VENV_PYTHON="python3"
        echo "  使用系统 Python"
    fi

    # 启动 API 服务
    cd "$PROJECT_ROOT"
    nohup $VENV_PYTHON -c "
import uvicorn
from api.app import app
uvicorn.run(app, host='0.0.0.0', port=$API_PORT)
" > "$LOG_DIR/api.log" 2>&1 &
    API_PID=$!
    echo "  ✅ API 进程已启动（PID: $API_PID）"

    # 等待 API 就绪
    echo -n "  等待 API 就绪..."
    for i in $(seq 1 15); do
        if curl -s --max-time 2 http://localhost:"$API_PORT"/ping > /dev/null 2>&1; then
            echo " ✅ 就绪"
            break
        fi
        sleep 2
        echo -n "."
    done
fi
echo ""

# ========== 4. 打印公网访问信息 ==========
echo "【4/4】获取公网访问信息..."
echo ""

# 从 ngrok API 获取实际公网地址
NGROK_URL=""
if curl -s --max-time 3 http://127.0.0.1:4040/api/tunnels > /dev/null 2>&1; then
    NGROK_URL=$(curl -s --max-time 3 http://127.0.0.1:4040/api/tunnels 2>/dev/null | \
        python3 -c "import sys,json; d=json.load(sys.stdin); tunnels=d.get('tunnels',[]); print(tunnels[0]['public_url'] if tunnels else '')" 2>/dev/null)
fi

if [ -z "$NGROK_URL" ]; then
    NGROK_URL="$PUBLIC_URL"
fi

echo "========================================"
echo "  🎉 公网暴露完成！"
echo "========================================"
echo ""
echo "  🌐 公网访问地址:"
echo "     $NGROK_URL"
echo "     $NGROK_URL/ping  (快速检查)"
echo "     $NGROK_URL/docs  (API文档)"
echo "     $NGROK_URL/api/public/health  (公网健康检查)"
echo ""
echo "  🔒 本地访问地址:"
echo "     http://localhost:$API_PORT"
echo "     http://localhost:$API_PORT/docs"
echo ""
echo "  📊 ngrok 管理面板:"
echo "     http://127.0.0.1:4040"
echo ""
echo "  📝 日志文件:"
echo "     API:     $LOG_DIR/api.log"
echo "     ngrok:   $LOG_DIR/ngrok.log"
echo ""
echo "  ⏹  停止服务:  bash scripts/stop_all.sh"
echo ""
