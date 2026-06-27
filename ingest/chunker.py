#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文本分块器模块（ingest版本）
复用ingestion.chunker的核心功能，适配SQLite存储

核心功能：
1. 按token数量分块，默认512token
2. 支持重叠，默认重叠128token
3. 智能边界处理：尽量在句子/段落边界分割
4. 支持中英文混合文本
5. 保留上下文连贯性

设计原则：
- 复用现有代码，减少重复开发
- 性能优化：避免重复计算token
- 可配置性：支持自定义块大小和重叠大小
- 错误处理：无效输入友好提示
"""

import sys
import logging
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass

# 添加项目根目录到Python路径
sys.path.insert(0, str(Path(__file__).parent.parent))

# 尝试导入现有的分块器
try:
    from ingestion.chunker import (
        SmartChunker as IngestionSmartChunker,
        ChunkConfig as IngestionChunkConfig
    )
    HAS_INGESTION_CHUNKER = True
except ImportError:
    HAS_INGESTION_CHUNKER = False
    logger = logging.getLogger(__name__)
    logger.warning("ingestion.chunker模块不可用，将使用简化版分块器")

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class ChunkConfig:
    """分块配置"""
    chunk_size: int = 512        # 每个块的token数量
    overlap_size: int = 128      # 重叠的token数量
    min_chunk_size: int = 50     # 最小块大小（避免过小片段）
    max_chunk_size: int = 1024   # 最大块大小（安全限制）
    
    def validate(self) -> None:
        """验证配置参数"""
        if self.chunk_size <= 0:
            raise ValueError("chunk_size必须大于0")
        if self.overlap_size < 0:
            raise ValueError("overlap_size不能为负数")
        if self.overlap_size >= self.chunk_size:
            raise ValueError("overlap_size必须小于chunk_size")
        if self.min_chunk_size <= 0:
            raise ValueError("min_chunk_size必须大于0")
        if self.max_chunk_size < self.chunk_size:
            raise ValueError("max_chunk_size不能小于chunk_size")
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'chunk_size': self.chunk_size,
            'overlap_size': self.overlap_size,
            'min_chunk_size': self.min_chunk_size,
            'max_chunk_size': self.max_chunk_size
        }


class SimpleTokenEstimator:
    """简化的token估算器"""
    
    @staticmethod
    def estimate_tokens(text: str) -> int:
        """
        估算文本的token数量
        
        简化估算规则：
        - 中文字符：1字符 ≈ 2 tokens
        - 英文字符：1字符 ≈ 0.25 tokens
        - 数字/标点：1字符 ≈ 0.5 tokens
        
        Args:
            text: 输入文本
            
        Returns:
            估算的token数量
        """
        if not text:
            return 0
        
        chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        english_chars = sum(1 for c in text if c.isalpha() and not ('\u4e00' <= c <= '\u9fff'))
        other_chars = len(text) - chinese_chars - english_chars
        
        # 估算token
        estimated_tokens = chinese_chars * 2 + english_chars * 0.25 + other_chars * 0.5
        
        # 向上取整，确保安全边界
        return int(estimated_tokens) + 1


class SimpleChunker:
    """简化的分块器"""
    
    def __init__(self, config: Optional[ChunkConfig] = None):
        """
        初始化分块器
        
        Args:
            config: 分块配置，None使用默认配置
        """
        self.config = config or ChunkConfig()
        self.config.validate()
        self.token_estimator = SimpleTokenEstimator()
    
    def chunk_text(self, text: str) -> List[str]:
        """
        将长文本分割为多个块
        
        Args:
            text: 输入文本
            
        Returns:
            文本块列表
            
        Raises:
            ValueError: 输入文本为空
        """
        if not text or not text.strip():
            raise ValueError("输入文本不能为空")
        
        # 清理文本
        text = text.strip()
        
        chunks = []
        remaining_text = text
        
        logger.info(f"开始分块，文本长度: {len(text)}字符")
        
        while remaining_text:
            # 估算剩余文本的token数
            remaining_tokens = self.token_estimator.estimate_tokens(remaining_text)
            
            # 如果剩余文本可以直接作为一个块
            if remaining_tokens <= self.config.chunk_size:
                chunks.append(remaining_text.strip())
                break
            
            # 需要分割：找到合适的分割位置
            split_pos = self._find_split_position(remaining_text)
            
            if split_pos <= 0 or split_pos >= len(remaining_text):
                # 无法找到合适的分割位置，强制在chunk_size估算位置分割
                estimated_chars = int(
                    len(remaining_text) * self.config.chunk_size / remaining_tokens
                )
                split_pos = min(estimated_chars, len(remaining_text) - 1)
            
            # 提取当前块
            current_chunk = remaining_text[:split_pos].strip()
            if current_chunk:
                chunks.append(current_chunk)
            
            # 计算重叠部分
            overlap_start = self._calculate_overlap_start(current_chunk)
            remaining_text = remaining_text[overlap_start:]
            
            logger.debug(f"分块进度: 已生成{len(chunks)}个块，剩余字符: {len(remaining_text)}")
        
        # 后处理：确保块大小在合理范围内
        chunks = self._postprocess_chunks(chunks)
        
        logger.info(f"分块完成: 共{len(chunks)}个块")
        return chunks
    
    def _find_split_position(self, text: str) -> int:
        """
        在文本中找到最佳分割位置
        
        策略：
        1. 先找段落边界（\n\n）
        2. 再找句子边界（。！？.?!）
        3. 再找逗号边界（，,）
        4. 最后在空格处分割
        
        Args:
            text: 待分割文本
            
        Returns:
            最佳分割位置（字符索引），-1表示不需要分割
        """
        # 计算目标字符数（基于配置的chunk_size）
        target_chars = int(len(text) * 0.7)  # 初始目标为文本长度的70%
        
        # 策略1：在段落边界分割
        split_pos = text.rfind('\n\n', 0, target_chars + 100)
        if split_pos > 0:
            return split_pos + 2  # 包含换行符
        
        # 策略2：在句子边界分割
        sentence_boundaries = ['。', '！', '？', '.', '!', '?', ';', '；']
        for boundary in sentence_boundaries:
            split_pos = text.rfind(boundary, 0, target_chars + 100)
            if split_pos > 0:
                return split_pos + len(boundary)
        
        # 策略3：在逗号边界分割
        comma_boundaries = ['，', ',', '、']
        for boundary in comma_boundaries:
            split_pos = text.rfind(boundary, 0, target_chars + 100)
            if split_pos > 0:
                return split_pos + len(boundary)
        
        # 策略4：在空格处分割
        split_pos = text.rfind(' ', 0, target_chars + 100)
        if split_pos > 0:
            return split_pos + 1
        
        # 策略5：在目标位置附近找第一个非中文字符
        for i in range(target_chars, max(0, target_chars - 100), -1):
            if i < len(text) and not ('\u4e00' <= text[i] <= '\u9fff'):
                return i
        
        # 最后手段：在目标位置分割
        return target_chars
    
    def _calculate_overlap_start(self, chunk: str) -> int:
        """
        计算重叠部分的起始位置
        
        Args:
            chunk: 当前文本块
            
        Returns:
            重叠起始位置（字符索引）
        """
        if self.config.overlap_size <= 0:
            return len(chunk)  # 无重叠
        
        # 估算重叠字符数
        chunk_tokens = self.token_estimator.estimate_tokens(chunk)
        if chunk_tokens <= self.config.overlap_size:
            return 0  # 整个块作为重叠
        
        # 计算目标重叠字符数
        target_overlap_chars = int(
            len(chunk) * self.config.overlap_size / chunk_tokens
        )
        
        # 从块末尾向前找合适的边界
        search_start = max(0, len(chunk) - target_overlap_chars - 100)
        
        # 优先在句子边界开始
        for i in range(len(chunk) - 1, search_start, -1):
            if any(chunk[i:].startswith(ending) for ending in ['。', '！', '？', '.', '!', '?']):
                return i
        
        # 其次在逗号边界
        for i in range(len(chunk) - 1, search_start, -1):
            if chunk[i] in [',', '，', '、']:
                return i + 1
        
        # 最后在空格处
        for i in range(len(chunk) - 1, search_start, -1):
            if chunk[i] == ' ':
                return i + 1
        
        # 找不到边界，使用估算位置
        return max(0, len(chunk) - target_overlap_chars)
    
    def _postprocess_chunks(self, chunks: List[str]) -> List[str]:
        """
        后处理分块结果
        
        确保：
        1. 每个块都不为空
        2. 块大小在合理范围内
        3. 去除过小的块（合并到前一个块）
        
        Args:
            chunks: 原始分块列表
            
        Returns:
            处理后的分块列表
        """
        if not chunks:
            return []
        
        processed_chunks = []
        
        for chunk in chunks:
            if not chunk or not chunk.strip():
                continue
            
            chunk_tokens = self.token_estimator.estimate_tokens(chunk)
            
            # 检查块是否过小（且不是最后一个块）
            if (chunk_tokens < self.config.min_chunk_size and 
                processed_chunks and 
                chunk is not chunks[-1]):
                # 合并到前一个块
                processed_chunks[-1] = processed_chunks[-1] + " " + chunk.strip()
                continue
            
            # 检查块是否过大
            if chunk_tokens > self.config.max_chunk_size:
                logger.warning(f"块过大 ({chunk_tokens} tokens)，尝试重新分割")
                # 递归分割过大的块
                sub_chunks = self.chunk_text(chunk)
                processed_chunks.extend(sub_chunks)
            else:
                processed_chunks.append(chunk)
        
        return processed_chunks


# ========== 分块器工厂 ==========

class ChunkerFactory:
    """分块器工厂"""
    
    @staticmethod
    def create_chunker(config: Optional[ChunkConfig] = None):
        """
        创建分块器实例
        
        Args:
            config: 分块配置
            
        Returns:
            分块器实例
        """
        config = config or ChunkConfig()
        
        # 优先使用现有的分块器
        if HAS_INGESTION_CHUNKER:
            try:
                # 将配置转换为ingestion格式
                ingestion_config = IngestionChunkConfig(
                    chunk_size=config.chunk_size,
                    overlap_size=config.overlap_size,
                    min_chunk_size=config.min_chunk_size,
                    max_chunk_size=config.max_chunk_size
                )
                chunker = IngestionSmartChunker(ingestion_config)
                logger.info("使用ingestion.chunker")
                return chunker
            except Exception as e:
                logger.warning(f"使用ingestion.chunker失败: {e}")
        
        # 使用简化版分块器
        logger.info("使用简化版分块器")
        return SimpleChunker(config)


# ========== 全局函数接口 ==========

def chunk_text(text: str, 
               chunk_size: int = 512, 
               overlap_size: int = 128) -> List[str]:
    """
    分块文本（全局函数接口）
    
    Args:
        text: 输入文本
        chunk_size: 块大小（token数）
        overlap_size: 重叠大小（token数）
        
    Returns:
        文本块列表
    """
    config = ChunkConfig(chunk_size=chunk_size, overlap_size=overlap_size)
    chunker = ChunkerFactory.create_chunker(config)
    return chunker.chunk_text(text)


def estimate_tokens(text: str) -> int:
    """
    估算文本的token数量（全局函数接口）
    
    Args:
        text: 输入文本
        
    Returns:
        估算的token数量
    """
    estimator = SimpleTokenEstimator()
    return estimator.estimate_tokens(text)


if __name__ == "__main__":
    # 测试代码
    print("测试ingest分块器模块...")
    
    # 测试文本
    test_text = """
    这是一个测试文档。它包含多个段落。
    
    第一段有多个句子。这是第一段的第二句。这是第一段的第三句。
    
    第二段从这里开始。它也有多个句子。这是第二段的第二句。
    
    第三段是最后一段。它结束了这个文档。
    """
    
    print(f"测试文本长度: {len(test_text)}字符")
    print(f"估算token数: {estimate_tokens(test_text)}")
    
    # 测试分块
    print("\n测试分块功能:")
    chunks = chunk_text(test_text, chunk_size=50, overlap_size=10)
    
    print(f"生成 {len(chunks)} 个块:")
    for i, chunk in enumerate(chunks):
        chunk_tokens = estimate_tokens(chunk)
        print(f"\n--- 块 {i+1} (约{chunk_tokens} tokens) ---")
        print(chunk[:100] + "..." if len(chunk) > 100 else chunk)
    
    # 测试配置
    print("\n测试配置:")
    config = ChunkConfig(chunk_size=512, overlap_size=128)
    print(f"配置: {config.to_dict()}")