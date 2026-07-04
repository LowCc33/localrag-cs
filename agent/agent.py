"""
Agent 核心模块

实现工具调用循环：
1. 接收用户问题
2. 调用 DeepSeek-V4-Flash 判断意图，决定调哪个工具
3. 执行工具调用
4. 把工具结果送回 DeepSeek，让它判断是否还需要调其他工具
5. 最多 3 轮循环，超时 30 秒
6. DeepSeek API 不可用时自动降级到原有 RAG 流程
"""

import json
import logging
import time
from typing import List, Dict, Any, Optional

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import config

from agent.llm_client import DeepSeekClient
from agent.tools import get_tool_definitions, execute_tool

logger = logging.getLogger(__name__)


# ============== Agent 系统提示词 ==============

AGENT_SYSTEM_PROMPT = """你是一个智能客服助手，负责理解用户问题并调用合适的工具来回答。

你的工作流程：
1. 分析用户问题，判断需要哪些信息
2. 调用 retrieve_knowledge 工具从知识库检索相关信息
3. 基于检索结果，调用 generate_answer 工具生成回答
4. 如果信息不足，可以多次调用 retrieve_knowledge 补充检索

注意事项：
- 先检索再回答，不要凭空编造信息
- 如果检索结果为空，告诉用户知识库中没有相关内容
- 回答要简洁、准确，使用中文
- 如果用户问题不明确，基于常识理解最合理的意图直接处理"""


