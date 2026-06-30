#!/bin/bash
# 智能客服系统一键启动脚本
# 启动顺序：LLM模型 -> Embedding模型 -> Reranker模型 -> 等待初始化 -> API服务

echo "========================================"
echo "  智能客服系统 - 一键启动所有服务"
echo "========================================"
echo ""

# 获取脚本所在目录的绝对路径
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_ROOT/logs"
LLAMA_SERVER="/home/zbs/llama.cpp/build/bin/llama-server"
MODEL_BASE="/home/zbs/models"

echo "项目根目录: $PROJECT_ROOT"
echo "日志目录: $LOG_DIR"
echo ""

# 创建日志目录
mkdir -p "$LOG_DIR"

# 等待服务就绪函数
wait_for_service() {
    local NAME="$1"
    local URL="$2"
    local MAX_WAIT="${3:-60}"  # 默认最多等60秒
    local COUNT=0
    
    echo -n "等待 $NAME 就绪..."
    while [ $COUNT -lt $MAX_WAIT ]; do
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "$URL" 2>/dev/null)
        if [ "$HTTP_CODE" = "200" ]; then
            echo " ✅ 就绪"
            return 0
        fi
        sleep 2
        COUNT=$((COUNT + 2))
        echo -n "."
    done
    echo " ❌ 超时（超过${MAX_WAIT}秒）"
    echo "警告: $NAME 未能正常启动，请检查日志"
    return 1
}

# 第一步：启动LLM生成模型（最重要，先启动确保显存）
echo "【1/5】启动LLM生成模型（端口8080, GPU模式）..."
$LLAMA_SERVER \
  -m $MODEL_BASE/Qwen2.5-7B-Instruct-GGUF/Qwen2.5-7B-Instruct-Q3_K_M.gguf \
  --port 8080 -c 2048 -b 256 --n-gpu-layers 99 --fit off \
  > /tmp/llama_gen.log 2>&1 &
LLM_PID=$!
echo "  ✅ LLM进程已创建（PID: $LLM_PID）"
wait_for_service "LLM服务" "http://127.0.0.1:8080/health" 90
echo ""

# 第二步：启动Embedding向量模型
echo "【2/5】启动Embedding向量模型（端口8081, CPU模式）..."
$LLAMA_SERVER \
  -m $MODEL_BASE/Qwen3-Embedding-0.6B-GGUF/Qwen3-Embedding-0.6B-Q8_0.gguf \
  --port 8081 --embedding --n-gpu-layers 99 -c 768 \
  > /tmp/llama_emb.log 2>&1 &
EMB_PID=$!
echo "  ✅ Embedding进程已创建（PID: $EMB_PID）"
wait_for_service "Embedding服务" "http://localhost:8081/health" 30
echo ""

# 第三步：启动Reranker重排模型
echo "【3/5】启动Reranker重排模型（端口8082, CPU模式）..."
$LLAMA_SERVER \
  -m $MODEL_BASE/Qwen3-Reranker-0.6B-GGUF/Qwen3-Reranker-0.6B-q8_0.gguf \
  --port 8082 --reranking --n-gpu-layers 0 -c 768 -b 768 -ub 512 --fit off \
  > /tmp/llama_rerank.log 2>&1 &
RERANK_PID=$!
echo "  ✅ Reranker进程已创建（PID: $RERANK_PID）"
wait_for_service "Reranker服务" "http://localhost:8082/health" 30
echo ""

# 第四步：所有模型服务就绪
echo "【4/5】所有模型服务已就绪！"
echo ""

# 第五步：启动API服务
echo "【5/5】启动API服务..."
cd "$PROJECT_ROOT"

# 检查虚拟环境（优先使用localrag-cs的现成venv，其次项目内venv）
if [ -d "/home/zbs/localrag-cs1/venv" ]; then
    echo "  使用 localrag-cs1 的虚拟环境"
    VENV_PYTHON="/home/zbs/localrag-cs1/venv/bin/python"
elif [ -d "venv" ]; then
    echo "  使用项目内虚拟环境 venv"
    VENV_PYTHON="$PROJECT_ROOT/venv/bin/python"
else
    echo "  使用系统Python"
    VENV_PYTHON="python3"
fi

# 后台启动API服务
nohup $VENV_PYTHON -c "
import uvicorn
from api.app import app
uvicorn.run(app, host='0.0.0.0', port=8000)
" > "$LOG_DIR/api.log" 2>&1 &
API_PID=$!

echo "  ✅ API进程已创建（PID: $API_PID）"
wait_for_service "API服务" "http://localhost:8000/api/health" 90
echo ""

# 最终完整检查
echo "【完成】运行最终服务状态检查..."
bash "$SCRIPT_DIR/test_services.sh"
echo ""

# 打印使用提示
echo "========================================"
echo "  🎉 服务启动完成！"
echo "========================================"
echo ""
echo "  📊 健康检查地址: http://localhost:8000/api/health"
echo "  📚 API文档地址:   http://localhost:8000/docs"
echo "  📝 日志目录:"
echo "     API服务: $LOG_DIR/api.log"
echo "     LLM模型: /tmp/llama_gen.log"
echo "     Embedding: /tmp/llama_emb.log"
echo "     Reranker: /tmp/llama_rerank.log"
echo ""
echo "  ❓ 查看状态:  bash scripts/test_services.sh"
echo "  ⏹  停止服务:  bash scripts/stop_all.sh"
echo ""
