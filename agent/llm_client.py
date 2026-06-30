"""
DeepSeek-V4-Flash API 客户端

调用火山引擎 DeepSeek-V4-Flash 的 chat/completions 接口
支持 function calling 格式
超时 15 秒
失败时抛异常，由 agent.py 捕获并降级

使用 requests 库（项目已有），零额外依赖
"""

import json
import logging
import requests
from typing import Optional, List, Dict, Any

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import config

logger = logging.getLogger(__name__)


class DeepSeekClient:
    """
    DeepSeek-V4-Flash API 客户端

    用于 Agent 规划层的意图理解和工具调用决策
    不占本地显存，走火山引擎 API

    使用方式:
        client = DeepSeekClient()
        response = client.chat(messages, tools)
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ):
        """
        初始化 DeepSeek 客户端

        Args:
            api_key: API Key，优先读环境变量 DEEPSEEK_API_KEY
            api_url: API 地址，默认使用 config.DEEPSEEK_API_URL
            model: 模型名称，默认使用 config.DEEPSEEK_MODEL
            timeout: 请求超时秒数，默认使用 config.AGENT_TIMEOUT
        """
        # API Key 优先级：参数 > 环境变量 > config.py 兜底
        self.api_key = api_key or config.DEEPSEEK_API_KEY
        self.api_url = api_url or config.DEEPSEEK_API_URL
        self.model = model or config.DEEPSEEK_MODEL
        self.timeout = timeout if timeout is not None else config.AGENT_TIMEOUT

    def chat(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.1,
    ) -> Dict[str, Any]:
        """
        调用 DeepSeek chat/completions 接口

        Args:
            messages: 消息列表，格式为 [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}]
            tools: 工具定义列表（function calling 格式）
            temperature: 采样温度，Agent 决策用低温度保证确定性

        Returns:
            API 响应字典，包含 choices[0].message

        Raises:
            requests.RequestException: 网络错误/超时
            ValueError: API Key 未配置
        """
        if not self.api_key:
            raise ValueError("DeepSeek API Key 未配置，请设置 DEEPSEEK_API_KEY 环境变量")

        # 构建请求体
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }

        # 如果有工具定义，加入请求
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        # 构建请求头
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        logger.debug(f"DeepSeek 请求: model={self.model}, messages={len(messages)}条, tools={len(tools) if tools else 0}个")

        # 发送请求
        response = requests.post(
            self.api_url,
            headers=headers,
            json=payload,
            timeout=self.timeout,
        )

        # 检查 HTTP 状态码
        if response.status_code != 200:
            error_msg = f"DeepSeek API 返回错误: HTTP {response.status_code}"
            try:
                error_detail = response.json()
                error_msg += f", detail: {error_detail}"
            except Exception:
                error_msg += f", body: {response.text[:200]}"
            logger.error(error_msg)
            response.raise_for_status()

        result = response.json()

        logger.debug(f"DeepSeek 响应: usage={result.get('usage', {})}")

        return result

    def extract_tool_call(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        从 DeepSeek 返回的消息中提取工具调用信息

        Args:
            message: API 返回的 message 字典

        Returns:
            工具调用信息字典，格式为 {"name": "工具名", "arguments": {"参数名": "值"}}
            如果没有工具调用，返回 None
        """
        # 检查 tool_calls 字段
        tool_calls = message.get("tool_calls", [])
        if not tool_calls:
            return None

        # 取第一个工具调用
        tool_call = tool_calls[0]
        function_info = tool_call.get("function", {})

        try:
            arguments = json.loads(function_info.get("arguments", "{}"))
        except json.JSONDecodeError:
            logger.warning(f"工具调用参数解析失败: {function_info.get('arguments', '')}")
            arguments = {}

        return {
            "name": function_info.get("name", ""),
            "arguments": arguments,
        }

    def get_content(self, message: Dict[str, Any]) -> str:
        """
        从 DeepSeek 返回的消息中提取文本内容

        Args:
            message: API 返回的 message 字典

        Returns:
            文本内容字符串
        """
        return message.get("content", "") or ""
