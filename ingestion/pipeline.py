#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据处理流水线模块
整合文件解析、分块、向量化、ES写入的完整流程

核心功能：
1. 扫描目录或处理单个文件
2. 调用解析器提取文本
3. 调用分块器进行智能分块
4. 复用现有embedding客户端生成向量
5. 复用现有ES客户端写入索引
6. 进度跟踪和错误处理

设计原则：
- 模块化：每个步骤独立，可单独测试
- 可恢复：记录处理状态，支持断点续传
- 错误容忍：单个文件失败不影响整体流程
- 性能优化：批量处理，减少IO操作
"""

import sys
import time
import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, asdict
from datetime import datetime

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

# 导入项目配置和客户端
import config
from core.embedding import EmbeddingClient
from core.es_client import ElasticsearchClient

# 导入本模块的解析器和分块器
from .parsers import ParserFactory, parse_file
from .chunker import SmartChunker, ChunkConfig

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class ProcessingStats:
    """处理统计信息"""
    total_files: int = 0
    processed_files: int = 0
    failed_files: int = 0
    total_chunks: int = 0
    successful_chunks: int = 0
    failed_chunks: int = 0
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    
    @property
    def elapsed_time(self) -> float:
        """计算耗时"""
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        elif self.start_time:
            return time.time() - self.start_time
        return 0.0
    
    @property
    def success_rate(self) -> float:
        """计算成功率"""
        if self.total_files == 0:
            return 0.0
        return self.processed_files / self.total_files * 100
    
    def start(self):
        """开始计时"""
        self.start_time = time.time()
    
    def end(self):
        """结束计时"""
        self.end_time = time.time()
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        result = asdict(self)
        result['elapsed_time'] = self.elapsed_time
        result['success_rate'] = self.success_rate
        return result


@dataclass
class FileResult:
    """单个文件处理结果"""
    file_path: Path
    status: str  # 'success', 'failed', 'skipped'
    error: Optional[str] = None
    chunks_count: int = 0
    successful_chunks: int = 0
    failed_chunks: int = 0
    processing_time: float = 0.0


class IngestionPipeline:
    """数据导入流水线"""
    
    def __init__(
        self,
        es_client: Optional[ElasticsearchClient] = None,
        embedding_client: Optional[EmbeddingClient] = None,
        chunk_config: Optional[ChunkConfig] = None,
        index_name: Optional[str] = None
    ):
        """
        初始化流水线
        
        Args:
            es_client: ES客户端实例，None则创建新实例
            embedding_client: Embedding客户端实例，None则创建新实例
            chunk_config: 分块配置，None使用默认配置
            index_name: ES索引名称，None使用config中的默认值
        """
        # 初始化ES客户端
        if es_client:
            self.es_client = es_client
        else:
            logger.info("创建新的ES客户端...")
            self.es_client = ElasticsearchClient()
        
        # 初始化Embedding客户端
        if embedding_client:
            self.embedding_client = embedding_client
        else:
            logger.info("创建新的Embedding客户端...")
            self.embedding_client = EmbeddingClient()
        
        # 初始化分块器
        self.chunk_config = chunk_config or ChunkConfig()
        self.chunker = SmartChunker(self.chunk_config)
        
        # 索引名称
        self.index_name = index_name or config.ES_INDEX_NAME
        
        # 处理统计
        self.stats = ProcessingStats()
        self.file_results: List[FileResult] = []
        
        # 确保索引存在
        self._ensure_index()
    
    def _ensure_index(self) -> None:
        """确保ES索引存在"""
        try:
            if not self.es_client.index_exists(self.index_name):
                logger.info(f"创建索引: {self.index_name}")
                # 使用config中的默认mapping
                self.es_client.create_index(
                    self.index_name,
                    mappings=config.VECTOR_INDEX_MAPPING.get('mappings')
                )
            else:
                logger.info(f"索引已存在: {self.index_name}")
        except Exception as e:
            logger.error(f"确保索引失败: {e}")
            raise
    
    def process_directory(self, directory_path: str) -> ProcessingStats:
        """
        处理目录中的所有文件
        
        Args:
            directory_path: 目录路径
            
        Returns:
            处理统计信息
        """
        directory = Path(directory_path)
        if not directory.exists():
            raise FileNotFoundError(f"目录不存在: {directory_path}")
        if not directory.is_dir():
            raise ValueError(f"不是目录: {directory_path}")
        
        # 获取所有支持的文件
        supported_extensions = ParserFactory.get_parsers()
        all_extensions = []
        for parser in supported_extensions:
            all_extensions.extend(parser.supported_extensions)
        
        files = []
        for ext in all_extensions:
            files.extend(directory.glob(f"*{ext}"))
            files.extend(directory.glob(f"**/*{ext}"))
        
        # 去重和排序
        files = list(set(files))
        files.sort()
        
        self.stats.start()
        self.stats.total_files = len(files)
        
        logger.info(f"开始处理目录: {directory_path}")
        logger.info(f"找到 {len(files)} 个文件")
        
        for i, file_path in enumerate(files, 1):
            logger.info(f"处理文件 {i}/{len(files)}: {file_path.name}")
            result = self._process_single_file(file_path)
            self.file_results.append(result)
            
            # 更新统计
            if result.status == 'success':
                self.stats.processed_files += 1
                self.stats.total_chunks += result.chunks_count
                self.stats.successful_chunks += result.successful_chunks
                self.stats.failed_chunks += result.failed_chunks
            elif result.status == 'failed':
                self.stats.failed_files += 1
            # skipped不计入失败
        
        self.stats.end()
        self._print_summary()
        
        return self.stats
    
    def process_single_file(self, file_path: str) -> FileResult:
        """
        处理单个文件
        
        Args:
            file_path: 文件路径
            
        Returns:
            文件处理结果
        """
        file_path_obj = Path(file_path)
        if not file_path_obj.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")
        
        self.stats.start()
        self.stats.total_files = 1
        
        logger.info(f"开始处理文件: {file_path}")
        result = self._process_single_file(file_path_obj)
        
        self.stats.end()
        
        # 更新统计
        if result.status == 'success':
            self.stats.processed_files = 1
            self.stats.total_chunks = result.chunks_count
            self.stats.successful_chunks = result.successful_chunks
            self.stats.failed_chunks = result.failed_chunks
        elif result.status == 'failed':
            self.stats.failed_files = 1
        
        self._print_summary()
        
        return result
    
    def _process_single_file(self, file_path: Path) -> FileResult:
        """
        处理单个文件（内部方法）
        
        Args:
            file_path: 文件路径对象
            
        Returns:
            文件处理结果
        """
        start_time = time.time()
        result = FileResult(file_path=file_path, status='failed')
        
        try:
            # 1. 解析文件
            logger.debug(f"解析文件: {file_path.name}")
            content = parse_file(str(file_path))
            
            if not content:
                result.status = 'skipped'
                result.error = "文件解析失败或无内容"
                logger.warning(f"跳过文件 {file_path.name}: {result.error}")
                return result
            
            logger.debug(f"文件解析成功，字符数: {len(content)}")
            
            # 2. 分块
            logger.debug("开始分块...")
            chunks = self.chunker.chunk_text(content)
            
            if not chunks:
                result.status = 'skipped'
                result.error = "分块失败或无有效块"
                logger.warning(f"跳过文件 {file_path.name}: {result.error}")
                return result
            
            result.chunks_count = len(chunks)
            logger.debug(f"分块完成，共 {len(chunks)} 个块")
            
            # 3. 生成向量（批量）
            logger.debug("开始生成向量...")
            embeddings = self._generate_embeddings(chunks)
            
            if len(embeddings) != len(chunks):
                logger.warning(f"向量生成数量不匹配: 期望{len(chunks)}，实际{len(embeddings)}")
                # 继续处理，只处理成功生成向量的块
            
            # 4. 准备ES文档
            logger.debug("准备ES文档...")
            documents = self._prepare_documents(file_path, chunks, embeddings)
            
            # 5. 写入ES
            logger.debug("写入ES索引...")
            success_count = self._write_to_es(documents)
            
            result.successful_chunks = success_count
            result.failed_chunks = len(chunks) - success_count
            
            if success_count > 0:
                result.status = 'success'
                logger.info(f"文件处理成功: {file_path.name}，成功写入 {success_count}/{len(chunks)} 个块")
            else:
                result.status = 'failed'
                result.error = "所有块写入ES失败"
                logger.error(f"文件处理失败: {file_path.name}，{result.error}")
            
        except Exception as e:
            result.status = 'failed'
            result.error = str(e)
            logger.error(f"文件处理异常 {file_path.name}: {e}")
        
        result.processing_time = time.time() - start_time
        return result
    
    def _generate_embeddings(self, chunks: List[str]) -> List[List[float]]:
        """
        批量生成向量
        
        Args:
            chunks: 文本块列表
            
        Returns:
            向量列表
        """
        if not chunks:
            return []
        
        try:
            # 使用embedding客户端的批量编码功能
            embeddings = self.embedding_client.encode_batch(chunks)
            logger.debug(f"向量生成完成: {len(embeddings)} 个向量")
            return embeddings
        except Exception as e:
            logger.error(f"向量生成失败: {e}")
            # 尝试逐个生成
            embeddings = []
            for i, chunk in enumerate(chunks):
                try:
                    embedding = self.embedding_client.encode(chunk)
                    embeddings.append(embedding)
                    if (i + 1) % 10 == 0:
                        logger.debug(f"向量生成进度: {i + 1}/{len(chunks)}")
                except Exception as chunk_error:
                    logger.warning(f"块 {i} 向量生成失败: {chunk_error}")
                    # 添加空向量占位
                    embeddings.append([0.0] * self.embedding_client.get_dimension())
            
            return embeddings
    
    def _prepare_documents(
        self, 
        file_path: Path, 
        chunks: List[str], 
        embeddings: List[List[float]]
    ) -> List[Dict[str, Any]]:
        """
        准备ES文档
        
        Args:
            file_path: 源文件路径
            chunks: 文本块列表
            embeddings: 向量列表
            
        Returns:
            ES文档列表
        """
        documents = []
        
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            # 确保向量维度正确
            if len(embedding) != self.embedding_client.get_dimension():
                logger.warning(f"块 {i} 向量维度不匹配: 期望{self.embedding_client.get_dimension()}，实际{len(embedding)}")
                continue
            
            doc = {
                'doc_id': f"{file_path.stem}_{i:04d}",
                'question': chunk[:200],  # 前200字符作为question
                'answer': chunk,          # 完整内容作为answer
                'embedding': embedding,
                'category': file_path.suffix[1:].upper(),  # 文件扩展名作为类别
                'source_file': str(file_path.name),
                'chunk_index': i,
                'total_chunks': len(chunks),
                'create_time': datetime.now().isoformat()
            }
            documents.append(doc)
        
        return documents
    
    def _write_to_es(self, documents: List[Dict[str, Any]]) -> int:
        """
        批量写入ES
        
        Args:
            documents: 文档列表
            
        Returns:
            成功写入的文档数量
        """
        if not documents:
            return 0
        
        try:
            # 使用ES客户端的批量插入功能
            success = self.es_client.bulk_insert(self.index_name, documents)
            if success:
                logger.debug(f"批量插入成功: {len(documents)} 个文档")
                return len(documents)
            else:
                logger.error("批量插入失败")
                return 0
        except Exception as e:
            logger.error(f"ES写入失败: {e}")
            
            # 尝试逐个写入
            success_count = 0
            for i, doc in enumerate(documents):
                try:
                    result = self.es_client.insert_document(self.index_name, doc, doc_id=doc['doc_id'])
                    if result:
                        success_count += 1
                    if (i + 1) % 10 == 0:
                        logger.debug(f"逐个写入进度: {i + 1}/{len(documents)}")
                except Exception as doc_error:
                    logger.warning(f"文档 {i} 写入失败: {doc_error}")
            
            return success_count
    
    def _print_summary(self) -> None:
        """打印处理摘要"""
        logger.info("=" * 60)
        logger.info("处理完成摘要")
        logger.info("=" * 60)
        logger.info(f"总文件数: {self.stats.total_files}")
        logger.info(f"成功处理: {self.stats.processed_files}")
        logger.info(f"处理失败: {self.stats.failed_files}")
        logger.info(f"总块数: {self.stats.total_chunks}")
        logger.info(f"成功块数: {self.stats.successful_chunks}")
        logger.info(f"失败块数: {self.stats.failed_chunks}")
        logger.info(f"成功率: {self.stats.success_rate:.1f}%")
        logger.info(f"总耗时: {self.stats.elapsed_time:.2f}秒")
        logger.info("=" * 60)
        
        # 打印失败文件详情
        failed_files = [r for r in self.file_results if r.status == 'failed']
        if failed_files:
            logger.warning("失败文件列表:")
            for result in failed_files:
                logger.warning(f"  - {result.file_path.name}: {result.error}")
    
    def save_results(self, output_path: Optional[str] = None) -> str:
        """
        保存处理结果到JSON文件
        
        Args:
            output_path: 输出文件路径，None则自动生成
            
        Returns:
            保存的文件路径
        """
        if output_path is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = f"ingestion_results_{timestamp}.json"
        
        results = {
            'stats': self.stats.to_dict(),
            'file_results': [
                {
                    'file_path': str(r.file_path),
                    'status': r.status,
                    'error': r.error,
                    'chunks_count': r.chunks_count,
                    'successful_chunks': r.successful_chunks,
                    'failed_chunks': r.failed_chunks,
                    'processing_time': r.processing_time
                }
                for r in self.file_results
            ],
            'timestamp': datetime.now().isoformat(),
            'config': {
                'index_name': self.index_name,
                'chunk_size': self.chunk_config.chunk_size,
                'overlap_size': self.chunk_config.overlap_size
            }
        }
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        
        logger.info(f"结果已保存到: {output_path}")
        return output_path


# 全局函数接口
def process_directory(
    directory_path: str,
    index_name: Optional[str] = None,
    chunk_size: int = 512,
    overlap_size: int = 50
) -> ProcessingStats:
    """
    处理目录（全局函数接口）
    
    Args:
        directory_path: 目录路径
        index_name: ES索引名称
        chunk_size: 分块大小
        overlap_size: 重叠大小
        
    Returns:
        处理统计信息
    """
    chunk_config = ChunkConfig(chunk_size=chunk_size, overlap_size=overlap_size)
    pipeline = IngestionPipeline(chunk_config=chunk_config, index_name=index_name)
    return pipeline.process_directory(directory_path)


def process_single_file(
    file_path: str,
    index_name: Optional[str] = None,
    chunk_size: int = 512,
    overlap_size: int = 50
) -> FileResult:
    """
    处理单个文件（全局函数接口）
    
    Args:
        file_path: 文件路径
        index_name: ES索引名称
        chunk_size: 分块大小
        overlap_size: 重叠大小
        
    Returns:
        文件处理结果
    """
    chunk_config = ChunkConfig(chunk_size=chunk_size, overlap_size=overlap_size)
    pipeline = IngestionPipeline(chunk_config=chunk_config, index_name=index_name)
    return pipeline.process_single_file(file_path)


if __name__ == "__main__":
    # 测试代码
    import sys
    
    if len(sys.argv) > 1:
        if sys.argv[1] == '--dir' and len(sys.argv) > 2:
            stats = process_directory(sys.argv[2])
            print(f"处理完成: {stats.processed_files}/{stats.total_files} 个文件成功")
        elif sys.argv[1] == '--file' and len(sys.argv) > 2:
            result = process_single_file(sys.argv[2])
            print(f"文件处理结果: {result.status}")
            if result.error:
                print(f"错误: {result.error}")
        else:
            print("用法:")
            print("  python pipeline.py --dir <目录路径>")
            print("  python pipeline.py --file <文件路径>")
    else:
        print("请提供参数，用法同上")