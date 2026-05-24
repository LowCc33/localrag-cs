#!/bin/bash
# 智能客服系统服务状态检查脚本
# 一键检查5个核心服务状态：LLM、Embedding、Reranker、ES、API

echo "========================================"
echo "  智能客服系统 - 服务状态检查"
echo "========================================"
echo ""

# 默认配置
ES_HOST="https://192.168.1.3:9200"
LLM_URL="http://localhost:8080"
EMBEDDING_URL="http://localhost:8081"
RERANKER_URL="http://localhost:8082"
API_URL="http://localhost:8000"

SUCCESS=0
FAILED=0

# 检查函数
check_service() {
    local NAME="$1"
    local URL="$2"
    local AUTH="$3"  # 可选认证信息
    
    echo -n "  $NAME ... "
    
    # 尝试连接（带认证）
    if [ -n "$AUTH" ]; then
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 -k -u "$AUTH" "$URL" 2>/dev/null)
    else
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "$URL" 2>/dev/null)
    fi
    
    if [ "$HTTP_CODE" = "200" ]; then
        echo "✅ 正常 (HTTP: $HTTP_CODE)"
        SUCCESS=$((SUCCESS + 1))
        return 0
    else
        echo "❌ 异常 (HTTP: $HTTP_CODE)"
        FAILED=$((FAILED + 1))
        return 1
    fi
}

echo "服务地址："
echo "  - LLM:         $LLM_URL/health"
echo "  - Embedding:   $EMBEDDING_URL/health"
echo "  - Reranker:    $RERANKER_URL/health"
echo "  - Elasticsearch: $ES_HOST"
echo "  - API:         $API_URL/api/health"
echo ""
echo "开始检查..."
echo ""

# 检查所有服务
check_service "LLM 服务      " "$LLM_URL/health"
check_service "Embedding 服务" "$EMBEDDING_URL/health"
check_service "Reranker 服务 " "$RERANKER_URL/health"
check_service "Elasticsearch " "$ES_HOST" "elastic:Xw5sMLBqQuJfowJe8T*q"
check_service "API 服务      " "$API_URL/api/health"

echo ""
echo "========================================"
echo "  检查结果: $SUCCESS/5 正常"
echo "========================================"

if [ $FAILED -gt 0 ]; then
    echo ""
    echo "  ⚠️  $FAILED 个服务异常"
    exit 1
else
    echo ""
    echo "  ✅ 所有服务正常运行！"
    echo ""
    exit 0
fi
