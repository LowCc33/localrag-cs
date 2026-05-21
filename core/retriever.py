"""
检索器封装模块
包含查询重写、混合检索、结果重排等完整检索流水线
"""
from __future__ import annotations

import logging
import re
from typing import List, Dict, Any, Optional, TYPE_CHECKING

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

if TYPE_CHECKING:
    from core.es_client import ElasticsearchClient
    from core.embedding import EmbeddingClient as VectorEncoder

import config

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class QueryRewrite:
    """
    查询重写模块
    
    功能:
    - 中文纠错（全角转半角、去多余空格）
    - 同义词扩展
    - 意图识别（简单规则）
    """
    
    # 默认同义词词典
    DEFAULT_SYNONYMS = {
        '医保': '医疗保险',
        '社保': '社会保险',
        '报销': '理赔',
        '看病': '就医',
        '大病': '重大疾病',
        '住院': '入院',
        '门诊': '门急诊',
        '缴费': '交费',
        '钱': '费用',
        '怎么': '如何'
    }
    
    def __init__(self, synonym_dict: Optional[Dict[str, str]] = None):
        """
        初始化查询重写模块
        
        Args:
            synonym_dict: 自定义同义词词典，None则使用默认
        """
        self.synonym_dict = synonym_dict or self.DEFAULT_SYNONYMS
    
    @staticmethod
    def fullwidth_to_halfwidth(text: str) -> str:
        """
        全角字符转半角
        
        Args:
            text: 输入文本
        
        Returns:
            转换后的文本
        """
        result = []
        for char in text:
            code = ord(char)
            # 全角空格转换
            if code == 0x3000:
                result.append(' ')
            # 其他全角字符转换
            elif 0xFF01 <= code <= 0xFF5E:
                result.append(chr(code - 0xFEE0))
            else:
                result.append(char)
        return ''.join(result)
    
    @staticmethod
    def remove_extra_spaces(text: str) -> str:
        """
        去除多余空格，多个空格合并为一个
        
        Args:
            text: 输入文本
        
        Returns:
            处理后的文本
        """
        return re.sub(r'\s+', ' ', text).strip()
    
    @staticmethod
    def clean_special_chars(text: str) -> str:
        """
        清除特殊字符，保留中文、英文、数字
        
        Args:
            text: 输入文本
        
        Returns:
            处理后的文本
        """
        # 保留中文、英文、数字、常见标点
        return re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9，。！？、；：""''（）\s]', '', text)
    
    def synonym_expand(self, query: str) -> str:
        """
        同义词扩展
        
        Args:
            query: 查询文本
        
        Returns:
            扩展后的查询文本
        """
        expanded = query
        for key, value in self.synonym_dict.items():
            if key in expanded:
                expanded = expanded.replace(key, f"{key} {value}")
        return expanded
    
    def correct_chinese(self, text: str) -> str:
        """
        中文简单纠错组合
        
        Args:
            text: 输入文本
        
        Returns:
            纠错后的文本
        """
        text = self.fullwidth_to_halfwidth(text)
        text = self.remove_extra_spaces(text)
        text = self.clean_special_chars(text)
        return text
    
    def detect_intent(self, query: str) -> str:
        """
        简单意图识别
        
        Args:
            query: 查询文本
        
        Returns:
            意图类型: 'query' | 'complaint' | 'consultation'
        """
        complaint_keywords = ['投诉', '不满', '不合理', '太差', '慢', '问题']
        consultation_keywords = ['咨询', '请问', '想知道', '怎么', '如何', '多少']
        
        if any(k in query for k in complaint_keywords):
            return 'complaint'
        elif any(k in query for k in consultation_keywords):
            return 'consultation'
        else:
            return 'query'
    
    def process(self, query: str) -> Dict[str, Any]:
        """
        完整查询处理流水线
        
        Args:
            query: 原始查询文本
        
        Returns:
            处理结果字典: {
                'original': 原始查询,
                'cleaned': 清洗后的查询,
                'expanded': 同义词扩展后的查询,
                'intent': 识别出的意图
            }
        """
        result = {
            'original': query,
            'cleaned': self.correct_chinese(query),
            'expanded': '',
            'intent': ''
        }
        
        result['expanded'] = self.synonym_expand(result['cleaned'])
        result['intent'] = self.detect_intent(result['cleaned'])
        
        logger.info(f"查询重写完成: 原始='{query}', 清洗='{result['cleaned']}', 意图={result['intent']}")
        return result


