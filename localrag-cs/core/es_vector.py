"""
LocalRAG-CS ES向量检索扩展模块
提供向量索引创建、向量文档插入、语义检索功能
"""
import logging
from typing import Optional, List, Dict, Any, Tuple
from elasticsearch import Elasticsearch, helpers

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


class VectorSearchMixin:
    """
    ES向量检索混合类
    
    通过mixin方式为ElasticsearchClient添加向量相关功能
    提供向量索引创建、向量文档插入、语义检索功能
    """
    
    def create_vector_index(
        self,
        index_name: str,
        vector_dim: Optional[int] = None
    ) -> bool:
        """
        创建支持向量检索的混合索引
        
        同时支持BM25全文检索和dense_vector向量检索
        使用IK中文分词器，配置HNSW向量索引
        
        Args:
            index_name: 索引名称
            vector_dim: 向量维度，默认使用 config.EMBEDDING_DIM (Qwen3-Embedding-0.6B 为 1024 维)
        
        Returns:
            bool: 创建是否成功
        """
        try:
            if self.index_exists(index_name):
                logger.warning(f"索引 {index_name} 已存在")
                return False
            
            mapping = config.HYBRID_INDEX_MAPPING.copy()
            if vector_dim and vector_dim != config.EMBEDDING_DIM:
                mapping['mappings']['properties']['embedding']['dims'] = vector_dim
            
            response = self.client.indices.create(index=index_name, body=mapping)
            
            if response.get('acknowledged'):
                logger.info(f"混合索引 {index_name} 创建成功")
                return True
            else:
                logger.error(f"混合索引创建失败: {response}")
                return False
        
        except Exception as e:
            logger.error(f"创建混合索引失败: {e}")
            return False
    
    def insert_vector_document(
        self,
        index_name: str,
        document: Dict[str, Any],
        doc_id: Optional[str] = None,
        embedding: Optional[List[float]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        插入向量文档（同时存入文本和向量）
        
        Args:
            index_name: 索引名称
            document: 文档内容字典，必须包含question字段
            doc_id: 文档ID，None时自动生成
            embedding: 预计算的向量，None时由外部编码后传入
        
        Returns:
            Optional[Dict]: 插入结果，包含_id等信息
        """
        try:
            # 添加向量字段（如果有）
            if embedding:
                document = document.copy()
                document['embedding'] = embedding
            
            response = self.client.index(
                index=index_name,
                id=doc_id,
                document=document
            )
            
            logger.info(f"向量文档插入成功: index={index_name}, id={response.get('_id')}")
            return {
                '_id': response.get('_id'),
                '_index': response.get('_index'),
                '_version': response.get('_version'),
                'result': response.get('result')
            }
        except Exception as e:
            logger.error(f"向量文档插入失败: index={index_name}, error={e}")
            return None
    
    def bulk_insert_vectors(
        self,
        index_name: str,
        documents: List[Dict[str, Any]],
        embeddings: Optional[List[List[float]]] = None
    ) -> bool:
        """
        批量插入向量文档
        
        Args:
            index_name: 索引名称
            documents: 文档列表，每个包含question等字段
            embeddings: 向量列表，与documents一一对应
        
        Returns:
            bool: 批量插入是否成功
        """
        try:
            # 准备bulk操作数据
            actions = []
            for i, doc in enumerate(documents):
                action_doc = {k: v for k, v in doc.items() if k != '_id'}
                # 添加向量（如果有）
                if embeddings and i < len(embeddings):
                    action_doc['embedding'] = embeddings[i]
                
                action = {
                    '_index': index_name,
                    '_source': action_doc
                }
                if '_id' in doc:
                    action['_id'] = doc['_id']
                actions.append(action)
            
            # 执行bulk操作
            success, errors = helpers.bulk(
                self.client,
                actions,
                raise_on_error=False
            )
            
            if errors:
                logger.error(f"批量插入出现 {len(errors)} 个错误")
                return False
            
            logger.info(f"批量向量插入成功: {success} 条文档")
            return True
        
        except Exception as e:
            logger.error(f"批量向量插入失败: {e}")
            return False
    
    def semantic_search(
        self,
        index_name: str,
        query_vector: List[float],
        size: int = 100,
        num_candidates: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        ES dense_vector 语义检索（原生 kNN）
        
        使用 ES 的原生 kNN 检索（HNSW 近似最近邻），相比 script_score+cosineSimilarity：
          - 性能更好（HNSW 索引而不是逐文档脚本计算）
          - 对缺失 embedding 字段的文档自动跳过，不会抛 script_exception
          - 评分语义为 cosine 相似度（与索引 mapping 的 similarity 一致）
        
        Args:
            index_name: 索引名称
            query_vector: 查询向量（Qwen3-Embedding-0.6B 为 config.EMBEDDING_DIM 维归一化向量）
            size: 返回结果数量
            num_candidates: HNSW 候选池大小，越大越精确越慢；默认取 max(size*4, 100)
        
        Returns:
            List[Dict]: 搜索结果列表，包含_id, _score, _source
        """
        try:
            if num_candidates is None:
                num_candidates = max(size * 4, 100)
            
            response = self.client.search(
                index=index_name,
                knn={
                    'field': 'embedding',
                    'query_vector': query_vector,
                    'k': size,
                    'num_candidates': num_candidates,
                },
                size=size,
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
            
            logger.info(f"语义检索完成(kNN): 找到 {len(results)} 条结果")
            return results
        
        except Exception as e:
            logger.error(f"语义检索失败: {e}")
            return []
