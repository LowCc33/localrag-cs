"""
LocalRAG-CS 混合检索模块
实现BM25检索和RRF融合排序
"""
import logging
from typing import Optional, List, Dict, Any

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class HybridSearchMixin:
    """
    混合检索混合类
    
    提供BM25检索、RRF融合排序功能
    """
    
    def bm25_search(
        self,
        index_name: str,
        query: str,
        size: int = 100
    ) -> List[Dict[str, Any]]:
        """
        ES内置BM25全文检索
        
        Args:
            index_name: 索引名称
            query: 查询关键词
            size: 返回结果数量
        
        Returns:
            List[Dict]: BM25搜索结果列表
        """
        try:
            response = self.client.search(
                index=index_name,
                body={
                    'query': {
                        'multi_match': {
                            'query': query,
                            'fields': ['question^3', 'answer^2', 'category', 'tags'],
                            'type': 'best_fields',
                            'operator': 'or'
                        }
                    },
                    'size': size
                }
            )
            
            hits = response.get('hits', {}).get('hits', [])
            results = []
            for hit in hits:
                results.append({
                    '_id': hit.get('_id'),
                    '_score': hit.get('_score'),
                    '_index': hit.get('_index'),
                    '_source': hit.get('_source')
                })
            
            logger.info(f"BM25检索完成: query='{query}', 找到 {len(results)} 条结果")
            return results
        
        except Exception as e:
            logger.error(f"BM25检索失败: query='{query}', error={e}")
            return []
    
    def hybrid_search(
        self,
        index_name: str,
        query: str,
        query_vector: Optional[List[float]] = None,
        top_k: int = None,
        bm25_weight: float = 1.0,
        vector_weight: float = 1.0,
        rrf_k: int = None
    ) -> List[Dict[str, Any]]:
        """
        混合检索：同时调用BM25和向量检索，RRF融合排序
        
        RRF公式：score = Σ(1/(k + rank))
        k=60时，排名1的分数=1/61≈0.016，排名60的分数=1/120≈0.008
        
        Args:
            index_name: 索引名称
            query: 查询关键词
            query_vector: 查询向量，用于语义检索
            top_k: 返回结果数量，默认使用config.HYBRID_TOP_K
            bm25_weight: BM25结果权重
            vector_weight: 向量结果权重
            rrf_k: RRF常数k，默认60
        
        Returns:
            List[Dict]: 融合排序后的结果列表
        """
        top_k = top_k or config.RETRIEVE_TOP_K
        rrf_k = rrf_k or config.RRF_K
        
        # 并行执行两种检索
        bm25_results = self.bm25_search(index_name, query, size=100)
        
        vector_results = []
        if query_vector:
            vector_results = self.semantic_search(index_name, query_vector, size=100)
        
        # RRF融合
        return self._rrf_fusion(
            bm25_results, 
            vector_results, 
            top_k, 
            rrf_k,
            bm25_weight,
            vector_weight
        )
    
    def _rrf_fusion(
        self,
        bm25_results: List[Dict[str, Any]],
        vector_results: List[Dict[str, Any]],
        top_k: int,
        k: int,
        bm25_weight: float = 1.0,
        vector_weight: float = 1.0
    ) -> List[Dict[str, Any]]:
        """
        RRF (Reciprocal Rank Fusion) 融合排序
        
        公式：RRF_score = weight * Σ(1/(k + rank))
        对每个文档在两种检索中的排名进行加权融合
        
        Args:
            bm25_results: BM25检索结果列表
            vector_results: 向量检索结果列表
            top_k: 返回前K个结果
            k: RRF常数k
            bm25_weight: BM25结果权重
            vector_weight: 向量结果权重
        
        Returns:
            List[Dict]: 融合排序后的结果
        """
        # 构建文档ID到结果和排名的映射
        bm25_dict = {}
        for rank, result in enumerate(bm25_results):
            doc_id = result.get('_id')
            if doc_id:
                bm25_dict[doc_id] = {
                    'rank': rank,
                    'score': result.get('_score', 0),
                    'data': result
                }
        
        vector_dict = {}
        for rank, result in enumerate(vector_results):
            doc_id = result.get('_id')
            if doc_id:
                vector_dict[doc_id] = {
                    'rank': rank,
                    'score': result.get('_score', 0),
                    'data': result
                }
        
        # 收集所有唯一的文档ID
        all_doc_ids = set(bm25_dict.keys()) | set(vector_dict.keys())
        
        # 计算每个文档的RRF分数
        fused_scores = []
        for doc_id in all_doc_ids:
            rrf_score = 0.0
            
            # BM25贡献
            if doc_id in bm25_dict:
                rank = bm25_dict[doc_id]['rank']
                rrf_score += bm25_weight * (1.0 / (k + rank + 1))  # rank从0开始，所以+1
            
            # 向量贡献
            if doc_id in vector_dict:
                rank = vector_dict[doc_id]['rank']
                rrf_score += vector_weight * (1.0 / (k + rank + 1))
            
            # 获取文档数据（优先用BM25的结果，因为包含_source）
            if doc_id in bm25_dict:
                doc_data = bm25_dict[doc_id]['data']
            else:
                doc_data = vector_dict[doc_id]['data']
            
            fused_scores.append({
                '_id': doc_id,  # 保留_id字段，供去重使用
                'doc_id': doc_id,
                '_score': rrf_score,  # RRF融合后的分数（排序用，不显示）
                '_rrf_score': rrf_score,  # 同时保留原始RRF分数
                'bm25_score': bm25_dict.get(doc_id, {}).get('score', 0),  # ES原始分
                'vector_score': vector_dict.get(doc_id, {}).get('score', 0),  # 向量模型原始分
                'bm25_rank': bm25_dict.get(doc_id, {}).get('rank', None),
                'vector_rank': vector_dict.get(doc_id, {}).get('rank', None),
                '_source': doc_data.get('_source'),
                'question': doc_data.get('_source', {}).get('question'),
                'answer': doc_data.get('_source', {}).get('answer')
            })
        
        # 按RRF分数降序排序
        fused_scores.sort(key=lambda x: x['_rrf_score'], reverse=True)
        
        # 返回前top_k个结果
        logger.info(f"RRF融合完成: BM25召回{len(bm25_results)}条，向量召回{len(vector_results)}条，" 
                   f"融合后{len(fused_scores)}条，返回前{top_k}条")
        
        return fused_scores[:top_k]
