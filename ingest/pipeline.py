#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据处理流水线模块（ingest版本）
整合文件解析、分块、向量化、SQLite存储的完整流程

核心功能：
1. 扫描目录或处理单个文件
2. 调用解析器提取文本
3. 调用分块器进行智能分块
4. 调用嵌入器生成向量
5. 调用存储管理器保存到SQLite
6. 进度跟踪和错误处理

设计原则：
- 模块化：每个步骤独立，可单独测试
- 可恢复：记录处理状态，支持断点续传
- 错误容忍：单个文件失败不影响整体流程
- 性能优化：批量处理，减少IO操作
- 幂等设计：重复导入同一文件不产生重复数据
"""

import os
import sys
import time
import json
import uuid
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, asdict
from datetime import datetime

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

# 导入本模块的组件
from .storage import StorageManager
from .embedder import EmbedderConfig, create_embedder, BaseEmbedder
from .parsers import parse_file, get_supported_extensions, is_supported_file
from .chunker import ChunkConfig, chunk_text, estimate_tokens

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
    skipped_files: int = 0
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
    file_path: str
    status: str  # 'success', 'failed', 'skipped', 'duplicate'
    error: Optional[str] = None
    document_id: Optional[int] = None
    chunks_count: int = 0
    successful_chunks: int = 0
    failed_chunks: int = 0
    processing_time: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return asdict(self)


class IngestionPipeline:
    """数据导入流水线"""
    
    def __init__(
        self,
        storage_manager: Optional[StorageManager] = None,
        embedder: Optional[BaseEmbedder] = None,
        chunk_config: Optional[ChunkConfig] = None,
        embedder_config: Optional[EmbedderConfig] = None,
        db_path: Optional[str] = None
    ):
        """
        初始化流水线
        
        Args:
            storage_manager: 存储管理器实例，None则创建新实例
            embedder: 嵌入器实例，None则创建新实例
            chunk_config: 分块配置，None使用默认配置
            embedder_config: 嵌入器配置，None使用默认配置
            db_path: SQLite数据库路径，None使用默认路径
        """
        # 初始化存储管理器
        if storage_manager:
            self.storage = storage_manager
        else:
            logger.info("创建新的存储管理器...")
            self.storage = StorageManager(db_path)
        
        # 初始化嵌入器
        if embedder:
            self.embedder = embedder
        else:
            logger.info("创建新的嵌入器...")
            embedder_config = embedder_config or EmbedderConfig()
            self.embedder = create_embedder(embedder_config)
        
        # 初始化分块配置
        self.chunk_config = chunk_config or ChunkConfig()
        
        # 处理统计
        self.stats = ProcessingStats()
        self.file_results: List[FileResult] = []
        
        # 任务ID（用于跟踪）
        self.task_id = str(uuid.uuid4())[:8]
        
        logger.info(f"✅ 流水线初始化完成，任务ID: {self.task_id}")
        logger.info(f"分块配置: {self.chunk_config}")
        logger.info(f"嵌入器: {self.embedder.__class__.__name__}")
    
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
        supported_extensions = get_supported_extensions()
        files = []
        
        for ext in supported_extensions:
            files.extend(directory.glob(f"*{ext}"))
            files.extend(directory.glob(f"**/*{ext}"))
        
        # 去重和排序
        files = list(set(files))
        files.sort()
        
        self.stats.start()
        self.stats.total_files = len(files)
        
        logger.info(f"开始处理目录: {directory_path}")
        logger.info(f"找到 {len(files)} 个文件")
        logger.info(f"支持的文件格式: {supported_extensions}")
        
        # 创建导入任务记录
        _ = self.storage.create_import_task(
            task_id=self.task_id,
            task_type='dir',
            source_path=directory_path,
            total_files=len(files)
        )
        
        for i, file_path in enumerate(files, 1):
            logger.info(f"处理文件 {i}/{len(files)}: {file_path.name}")
            
            # 更新任务进度
            self.storage.update_task_progress(
                task_id=self.task_id,
                processed_files=i-1,
                status='processing'
            )
            
            # 处理单个文件
            result = self._process_single_file(str(file_path))
            self.file_results.append(result)
            
            # 更新统计
            if result.status == 'success':
                self.stats.processed_files += 1
                self.stats.total_chunks += result.chunks_count
                self.stats.successful_chunks += result.successful_chunks
                self.stats.failed_chunks += result.failed_chunks
            elif result.status == 'failed':
                self.stats.failed_files += 1
            elif result.status == 'skipped':
                self.stats.skipped_files += 1
            elif result.status == 'duplicate':
                self.stats.skipped_files += 1
        
        # 更新任务为完成状态
        final_status = 'completed' if self.stats.failed_files == 0 else 'partial'
        self.storage.update_task_progress(
            task_id=self.task_id,
            processed_files=self.stats.total_files,
            status=final_status
        )
        
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
        
        # 创建导入任务记录
        _ = self.storage.create_import_task(
            task_id=self.task_id,
            task_type='file',
            source_path=file_path,
            total_files=1
        )
        
        # 处理文件
        result = self._process_single_file(file_path)
        
        # 更新任务进度
        final_status = 'completed' if result.status == 'success' else 'failed'
        self.storage.update_task_progress(
            task_id=self.task_id,
            processed_files=1,
            status=final_status
        )
        
        self.stats.end()
        
        # 更新统计
        if result.status == 'success':
            self.stats.processed_files = 1
            self.stats.total_chunks = result.chunks_count
            self.stats.successful_chunks = result.successful_chunks
            self.stats.failed_chunks = result.failed_chunks
        elif result.status == 'failed':
            self.stats.failed_files = 1
        elif result.status in ['skipped', 'duplicate']:
            self.stats.skipped_files = 1
        
        self._print_summary()
        
        return result
    
    def _process_single_file(self, file_path: str) -> FileResult:
        """
        处理单个文件（内部方法）
        
        Args:
            file_path: 文件路径
            
        Returns:
            文件处理结果
        """
        start_time = time.time()
        result = FileResult(file_path=file_path, status='failed')
        
        try:
            file_path_obj = Path(file_path)
            
            # 1. 检查文件是否支持
            if not is_supported_file(file_path):
                result.status = 'skipped'
                result.error = f"不支持的文件格式: {file_path_obj.suffix}"
                logger.warning(f"跳过文件 {file_path_obj.name}: {result.error}")
                return result
            
            # 2. 计算文件哈希，检查是否已存在
            file_hash = self.storage.calculate_file_hash(file_path)
            if self.storage.file_exists(file_hash):
                result.status = 'duplicate'
                result.error = "文件已存在（基于哈希去重）"
                logger.info(f"跳过重复文件: {file_path_obj.name}")
                return result
            
            # 3. 解析文件
            logger.debug(f"解析文件: {file_path_obj.name}")
            content = parse_file(file_path)
            
            if not content:
                result.status = 'skipped'
                result.error = "文件解析失败或无内容"
                logger.warning(f"跳过文件 {file_path_obj.name}: {result.error}")
                return result
            
            logger.debug(f"文件解析成功，字符数: {len(content)}")
            
            # 4. 创建文档记录
            document = self.storage.create_document(
                file_path=file_path,
                file_hash=file_hash,
                file_type=file_path_obj.suffix[1:].lower(),  # 去掉点号
                title=file_path_obj.stem
            )
            
            result.document_id = document.id
            
            # 5. 分块
            logger.debug("开始分块...")
            chunks = chunk_text(content, 
                              chunk_size=self.chunk_config.chunk_size,
                              overlap_size=self.chunk_config.overlap_size)
            
            if not chunks:
                result.status = 'failed'
                result.error = "分块失败或无有效块"
                logger.error(f"文件处理失败 {file_path_obj.name}: {result.error}")
                
                # 更新文档状态
                self.storage.update_document_status(
                    document_id=document.id,
                    status='failed',
                    error_message=result.error
                )
                return result
            
            result.chunks_count = len(chunks)
            logger.debug(f"分块完成，共 {len(chunks)} 个块")
            
            # 6. 生成向量（批量）
            logger.debug("开始生成向量...")
            embeddings = self.embedder.encode_batch(chunks)
            
            if len(embeddings) != len(chunks):
                logger.warning(f"向量生成数量不匹配: 期望{len(chunks)}，实际{len(embeddings)}")
                # 继续处理，只处理成功生成向量的块
            
            # 7. 保存分块和向量到数据库
            logger.debug("保存分块到数据库...")
            success_count = 0
            fail_count = 0
            
            for i, (chunk_text_content, embedding) in enumerate(zip(chunks, embeddings)):
                try:
                    # 估算token数
                    chunk_tokens = estimate_tokens(chunk_text_content)
                    
                    # 创建分块记录
                    chunk = self.storage.create_chunk(
                        document_id=document.id,
                        chunk_index=i,
                        chunk_text=chunk_text_content,
                        chunk_tokens=chunk_tokens,
                        embedding=embedding
                    )
                    
                    if chunk:
                        success_count += 1
                    else:
                        fail_count += 1
                        
                    # 每处理10个块记录一次进度
                    if (i + 1) % 10 == 0:
                        logger.debug(f"分块保存进度: {i + 1}/{len(chunks)}")
                        
                except Exception as chunk_error:
                    logger.warning(f"分块 {i} 保存失败: {chunk_error}")
                    fail_count += 1
            
            result.successful_chunks = success_count
            result.failed_chunks = fail_count
            
            # 8. 更新文档状态
            if success_count > 0:
                result.status = 'success'
                self.storage.update_document_status(
                    document_id=document.id,
                    status='completed',
                    total_chunks=success_count
                )
                logger.info(f"✅ 文件处理成功: {file_path_obj.name}，成功保存 {success_count}/{len(chunks)} 个块")
            else:
                result.status = 'failed'
                result.error = "所有分块保存失败"
                self.storage.update_document_status(
                    document_id=document.id,
                    status='failed',
                    error_message=result.error
                )
                logger.error(f"文件处理失败: {file_path_obj.name}，{result.error}")
            
        except Exception as e:
            result.status = 'failed'
            result.error = str(e)
            logger.error(f"文件处理异常 {file_path}: {e}")
            
            # 如果有文档ID，更新状态
            if result.document_id:
                self.storage.update_document_status(
                    document_id=result.document_id,
                    status='failed',
                    error_message=result.error
                )
        
        result.processing_time = time.time() - start_time
        return result
    
    def _print_summary(self) -> None:
        """打印处理摘要"""
        logger.info("=" * 60)
        logger.info("处理完成摘要")
        logger.info("=" * 60)
        logger.info(f"总文件数: {self.stats.total_files}")
        logger.info(f"成功处理: {self.stats.processed_files}")
        logger.info(f"处理失败: {self.stats.failed_files}")
        logger.info(f"跳过文件: {self.stats.skipped_files}")
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
                logger.warning(f"  - {Path(result.file_path).name}: {result.error}")
        
        # 打印跳过文件详情
        skipped_files = [r for r in self.file_results if r.status in ['skipped', 'duplicate']]
        if skipped_files:
            logger.info("跳过文件列表:")
            for result in skipped_files:
                logger.info(f"  - {Path(result.file_path).name}: {result.error}")
    
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
            output_path = f"ingest_results_{timestamp}.json"
        
        results = {
            'task_id': self.task_id,
            'stats': self.stats.to_dict(),
            'file_results': [r.to_dict() for r in self.file_results],
            'timestamp': datetime.now().isoformat(),
            'config': {
                'chunk_size': self.chunk_config.chunk_size,
                'overlap_size': self.chunk_config.overlap_size,
                'embedder': self.embedder.__class__.__name__
            }
        }
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        
        logger.info(f"结果已保存到: {output_path}")
        return output_path
    
    def get_stats(self) -> Dict[str, Any]:
        """获取数据库统计信息"""
        return self.storage.get_stats()
    
    def close(self):
        """关闭流水线，释放资源"""
        self.storage.close()
        logger.info("流水线已关闭")


# ========== 全局函数接口 ==========

def process_directory(
    directory_path: str,
    db_path: Optional[str] = None,
    chunk_size: int = 512,
    overlap_size: int = 128
) -> ProcessingStats:
    """
    处理目录（全局函数接口）
    
    Args:
        directory_path: 目录路径
        db_path: 数据库路径
        chunk_size: 分块大小
        overlap_size: 重叠大小
        
    Returns:
        处理统计信息
    """
    chunk_config = ChunkConfig(chunk_size=chunk_size, overlap_size=overlap_size)
    pipeline = IngestionPipeline(chunk_config=chunk_config, db_path=db_path)
    
    try:
        return pipeline.process_directory(directory_path)
    finally:
        pipeline.close()


def process_single_file(
    file_path: str,
    db_path: Optional[str] = None,
    chunk_size: int = 512,
    overlap_size: int = 128
) -> FileResult:
    """
    处理单个文件（全局函数接口）
    
    Args:
        file_path: 文件路径
        db_path: 数据库路径
        chunk_size: 分块大小
        overlap_size: 重叠大小
        
    Returns:
        文件处理结果
    """
    chunk_config = ChunkConfig(chunk_size=chunk_size, overlap_size=overlap_size)
    pipeline = IngestionPipeline(chunk_config=chunk_config, db_path=db_path)
    
    try:
        return pipeline.process_single_file(file_path)
    finally:
        pipeline.close()


if __name__ == "__main__":
    # 测试代码
    import tempfile
    import shutil
    
    print("测试ingest流水线模块...")
    
    # 创建测试目录和文件
    test_dir = tempfile.mkdtemp(prefix="ingest_test_")
    print(f"创建测试目录: {test_dir}")
    
    try:
        # 创建测试文件
        test_file = Path(test_dir) / "test.txt"
        with open(test_file, 'w', encoding='utf-8') as f:
            f.write("这是一个测试文档。\n" * 100)
        
        print(f"创建测试文件: {test_file}")
        
        # 创建临时数据库
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
            db_path = tmp.name
        
        print(f"使用临时数据库: {db_path}")
        
        # 测试处理单个文件
        print("\n测试处理单个文件:")
        result = process_single_file(str(test_file), db_path=db_path, chunk_size=100, overlap_size=20)
        print(f"处理结果: {result.status}")
        print(f"文档ID: {result.document_id}")
        print(f"块数: {result.chunks_count}")
        print(f"成功块数: {result.successful_chunks}")
        print(f"处理时间: {result.processing_time:.2f}秒")
        
        if result.error:
            print(f"错误: {result.error}")
        
        # 测试处理目录
        print("\n测试处理目录:")
        stats = process_directory(test_dir, db_path=db_path, chunk_size=100, overlap_size=20)
        print(f"总文件数: {stats.total_files}")
        print(f"成功处理: {stats.processed_files}")
        print(f"失败文件: {stats.failed_files}")
        print(f"总块数: {stats.total_chunks}")
        print(f"总耗时: {stats.elapsed_time:.2f}秒")
        
    finally:
        # 清理
        if os.path.exists(test_dir):
            shutil.rmtree(test_dir)
            print(f"清理测试目录: {test_dir}")
        
        if os.path.exists(db_path):
            os.unlink(db_path)
            print(f"清理临时数据库: {db_path}")