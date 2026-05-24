"""
LLM 生成模块

对接 Qwen2.5-7B-Instruct GGUF 模型，通过 llama.cpp /v1/chat/completions 接口

纯 requests 调用，零 openai SDK 依赖
"""

import json
import logging
import requests
from typing import Optional, Generator

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import config

logger = logging.getLogger(__name__)


class LLMClient:
    """
    LLM 客户端

    使用 llama.cpp 的 OpenAI 兼容 API 调用 Qwen2.5-7B-Instruct
    基于检索到的上下文生成自然语言回答

    服务启动命令示例:
    /home/zbs/llama.cpp/build/bin/llama-server \
        -m /home/zbs/models/Qwen2.5-7B-Instruct-GGUF/Qwen2.5-7B-Instruct-Q4_K_M.gguf \
        --port 8080
    """

    def __init__(
        self,
        api_url: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None
    ):
        """
        初始化 LLM 客户端

        Args:
            api_url: LLM API 地址，默认使用 config.LLM_API_URL
            temperature: 采样温度，默认使用 config.LLM_TEMPERATURE
            max_tokens: 最大生成token数，默认使用 config.LLM_MAX_TOKENS
        """
        self.api_url = api_url or config.LLM_API_URL
        self.temperature = temperature if temperature is not None else config.LLM_TEMPERATURE
        self.max_tokens = max_tokens if max_tokens is not None else config.LLM_MAX_TOKENS

        logger.info(f"LLMClient 初始化完成: api_url={self.api_url}, temperature={self.temperature}, max_tokens={self.max_tokens}")

        # 测试连接
        self._test_connection()

    def _test_connection(self) -> bool:
        """
        测试 LLM 服务连接

        Returns:
            bool: 连接成功返回 True，否则 False
        """
        try:
            # 发送一个简单的请求测试
            payload = {
                "model": "qwen",
                "messages": [
                    {"role": "user", "content": "ping"}
                ],
                "max_tokens": 5,
                "temperature": 0.1
            }

            response = requests.post(
                self.api_url,
                json=payload,
                timeout=5
            )

            if response.status_code == 200:
                logger.info("✓ LLM 服务连接测试通过")
                return True
            else:
                logger.warning(f"⚠ LLM 服务返回异常状态码: {response.status_code}")
                return False

        except requests.exceptions.ConnectionError:
            logger.error(f"✗ LLM 服务连接失败，请检查服务是否启动 (port 8080)")
            return False
        except Exception as e:
            logger.error(f"✗ LLM 服务连接测试异常: {e}")
            return False

    def generate(self, context: str, query: str) -> str:
        """
        基于上下文生成回答

        Args:
            context: 检索到的上下文信息
            query: 用户查询

        Returns:
            str: 生成的回答文本

        示例:
            >>> client = LLMClient()
            >>> context = "保险理赔流程包括：1. 报案 2. 提交材料..."
            >>> query = "怎么申请保险理赔？"
            >>> answer = client.generate(context, query)
            >>> print(answer)
            根据资料，理赔需要准备以下材料：...
        """
        if not query.strip():
            logger.warning("generate 收到空查询，返回提示信息")
            return "抱歉，没有收到您的问题。"

        if not context.strip():
            logger.warning("generate 收到空上下文")
            context = "暂无相关资料。"

        try:
            # 构建用户提示词
            user_prompt = f"上下文信息:\n{context}\n\n问题: {query}"

            # 构造 OpenAI 兼容格式的请求
            payload = {
                "model": "qwen",
                "messages": [
                    {"role": "system", "content": config.LLM_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "stream": False
            }

            logger.info(f"调用 LLM API: query='{query[:50]}...'")

            response = requests.post(
                self.api_url,
                json=payload,
                timeout=60,
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()

            result = response.json()

            # 解析响应
            choices = result.get('choices', [])
            if not choices:
                logger.error("LLM API 返回空 choices")
                return "生成回答时出错，请稍后重试。"

            answer = choices[0].get('message', {}).get('content', '').strip()

            logger.info(f"LLM 生成完成: answer_length={len(answer)}")

            return answer if answer else "抱歉，无法生成回答。"

        except requests.exceptions.Timeout:
            logger.error(f"LLM API 请求超时 (>{60}s)")
            return "生成回答超时，请稍后重试。"
        except requests.exceptions.ConnectionError as e:
            logger.error(f"LLM API 连接失败: {e}，请检查服务是否启动 (port 8080)")
            return "无法连接到语言模型服务，请检查服务是否启动。"
        except Exception as e:
            logger.error(f"LLM API 调用异常: {e}")
            return "生成回答时发生错误，请稍后重试。"

    def generate_stream(self, context: str, query: str) -> Generator[str, None, None]:
        """
        基于上下文以流式方式生成回答（SSE）

        通过 llama.cpp 的 OpenAI 兼容流式接口逐 Token 返回内容。
        本方法是一个生成器：调用方每收到一个 token 字符串就可以立刻推给前端，
        从而实现首字 1.5~2 秒到达、整体打字机效果。

        Args:
            context: 检索到的上下文信息
            query: 用户查询

        Yields:
            str: 单个 token 文本片段（按 llama.cpp 返回切分，可能是 1~N 个字符）

        异常处理：
            - 网络/超时/解析异常时，会先 yield 一段中文降级提示，然后正常结束。
            - 不会抛出异常打断调用方，便于上层 SSE 端点统一收尾。
        """
        # 空查询保护：直接返回提示，不发起远程请求
        if not query.strip():
            logger.warning("generate_stream 收到空查询，返回提示信息")
            yield "抱歉，没有收到您的问题。"
            return

        # 空上下文兜底：仍然继续调用，但用占位文本，避免模型胡编
        if not context.strip():
            logger.warning("generate_stream 收到空上下文")
            context = "暂无相关资料。"

        # 构建用户提示词（与非流式 generate 保持一致，保证输出风格统一）
        user_prompt = f"上下文信息:\n{context}\n\n问题: {query}"

        # 流式请求体：stream=true 是关键，告诉 llama.cpp 用 SSE 推送
        payload = {
            "model": "qwen",
            "messages": [
                {"role": "system", "content": config.LLM_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True
        }

        logger.info(f"调用 LLM 流式 API: query='{query[:50]}...'")

        # 累计是否输出过 token，用于判断空响应降级
        emitted_any = False

        try:
            # 注意：stream=True 让 requests 不一次性把响应读完
            # timeout=(连接超时, 读取超时) 二元组，分别控制建链和单包读取
            with requests.post(
                self.api_url,
                json=payload,
                timeout=(5, config.LLM_STREAM_READ_TIMEOUT),
                headers={"Content-Type": "application/json"},
                stream=True
            ) as response:
                response.raise_for_status()

                # 按行迭代 SSE 数据流；llama.cpp 的格式遵循 OpenAI:
                #   data: {"choices":[{"delta":{"content":"你"}}]}
                #   data: [DONE]
                for raw_line in response.iter_lines(decode_unicode=True):
                    # 跳过 keep-alive 空行
                    if not raw_line:
                        continue

                    line = raw_line.strip()
                    # 兼容某些实现返回的注释行（以 ":" 开头）
                    if line.startswith(":"):
                        continue
                    # 兼容部分实现不带 data: 前缀的情况
                    if line.startswith("data:"):
                        line = line[len("data:"):].strip()

                    # 流结束标记
                    if line == "[DONE]":
                        break

                    # 跳过无法解析的内容，避免单条坏数据搞挂整个流
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        logger.debug(f"忽略无法解析的 SSE 行: {line[:120]}")
                        continue

                    # 解析 OpenAI 兼容格式：choices[0].delta.content
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {}) or {}
                    token = delta.get("content", "")
                    if not token:
                        # 也兼容某些非标准实现把内容塞在 message.content 里
                        token = (choices[0].get("message", {}) or {}).get("content", "")
                    if token:
                        emitted_any = True
                        yield token

            # 流正常结束但没有任何输出，给出明确兜底提示
            if not emitted_any:
                logger.warning("LLM 流式响应未产生任何 token，返回兜底提示")
                yield "抱歉，无法生成回答。"

            logger.info("LLM 流式生成完成")

        except requests.exceptions.Timeout:
            # 读取超时（单包等待超过 LLM_STREAM_READ_TIMEOUT）
            logger.error(
                f"LLM 流式请求读取超时 (>{config.LLM_STREAM_READ_TIMEOUT}s)，已中断"
            )
            # 若已经输出过部分内容，追加中断提示；否则给完整降级
            if emitted_any:
                yield "\n\n[生成中断：响应超时，请重试]"
            else:
                yield "生成回答超时，请稍后重试。"
        except requests.exceptions.ConnectionError as e:
            logger.error(f"LLM 流式 API 连接失败: {e}，请检查服务是否启动 (port 8080)")
            yield "无法连接到语言模型服务，请检查服务是否启动。"
        except Exception as e:
            # 兜底异常：保证调用方不会因为底层错误而崩溃
            logger.error(f"LLM 流式 API 调用异常: {e}", exc_info=True)
            if emitted_any:
                yield "\n\n[生成中断：发生未知错误]"
            else:
                yield "生成回答时发生错误，请稍后重试。"


# ========== 简单测试 ==========
if __name__ == "__main__":
    print("=" * 60)
    print("测试 LLMClient")
    print("=" * 60)

    # 测试初始化
    client = LLMClient()

    # 健康检查
    print("\n[1] 健康检查...")
    is_healthy = client._test_connection()
    print(f"    健康状态: {'✅ 正常' if is_healthy else '❌ 异常'}")

    # 生成测试
    print("\n[2] 生成回答测试...")
    context = """保险理赔流程包括以下几个步骤：
1. 报案：出险后及时拨打保险公司客服电话报案
2. 查勘定损：保险公司派人到现场查勘，确定损失情况
3. 提交材料：准备身份证、保单、事故证明等相关材料
4. 审核：保险公司对材料进行审核
5. 赔付：审核通过后进行赔付
一般车险理赔需要3-5个工作日完成。"""

    query = "车险理赔流程是什么？"

    answer = client.generate(context, query)

    print(f"    查询: {query}")
    print(f"    上下文长度: {len(context)} 字符")
    print(f"    回答长度: {len(answer)} 字符")
    print()
    print("    回答内容:")
    print("    " + answer.replace('\n', '\n    '))

    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)