class Agent:
    """
    Agent 核心类

    实现工具调用循环，支持：
    - 多轮工具调用（最多 AGENT_MAX_ROUNDS 轮）
    - DeepSeek API 降级到原有 RAG 流程
    - 工具执行重试（1次）
    - 完整 trace 记录

    使用方式:
        agent = Agent()
        result = agent.run("你们的退货政策是什么")
    """

    def __init__(
        self,
        deepseek_client: Optional[DeepSeekClient] = None,
        max_rounds: Optional[int] = None,
        timeout: Optional[int] = None,
    ):
        """
        初始化 Agent

        Args:
            deepseek_client: DeepSeek 客户端实例，不传则自动创建
            max_rounds: 最大工具调用轮数，默认使用 config.AGENT_MAX_ROUNDS
            timeout: 单轮超时秒数，默认使用 config.AGENT_TIMEOUT
        """
        self.deepseek = deepseek_client or DeepSeekClient()
        self.max_rounds = max_rounds if max_rounds is not None else config.AGENT_MAX_ROUNDS
        self.timeout = timeout if timeout is not None else config.AGENT_TIMEOUT
        self.tools = get_tool_definitions()

    def run(self, question: str) -> Dict[str, Any]:
        """
        执行 Agent 完整流程

        Args:
            question: 用户问题

        Returns:
            结果字典，格式为：
            {
                "answer": "最终回答",
                "agent_trace": [...],  # Agent 思考过程记录
                "status": "success" | "fallback" | "error",
                "error": "错误信息（可选）"
            }
        """
        start_time = time.time()
        trace = []  # Agent 思考过程记录

        try:
            # 构建初始消息列表
            messages = [
                {"role": "system", "content": AGENT_SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ]

            # 工具调用循环
            for round_num in range(1, self.max_rounds + 1):
                # 检查总超时
                elapsed = time.time() - start_time
                if elapsed > self.timeout:
                    logger.warning(f"Agent 总超时 ({elapsed:.1f}s > {self.timeout}s)，强制终止")
                    break

                logger.info(f"🔄 Agent 第 {round_num}/{self.max_rounds} 轮")

                # 调用 DeepSeek 做决策
                try:
                    response = self.deepseek.chat(
                        messages=messages,
                        tools=self.tools,
                    )
                except Exception as e:
                    # DeepSeek API 挂了，降级到原有 RAG 流程
                    logger.warning(f"DeepSeek API 调用失败，降级到 RAG 流程: {e}")
                    return self._fallback_to_rag(question, trace, str(e))

                # 提取 DeepSeek 的回复
                choice = response.get("choices", [{}])[0]
                message = choice.get("message", {})

                # 提取 DeepSeek 的思考过程（content 字段，function calling 模式下模型会在里面写"自言自语"）
                reasoning = self.deepseek.get_content(message)

                # 记录消息到历史
                messages.append(message)

                # 检查是否有工具调用
                tool_call = self.deepseek.extract_tool_call(message)

                if tool_call is None:
                    # DeepSeek 认为任务完成，提取最终回答
                    final_answer = self.deepseek.get_content(message)
                    logger.info(f"✅ Agent 任务完成（第 {round_num} 轮）")
                    return {
                        "answer": final_answer,
                        "agent_trace": trace,
                        "status": "success",
                    }

                # 执行工具调用
                tool_name = tool_call["name"]
                tool_args = tool_call["arguments"]

                trace_step = {
                    "step": round_num,
                    "action": f"调用{tool_name}",
                    "reasoning": reasoning,  # 记录 DeepSeek 的思考过程
                    "input": tool_args,
                    "output": None,
                }

                # 执行工具（带重试）
                tool_result = self._execute_tool_with_retry(tool_name, tool_args)
                trace_step["output"] = tool_result
                trace.append(trace_step)

                # generate_answer 是最终回答工具，执行完直接返回结果
                if tool_name == "generate_answer":
                    answer_text = tool_result.get("answer", "")
                    logger.info("✅ Agent 任务完成（generate_answer 返回最终回答）")
                    return {
                        "answer": answer_text,
                        "agent_trace": trace,
                        "status": "success",
                    }

                # 把工具结果加回消息列表
                # 格式化成 DeepSeek 能理解的 tool 消息
                messages.append({
                    "role": "tool",
                    "tool_call_id": message.get("tool_calls", [{}])[0].get("id", ""),
                    "content": json.dumps(tool_result, ensure_ascii=False),
                })

            # 超过最大轮数，从最后一条消息提取回答
            logger.warning(f"Agent 超过最大轮数 ({self.max_rounds})，强制终止")
            final_answer = self._extract_last_answer(messages)
            return {
                "answer": final_answer or "抱歉，处理您的请求时遇到了问题，请稍后重试。",
                "agent_trace": trace,
                "status": "success",
            }

        except Exception as e:
            # 兜底异常处理
            logger.error(f"Agent 执行异常: {e}", exc_info=True)
            return self._fallback_to_rag(question, trace, str(e))

    def _execute_tool_with_retry(self, tool_name: str, tool_args: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行工具（带1次重试）

        Args:
            tool_name: 工具名称
            tool_args: 工具参数

        Returns:
            工具执行结果
        """
        # 第一次执行
        result = execute_tool(tool_name, tool_args)

        # 如果失败，重试一次
        if "error" in result:
            logger.warning(f"工具 {tool_name} 第一次执行失败，重试中...")
            result = execute_tool(tool_name, tool_args)

        return result

    def _fallback_to_rag(self, question: str, trace: List[Dict], error: str) -> Dict[str, Any]:
        """
        降级到原有 RAG 流程

        当 DeepSeek API 不可用时，直接调本地 RAG 接口

        Args:
            question: 用户问题
            trace: 已有的 trace 记录
            error: 降级原因

        Returns:
            降级后的结果
        """
        logger.info(f"⬇️ 降级到 RAG 流程: {error}")

        try:
            # 直接调本地 /api/ask 接口
            import requests as req
            ask_url = "http://localhost:8000/api/ask"
            payload = {"question": question}

            response = req.post(ask_url, json=payload, timeout=30)

            if response.status_code == 200:
                result = response.json()
                return {
                    "answer": result.get("answer", ""),
                    "agent_trace": trace + [{
                        "step": len(trace) + 1,
                        "action": "降级到RAG流程",
                        "input": {"question": question},
                        "output": {"reason": error},
                    }],
                    "status": "fallback",
                }
            else:
                return {
                    "answer": "抱歉，服务暂时不可用，请稍后重试。",
                    "agent_trace": trace,
                    "status": "error",
                    "error": f"RAG 降级失败: HTTP {response.status_code}",
                }

        except Exception as e:
            logger.error(f"RAG 降级失败: {e}")
            return {
                "answer": "抱歉，服务暂时不可用，请稍后重试。",
                "agent_trace": trace,
                "status": "error",
                "error": f"RAG 降级异常: {e}",
            }

    def _extract_last_answer(self, messages: List[Dict]) -> str:
        """
        从消息列表中提取最后一条助手回复

        Args:
            messages: 消息列表

        Returns:
            最后一条助手回复的文本内容
        """
        for msg in reversed(messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                return msg["content"]
        return ""
