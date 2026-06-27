"""
LocalRAG-CS ES客户端核心模块（阶段2完整版）
整合：基础CRUD + 向量索引 + 混合检索

⚠️ 注意：本文件只包含ES基础连接和CRUD功能
搜索相关方法（bm25_search、hybrid_search）定义在 es_hybrid.py 的 HybridSearchMixin 中
向量搜索方法定义在 es_vector.py 的 VectorSearchMixin 中
实际使用时通过多继承方式将三个类组合成完整的ES客户端
这是Mixin设计模式的正常写法，不是方法不存在
"""
import logging
import time
from typing import Optional, List, Dict, Any, Union
from elasticsearch import Elasticsearch, helpers
from elasticsearch.exceptions import NotFoundError

# 导入混合类
from core.es_vector import VectorSearchMixin
from core.es_hybrid import HybridSearchMixin

import config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class ElasticsearchClient(VectorSearchMixin, HybridSearchMixin):
    """
    ES客户端（阶段2完整版）
    
    继承VectorSearchMixin和HybridSearchMixin，提供：
    - 基础CRUD操作
    - 向量索引管理
    - BM25检索、语义检索、混合检索
    - RRF融合排序
    """
    
    def __init__(
        self,
        host: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: Optional[int] = None,
        verify_certs: Optional[bool] = None,
        max_retries: int = 3,
        retry_delay: int = 1
    ):
        self.host = host or config.ES_HOST
        self.username = username or config.ES_USER
        self.password = password or config.ES_PASSWORD
        self.timeout = timeout or config.ES_TIMEOUT
        self.verify_certs = verify_certs if verify_certs is not None else config.ES_VERIFY_CERTS
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.client = self._create_client()
    
    def _create_client(self) -> Elasticsearch:
        try:
            client = Elasticsearch(
                [self.host],
                basic_auth=(self.username, self.password) if self.username and self.password else None,
                verify_certs=self.verify_certs,
                request_timeout=self.timeout
            )
            self.client = client
            if not self._ping_with_retry():
                logger.warning(f"无法连接到ES服务器: {self.host}")
            return client
        except Exception as e:
            logger.error(f"创建ES客户端失败: {e}")
            raise
    
    def _ping_with_retry(self) -> bool:
        for attempt in range(self.max_retries):
            try:
                if self.client.ping():
                    return True
            except Exception as e:
                logger.warning(f"Ping尝试 {attempt + 1}/{self.max_retries} 失败: {e}")
            if attempt < self.max_retries - 1:
                time.sleep(self.retry_delay)
        return False
    
    # ========== 基础索引管理 ==========
    
    def create_index(self, index_name: str, mappings: Optional[Dict] = None, settings: Optional[Dict] = None) -> bool:
        try:
            if self.index_exists(index_name):
                logger.warning(f"索引 {index_name} 已存在")
                return False
            body = {}
            if settings:
                body['settings'] = settings
            if mappings:
                body['mappings'] = mappings
            else:
                body['mappings'] = config.DEFAULT_MAPPING.get('mappings', {})
            response = self.client.indices.create(index=index_name, body=body)
            if response.get('acknowledged'):
                logger.info(f"索引 {index_name} 创建成功")
                return True
            else:
                logger.error(f"索引 {index_name} 创建失败: {response}")
                return False
        except Exception as e:
            logger.error(f"创建索引 {index_name} 失败: {e}")
            return False
    
    def delete_index(self, index_name: str) -> bool:
        try:
            if not self.index_exists(index_name):
                logger.warning(f"索引 {index_name} 不存在")
                return False
            response = self.client.indices.delete(index=index_name)
            if response.get('acknowledged'):
                logger.info(f"索引 {index_name} 删除成功")
                return True
            else:
                logger.error(f"索引 {index_name} 删除失败: {response}")
                return False
        except Exception as e:
            logger.error(f"删除索引 {index_name} 失败: {e}")
            return False
    
    def index_exists(self, index_name: str) -> bool:
        try:
            return self.client.indices.exists(index=index_name)
        except Exception as e:
            logger.error(f"检查索引 {index_name} 存在性失败: {e}")
            return False
    
    def list_indices(self) -> List[str]:
        try:
            response = self.client.indices.get_alias(index="*")
            return list(response.keys())
        except Exception as e:
            logger.error(f"列出索引失败: {e}")
            return []
    
    # ========== 基础CRUD操作 ==========
    
    def insert_document(self, index_name: str, document: Dict[str, Any], doc_id: Optional[str] = None) -> Optional[Dict]:
        try:
            response = self.client.index(index=index_name, id=doc_id, document=document)
            logger.info(f"文档插入成功: index={index_name}, id={response.get('_id')}")
            return {
                '_id': response.get('_id'),
                '_index': response.get('_index'),
                '_version': response.get('_version'),
                'result': response.get('result')
            }
        except Exception as e:
            logger.error(f"文档插入失败: index={index_name}, error={e}")
            return None
    
    def get_document(self, index_name: str, doc_id: str) -> Optional[Dict[str, Any]]:
        try:
            # ES 8+ 默认从 _source 中排除 dense_vector 字段以节省存储
            # 使用 _source_includes=* 确保返回所有字段（包括embedding）
            response = self.client.get(index=index_name, id=doc_id, _source_includes='*')
            return {
                '_id': response.get('_id'),
                '_index': response.get('_index'),
                '_version': response.get('_version'),
                '_source': response.get('_source'),
                'found': response.get('found', True)
            }
        except NotFoundError:
            logger.warning(f"文档不存在: index={index_name}, id={doc_id}")
            return None
        except Exception as e:
            logger.error(f"获取文档失败: index={index_name}, id={doc_id}, error={e}")
            return None
    
    def update_document(self, index_name: str, doc_id: str, document: Dict[str, Any]) -> bool:
        try:
            response = self.client.update(index=index_name, id=doc_id, doc=document)
            logger.info(f"文档更新成功: index={index_name}, id={doc_id}")
            return response.get('result') in ['updated', 'noop']
        except Exception as e:
            logger.error(f"文档更新失败: index={index_name}, id={doc_id}, error={e}")
            return False
    
    def delete_document(self, index_name: str, doc_id: str) -> bool:
        try:
            response = self.client.delete(index=index_name, id=doc_id)
            logger.info(f"文档删除成功: index={index_name}, id={doc_id}")
            return response.get('result') == 'deleted'
        except Exception as e:
            logger.error(f"文档删除失败: index={index_name}, id={doc_id}, error={e}")
            return False
    
    def bulk_insert(self, index_name: str, documents: List[Dict[str, Any]]) -> bool:
        try:
            actions = []
            for doc in documents:
                action = {
                    '_index': index_name,
                    '_source': {k: v for k, v in doc.items() if k != '_id'}
                }
                if '_id' in doc:
                    action['_id'] = doc['_id']
                actions.append(action)
            
            success, errors = helpers.bulk(self.client, actions, raise_on_error=False)
            if errors:
                logger.error(f"批量插入出现 {len(errors)} 个错误")
                return False
            logger.info(f"批量插入成功: {success} 条文档")
            return True
        except Exception as e:
            logger.error(f"批量插入失败: error={e}")
            return False
