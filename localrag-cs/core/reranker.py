"""
Reranker 重排模块

对接 Qwen3-Reranker-0.6B GGUF 模型，通过 llama.cpp /v1/rerank 接口

纯 requests 调用，零重型依赖
"""

import logging
import requests
from typing import List, Dict, Any, Optional

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import config

logger = logging.getLogger(__name__)


class RerankerClient:
    """
    Reranker 客户端
    
    使用 llama.cpp 的 OpenAI 兼容 API 调用 Qwen3-Reranker-0.6B
    对检索结果进行相关性重排序
    
    服务启动命令示例:
    /home/zbs/llama.cpp/build/bin/llama-server \
        -m /home/zbs/models/Qwen3-Reranker-0.6B-GGUF/Qwen3-Reranker-0.6B-Q8_0.gguf \
        --port 8082 \
        --reranking
    """
    
    def __init__(
        self,
        api_url: Optional[str] = None,
        top_k: Optional[int] = None
    ):
        """
        初始化 Reranker 客户端
        
        Args:
            api_url: Reranker API 地址，默认使用 config.RERANKER_API_URL
            top_k: 返回前 K 个最相关的文档，默认使用 config.RERANKER_TOP_K
        """
        self.api_url = api_url or config.RERANKER_API_URL
        self.top_k = top_k or config.RERANKER_TOP_K
        
        logger.info(f"RerankerClient 初始化完成: api_url={self.api_url}, top_k={self.top_k}")
        
        # 测试连接
        self._test_connection()
    
    def _test_connection(self) -> bool:
        """
        测试 Reranker 服务连接
        
        Returns:
            bool: 连接成功返回 True，否则 False
        """
        try:
            # 发送一个简单的重排请求测试
            test_query = "测试查询"
            test_docs = ["测试文档1", "测试文档2"]
            
            payload = {
                "query": test_query,
                "documents": test_docs
            }
            
            response = requests.post(
                self.api_url,
                json=payload,
                timeout=5
            )
            
            if response.status_code == 200:
                logger.info("✓ Reranker 服务连接测试通过")
                return True
            else:
                logger.warning(f"⚠ Reranker 服务返回异常状态码: {response.status_code}")
                return False
                
        except requests.exceptions.ConnectionError:
            logger.error(f"✗ Reranker 服务连接失败，请检查服务是否启动 (port 8082)")
            return False
        except Exception as e:
            logger.error(f"✗ Reranker 服务连接测试异常: {e}")
            return False
    
    def rerank(
        self,
        query: str,
        documents: List[str]
    ) -> List[Dict[str, Any]]:
        """
        对文档列表进行相关性重排序
        
        Args:
            query: 查询文本
            documents: 待排序的文档列表（每个文档是一段文本）
        
        Returns:
            List[Dict]: 重排后的结果列表，每个元素包含:
                - index: 原文档索引
                - text: 文档内容
                - score: 相关性得分（0-1）
        
        示例:
            >>> client = RerankerClient()
            >>> docs = ["保险理赔流程是...", "医疗报销需要提供..."]
            >>> results = client.rerank("理赔需要什么材料", docs)
            >>> print(results[0]['score'])
            0.95
        """
        if not documents:
            logger.warning("rerank 收到空文档列表，直接返回空结果")
            return []
        
        if not query.strip():
            logger.warning("rerank 收到空查询，直接返回原文档顺序")
            return [
                {'index': i, 'text': doc, 'score': 0.0}
                for i, doc in enumerate(documents[:self.top_k])
            ]
        
        try:
            # 截断过长的文档，避免超过reranker的batch size限制
            # Qwen3-Reranker默认batch=512 tokens，单条文档建议不超过400字符（约200 token）
            MAX_DOC_LENGTH = 400
            truncated_docs = [
                doc[:MAX_DOC_LENGTH] if len(doc) > MAX_DOC_LENGTH else doc 
                for doc in documents
            ]
            
            # 构造 llama.cpp OpenAI 兼容格式的请求
            payload = {
                "query": query,
                "documents": truncated_docs
            }
            
            logger.info(f"调用 Reranker API: query='{query[:50]}...', documents={len(documents)}")
            
            response = requests.post(
                self.api_url,
                json=payload,
                timeout=30,
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            
            result = response.json()
            
            # 解析 llama.cpp 返回格式
            # 预期格式: {"results": [{"index": 0, "relevance_score": 0.95}, ...]}
            rerank_results = result.get('results', [])
            
            if not rerank_results:
                logger.warning("Reranker API 返回空结果，降级到原文档顺序")
                return [
                    {'index': i, 'text': documents[i], 'score': 0.5}
                    for i in range(min(self.top_k, len(documents)))
                ]
            
            # 格式化输出，只返回 top_k 个
            formatted_results = []
            for item in rerank_results[:self.top_k]:
                idx = item.get('index', 0)
                score = item.get('relevance_score', 0.0)
                
                # 安全检查索引范围
                if 0 <= idx < len(documents):
                    formatted_results.append({
                        'index': idx,
                        'text': documents[idx],
                        'score': score
                    })
            
            logger.info(f"Reranker 重排完成: 输入{len(documents)}条，返回前{len(formatted_results)}条")
            return formatted_results
            
        except requests.exceptions.Timeout:
            logger.error(f"Reranker API 请求超时 (>{30}s)，降级到原文档顺序")
            return [
                {'index': i, 'text': documents[i], 'score': 0.0}
                for i in range(min(self.top_k, len(documents)))
            ]
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Reranker API 连接失败: {e}，请检查服务是否启动 (port 8082)")
            return [
                {'index': i, 'text': documents[i], 'score': 0.0}
                for i in range(min(self.top_k, len(documents)))
            ]
        except Exception as e:
            logger.error(f"Reranker API 调用异常: {e}，降级到原文档顺序")
            return [
                {'index': i, 'text': documents[i], 'score': 0.0}
                for i in range(min(self.top_k, len(documents)))
            ]


# ========== 简单测试 ==========
if __name__ == "__main__":
    print("=" * 60)
    print("测试 RerankerClient")
    print("=" * 60)
    
    # 测试初始化
    client = RerankerClient()
    
    # 健康检查
    print("\n[1] 健康检查...")
    is_healthy = client._test_connection()
    print(f"    健康状态: {'✅ 正常' if is_healthy else '❌ 异常'}")
    
    # 重排测试
    print("\n[2] 重排功能测试...")
    query = "保险理赔流程"
    documents = [
        "社保缴纳流程包括：1. 单位登记 2. 人员增减 3. 费用申报",
        "保险理赔需要提供以下材料：身份证、保单、事故证明、费用清单",
        "医疗报销范围包括门诊、住院、药品费用等",
        "车辆年检需要携带行驶证、交强险保单、车主身份证"
    ]
    
    results = client.rerank(query, documents)
    
    print(f"    查询: {query}")
    print(f"    文档数: {len(documents)}")
    print(f"    返回结果: {len(results)} 条")
    print()
    for i, result in enumerate(results, 1):
        print(f"    [{i}] 相关性: {result['score']:.4f}")
        print(f"        内容: {result['text'][:60]}...")
        print()
    
    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)
