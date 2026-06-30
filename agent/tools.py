"""
Agent 工具注册模块

定义 Agent 可调用的工具列表和对应的执行函数
当前支持两个工具：
1. retrieve_knowledge: 从知识库检索相关文档
2. generate_answer: 基于检索结果生成回答

工具执行直接调用本地服务的 HTTP API，不走 FastAPI 路由
"""

import logging
import requests
from typing import List, Dict, Any

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import config

logger = logging.getLogger(__name__)


# ============== 工具定义（给 DeepSeek 的 function calling 格式） ==============

TOOLS_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "retrieve_knowledge",
            "description": "从知识库检索与用户问题相关的文档片段",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "检索关键词"
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回结果数量，默认5"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "generate_answer",
            "description": "基于检索到的文档片段生成自然语言回答",
            "parameters": {
                "type": "object",
                "properties": {
                    "context": {
                        "type": "string",
                        "description": "检索到的文档内容"
                    },
                    "question": {
                        "type": "string",
                        "description": "用户原始问题"
                    }
                },
                "required": ["context", "question"]
            }
        }
    }
]


def get_tool_definitions() -> List[Dict[str, Any]]:
    """
    获取工具定义列表（给 DeepSeek function calling 用）

    Returns:
        工具定义列表
    """
    return TOOLS_DEFINITIONS


def get_tool_names() -> List[str]:
    """
    获取所有工具名称列表

    Returns:
        工具名称列表
    """
    return [t["function"]["name"] for t in TOOLS_DEFINITIONS]


# ============== 工具执行函数 ==============


def execute_retrieve_knowledge(query: str, top_k: int = 5) -> Dict[str, Any]:
    """
    执行知识库检索

    直接调用 ES 进行 BM25 检索，不走完整的 RAG 链路
    速度快，适合 Agent 快速获取相关信息

    Args:
        query: 检索关键词
        top_k: 返回结果数量

    Returns:
        检索结果字典，包含 documents 列表
        失败时返回 {"error": "错误信息"}
    """
    try:
        logger.info(f"🔍 Agent 调用 retrieve_knowledge: query='{query}', top_k={top_k}")

        # 直接调 ES 进行 BM25 检索
        es_url = f"{config.ES_HOST}/{config.ES_INDEX_NAME}/_search"
        es_payload = {
            "query": {
                "multi_match": {
                    "query": query,
                    "fields": ["question^2", "answer"],
                    "type": "best_fields",
                }
            },
            "size": top_k,
            "_source": ["doc_id", "question", "answer", "category"],
        }

        auth = requests.auth.HTTPBasicAuth(config.ES_USER, config.ES_PASSWORD)
        response = requests.post(
            es_url,
            json=es_payload,
            auth=auth,
            timeout=10,
            verify=False,  # nosec B501: ES自签名证书，与项目其他模块一致
        )

        if response.status_code != 200:
            error_msg = f"ES 检索返回错误: HTTP {response.status_code}"
            logger.error(error_msg)
            return {"error": error_msg}

        result = response.json()
        hits = result.get("hits", {}).get("hits", [])

        documents = []
        for hit in hits:
            source = hit.get("_source", {})
            documents.append({
                "doc_id": source.get("doc_id", ""),
                "title": source.get("question", ""),
                "content": source.get("answer", ""),
                "category": source.get("category", ""),
                "score": hit.get("_score", 0),
            })

        return {
            "documents": documents,
            "total": len(documents),
        }

    except requests.RequestException as e:
        error_msg = f"ES 检索调用失败: {e}"
        logger.error(error_msg)
        return {"error": error_msg}
    except Exception as e:
        error_msg = f"检索执行异常: {e}"
        logger.error(error_msg, exc_info=True)
        return {"error": error_msg}


def execute_generate_answer(context: str, question: str) -> Dict[str, Any]:
    """
    执行答案生成

    直接调用本地 LLM 服务生成回答

    Args:
        context: 检索到的文档内容
        question: 用户原始问题

    Returns:
        生成结果字典，包含 answer 字段
        失败时返回 {"error": "错误信息"}
    """
    try:
        logger.info(f"🤖 Agent 调用 generate_answer: question='{question[:50]}...', context_len={len(context)}")

        # 构建 LLM 请求
        llm_url = config.LLM_API_URL
        system_prompt = config.LLM_SYSTEM_PROMPT

        payload = {
            "model": "qwen2.5-7b",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"上下文信息：\n{context}\n\n用户问题：{question}"}
            ],
            "temperature": config.LLM_TEMPERATURE,
            "max_tokens": config.LLM_MAX_TOKENS,
            "stream": False,
        }

        response = requests.post(
            llm_url,
            json=payload,
            timeout=30,
        )

        if response.status_code != 200:
            error_msg = f"生成服务返回错误: HTTP {response.status_code}"
            logger.error(error_msg)
            return {"error": error_msg}

        result = response.json()

        # 提取生成的文本
        choices = result.get("choices", [])
        if not choices:
            return {"error": "生成服务返回空结果"}

        answer = choices[0].get("message", {}).get("content", "")

        return {"answer": answer}

    except requests.RequestException as e:
        error_msg = f"生成服务调用失败: {e}"
        logger.error(error_msg)
        return {"error": error_msg}
    except Exception as e:
        error_msg = f"生成执行异常: {e}"
        logger.error(error_msg, exc_info=True)
        return {"error": error_msg}


# ============== 工具调度器 ==============

# 工具名称到执行函数的映射
_TOOL_EXECUTORS = {
    "retrieve_knowledge": execute_retrieve_knowledge,
    "generate_answer": execute_generate_answer,
}


def execute_tool(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """
    执行指定工具

    Args:
        tool_name: 工具名称
        arguments: 工具参数字典

    Returns:
        工具执行结果

    Raises:
        ValueError: 工具不存在
    """
    if tool_name not in _TOOL_EXECUTORS:
        raise ValueError(f"未知工具: {tool_name}，可用工具: {list(_TOOL_EXECUTORS.keys())}")

    executor = _TOOL_EXECUTORS[tool_name]
    logger.info(f"⚡ Agent 执行工具: {tool_name}, 参数: {arguments}")

    return executor(**arguments)
