"""
向量编码器模块 (llama.cpp HTTP API 版本)
使用 Qwen3-Embedding-0.6B GGUF 模型，通过 llama.cpp /v1/embeddings 接口获取向量
纯 requests 调用，零 sentence-transformers/torch 依赖
"""
from __future__ import annotations

import logging
from typing import List, Optional

import requests

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import config

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class EmbeddingClient:
    """
    文本向量编码器 (llama.cpp HTTP API 版本)
    
    调用本地 llama.cpp 服务的 /embeddings 接口
    使用 Qwen3-Embedding-0.6B GGUF 模型
    输出 1024 维向量
    """
    
    def __init__(
        self,
        api_url: Optional[str] = None,
        vector_dim: Optional[int] = None
    ):
        """
        初始化 Embedding 客户端
        
        Args:
            api_url: llama.cpp embeddings 接口地址，默认使用 http://localhost:8081/embeddings
            vector_dim: 向量维度，默认使用 config.EMBEDDING_DIM (1024)
        
        Raises:
            RuntimeError: 连接 llama.cpp 服务失败
        """
        # 处理 API URL
        if api_url:
            self.api_url = api_url
        else:
            self.api_url = "http://localhost:8081/embeddings"
        
        self.vector_dim = vector_dim or config.EMBEDDING_DIM
        
        logger.info(f"初始化 EmbeddingClient (API: {self.api_url}, 维度: {self.vector_dim})")
        
        # 测试连接
        try:
            self._test_connection()
            logger.info("llama.cpp 服务连接成功")
        except Exception as e:
            logger.warning(f"llama.cpp 服务连接测试失败: {e}，但客户端仍可创建")
    
    def _test_connection(self) -> None:
        """测试与 llama.cpp 服务的连接
        
        尝试调用实际的 embeddings 接口来验证服务可用性
        """
        try:
            payload = {
                "content": "test"
            }
            response = requests.post(
                self.api_url, 
                json=payload, 
                timeout=5
            )
            response.raise_for_status()
            logger.info(f"llama.cpp 服务连接成功 ({self.api_url})")
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"无法连接到 llama.cpp 服务: {e}")
            raise ConnectionError(
                f"无法连接到 llama.cpp 服务 ({self.api_url})。"
                f"请确保 llama.cpp 服务已启动并监听该端口。"
            ) from e
        except requests.exceptions.RequestException as e:
            logger.warning(f"llama.cpp 服务连接测试失败: {e}")
            # 非连接错误（如 404, 500）可能是服务存在但有问题，不阻断
    
    def _call_api(self, text: str, timeout: int = 30) -> List[float]:
        """
        调用 llama.cpp /embeddings 接口
        
        llama.cpp 响应格式:
        [{"index": 0, "embedding": [[...]]}]
        
        Args:
            text: 输入文本
            timeout: 请求超时时间(秒)
        
        Returns:
            List[float]: 向量列表
        
        Raises:
            ConnectionError: 连接失败
            RuntimeError: API 返回错误
            ValueError: 响应格式错误
        """
        payload = {
            "content": text
        }
        
        try:
            response = requests.post(self.api_url, json=payload, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            
            # 解析响应 (llama.cpp 原生格式)
            if isinstance(data, list) and len(data) > 0:
                embedding = data[0].get("embedding")
                if isinstance(embedding, list) and len(embedding) > 0:
                    # 处理嵌套数组的情况 [[...]]
                    if isinstance(embedding[0], list):
                        embedding = embedding[0]
                else:
                    raise ValueError(f"无效 embedding 格式: {type(embedding)}")
            else:
                raise ValueError(f"无法解析响应格式: {type(data)}")
            
            # 验证维度
            if len(embedding) != self.vector_dim:
                raise ValueError(
                    f"向量维度不匹配: 期望 {self.vector_dim}, 实际 {len(embedding)}"
                )
            
            return embedding
            
        except requests.exceptions.ConnectionError as e:
            raise ConnectionError(f"无法连接到 llama.cpp 服务 ({self.api_url}): {e}")
        except requests.exceptions.Timeout:
            raise ConnectionError(f"请求 llama.cpp 服务超时 ({timeout}s)")
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"API 请求失败: {e}")
    
    def encode(self, text: str) -> List[float]:
        """
        单文本编码
        
        Args:
            text: 输入文本
        
        Returns:
            List[float]: 1024 维向量列表
        
        Raises:
            ValueError: 输入文本为空
            ConnectionError: 连接 llama.cpp 失败
            RuntimeError: 编码失败
        """
        if not text or not text.strip():
            raise ValueError("输入文本不能为空")
        
        try:
            return self._call_api(text)
        except Exception as e:
            logger.error(f"文本编码失败: {e}")
            raise
    
    def encode_batch(self, texts: List[str], batch_size: int = 32) -> List[List[float]]:
        """
        批量文本编码
        
        Args:
            texts: 文本列表
            batch_size: 批次大小（当前逐条调用，此参数预留兼容）
        
        Returns:
            List[List[float]]: 向量列表，每个向量 1024 维
        
        Raises:
            ValueError: 输入文本列表为空
            ConnectionError: 连接 llama.cpp 失败
        """
        if not texts:
            raise ValueError("输入文本列表不能为空")
        
        # 过滤空文本
        valid_texts = [t for t in texts if t and t.strip()]
        if not valid_texts:
            raise ValueError("所有输入文本均为空")
        
        embeddings = []
        for i, text in enumerate(valid_texts):
            try:
                embedding = self.encode(text)
                embeddings.append(embedding)
                if (i + 1) % 10 == 0:
                    logger.info(f"批量编码进度: {i + 1}/{len(valid_texts)}")
            except Exception as e:
                logger.error(f"批量编码第 {i} 条失败: {e}")
                raise
        
        logger.info(f"批量编码完成: {len(valid_texts)} 条文本")
        return embeddings
    
    def get_dimension(self) -> int:
        """获取向量维度"""
        return self.vector_dim
    
    def is_ready(self) -> bool:
        """检查 llama.cpp 服务是否就绪"""
        try:
            self._test_connection()
            return True
        except Exception:
            return False


def create_encoder(api_url: Optional[str] = None, vector_dim: Optional[int] = None) -> EmbeddingClient:
    """
    创建 EmbeddingClient 实例的工厂函数
    
    Args:
        api_url: llama.cpp embeddings 接口地址
        vector_dim: 向量维度
    
    Returns:
        EmbeddingClient: 初始化好的客户端实例
    """
    return EmbeddingClient(api_url=api_url, vector_dim=vector_dim)