class HybridRetriever:
    """
    混合检索器
    
    封装完整检索流程:
    1. 查询重写
    2. 向量编码
    3. 混合检索 (BM25 + 向量 + RRF)
    4. 规则重排
    5. 结果格式化
    """
    
    def __init__(self, es_client: ElasticsearchClient, vector_encoder: VectorEncoder):
        """
        初始化混合检索器
        
        Args:
            es_client: Elasticsearch客户端实例
            vector_encoder: 向量编码器实例
        """
        self.es_client = es_client
        self.vector_encoder = vector_encoder
        self.query_rewrite = QueryRewrite()
    
    def search(
        self,
        index_name: str,
        query: str,
        top_k: int = 10,
        enable_rerank: bool = True,
        category_boost: Optional[Dict[str, float]] = None,
        fallback_strategy: str = "bm25"  # 降级策略: "bm25" | "none" | "cache"
    ) -> List[Dict[str, Any]]:
        """
        完整检索流水线（支持多级降级）
        
        Args:
            index_name: 索引名称
            query: 查询文本
            top_k: 返回结果数量
            enable_rerank: 是否启用规则重排
            category_boost: 类目权重配置 {类目名: 权重倍数}
            fallback_strategy: 降级策略，"bm25"=降级到BM25, "none"=不降级, "cache"=使用缓存
        
        Returns:
            检索结果列表，按得分降序排列
            如果所有检索都失败，返回空列表并记录错误
        
        降级链:
        1. 正常: 查询重写 -> 向量编码 -> 混合检索(RRF) -> 重排
        2. 向量编码失败: 降级到纯BM25
        3. ES混合检索失败/无结果: 降级到纯BM25
        4. ES连接失败: 返回空列表（或从缓存读取）
        """
        logger.info(f"开始检索: query='{query}', top_k={top_k}, fallback={fallback_strategy}")
        
        # ========== 第1级: 查询重写 ==========
        try:
            query_info = self.query_rewrite.process(query)
            search_query = query_info['expanded']
            logger.debug(f"查询重写完成: {query} -> {search_query}")
        except Exception as e:
            logger.warning(f"查询重写失败: {e}, 使用原始查询")
            query_info = {'original': query, 'cleaned': query, 'expanded': query, 'intent': 'unknown'}
            search_query = query
        
        # ========== 第2级: 向量编码（可能降级） ==========
        query_vector = None
        use_vector = False
        
        try:
            query_vector = self.vector_encoder.encode(search_query)
            use_vector = True
            logger.debug("向量编码成功")
        except Exception as e:
            logger.warning(f"向量编码失败: {e}")
            if fallback_strategy == "none":
                logger.error("向量编码失败且禁用降级，返回空结果")
                return []
            # 继续执行，use_vector=False会触发BM25降级路径
        
        # 保存向量使用状态，供格式化时使用
        has_vector = use_vector
        
        # ========== 第3级: 混合检索（带多级降级） ==========
        results = []
        retrieve_size = max(top_k * 3, 50)
        
        try:
            if use_vector:
                # 正常路径: 混合检索(RRF)
                logger.info("执行混合检索(RRF)...")
                try:
                    results = self.es_client.hybrid_search(
                        index_name=index_name,
                        query=search_query,
                        query_vector=query_vector,
                        top_k=retrieve_size,
                        rrf_k=config.RRF_K
                    )
                    
                    # 混合检索无结果，降级到BM25
                    if not results:
                        logger.warning("混合检索无结果，降级到纯BM25")
                        results = self.es_client.bm25_search(index_name, search_query, size=top_k)
                        has_vector = False  # 降级到BM25，标记为无向量
                
                except Exception as hybrid_err:
                    # 混合检索抛出异常（ES连接失败、超时等），降级到纯BM25
                    logger.warning(f"混合检索异常({type(hybrid_err).__name__}: {hybrid_err})，降级到纯BM25")
                    results = self.es_client.bm25_search(index_name, search_query, size=top_k)
                    has_vector = False  # 降级到BM25，标记为无向量
            else:
                # 降级路径: 纯BM25（向量编码失败时会走到这里）
                logger.info("向量不可用，执行纯BM25检索...")
                results = self.es_client.bm25_search(index_name, search_query, size=top_k)
                has_vector = False  # 纯BM25，标记为无向量
        
        except Exception as e:
            # 外层兜底：BM25也失败时才到这里
            logger.error(f"所有检索路径均失败: {type(e).__name__}: {e}")
            if fallback_strategy == "cache":
                logger.info("尝试从缓存读取...")
                # TODO: 实现缓存读取逻辑
                results = []
            else:
                results = []
        
        # 如果所有检索都失败，返回空列表
        if not results:
            logger.warning("所有检索路径均未返回结果")
            return []
        
        # ========== 第4级: 结果去重和截断 ==========
        results = self.deduplicate(results)
        results = results[:top_k]
        
        # ========== 第5级: 排序修正（确保降级分支排序正确） ==========
        if not has_vector:
            # 纯BM25降级分支：按ES原生_score降序排列（修复之前漏排序的bug）
            results.sort(key=lambda x: x.get('_score', 0), reverse=True)
        # 正常有向量分支：RRF融合排序逻辑完全不动，保持原生检索质量
        
        # ========== 第6级: 结果格式化 ==========
        results = self.format_results(results, query_info, has_vector)
        
        logger.info(f"检索完成: 返回 {len(results)} 条结果")
        return results
    
    def rule_rerank(
        self,
        results: List[Dict[str, Any]],
        query: str,
        category_boost: Optional[Dict[str, float]] = None
    ) -> List[Dict[str, Any]]:
        """
        简单规则重排
        
        规则:
        1. 类目匹配加权
        2. 关键词命中加权
        3. 标题命中加权
        
        Args:
            results: 原始检索结果
            query: 查询文本
            category_boost: 类目权重配置
        
        Returns:
            重排后的结果列表
        """
        category_boost = category_boost or {}
        query_keywords = set(query.split())
        
        for result in results:
            source = result.get('_source', {})
            boost = 1.0
            
            # 1. 类目匹配加权
            category = source.get('category', '')
            if category in category_boost:
                boost *= category_boost[category]
            
            # 2. 标题/问题关键词命中加权
            question = source.get('question', '')
            answer = source.get('answer', '')
            
            for keyword in query_keywords:
                if keyword in question:
                    boost *= 1.2  # 问题中命中加权
                elif keyword in answer:
                    boost *= 1.1  # 答案中命中加权
            
            # 应用权重（排序用加权后的分数，但保存原始BM25分用于显示）
            result['_original_bm25_score'] = result.get('_score', 0)  # 保存原始ES返回的BM25分，用于前端显示
            result['_score'] = result.get('_score', 0) * boost  # 加权后的分数用于排序，保持检索质量
            result['_boost'] = boost
        
        # 按新得分重排
        results.sort(key=lambda x: x.get('_score', 0), reverse=True)
        logger.info(f"规则重排完成")
        return results
    
    @staticmethod
    def deduplicate(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        结果去重，按doc_id去重，保留得分最高的
        
        Args:
            results: 检索结果列表
        
        Returns:
            去重后的结果列表
        """
        seen_ids = set()
        unique_results = []
        
        for result in results:
            doc_id = result.get('_id')
            if doc_id not in seen_ids:
                seen_ids.add(doc_id)
                unique_results.append(result)
        
        if len(unique_results) != len(results):
            logger.info(f"结果去重: {len(results)} -> {len(unique_results)}")
        
        return unique_results
    
    @staticmethod
    def format_results(
        results: List[Dict[str, Any]],
        query_info: Optional[Dict[str, Any]] = None,
        has_vector: bool = True
    ) -> List[Dict[str, Any]]:
        """
        结果格式化，统一输出字段
        
        Args:
            results: 检索结果列表
            query_info: 查询重写信息
            has_vector: 是否使用了向量检索（用于前端显示逻辑）
        
        Returns:
            格式化后的结果列表
        """
        formatted = []
        
        for i, result in enumerate(results, 1):
            source = result.get('_source', {})
            formatted.append({
                'rank': i,
                'doc_id': result.get('_id'),
                'score': round(result.get('_score', 0), 4),  # RRF融合后的分数（排序用）
                'bm25_score': result.get('bm25_score', result.get('_score', 0)),  # ES原生BM25分，用于显示
                'vector_score': result.get('vector_score', 0),  # 向量模型原始分
                'has_vector': has_vector,  # 告诉前端是否使用了向量检索
                'question': source.get('question', ''),
                'answer': source.get('answer', ''),
                'category': source.get('category', ''),
                'create_time': source.get('create_time', ''),
                '_rrf_score': result.get('_rrf_score', 0),
                '_query_info': query_info
            })
        
        return formatted
