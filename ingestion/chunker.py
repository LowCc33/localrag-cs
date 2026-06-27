#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智能分块器模块
将长文本分割为适合RAG处理的文本块

核心功能：
1. 按token数量分块（512 tokens + 50 tokens重叠）
2. 智能边界处理：尽量在段落、句子边界处分割
3. 支持中英文混合文本
4. 保留上下文连贯性

设计原则：
- 语义完整性：尽量不在句子中间分割
- 重叠机制：确保上下文连贯
- 性能优化：避免重复计算token
- 可配置性：支持自定义块大小和重叠大小
"""

import re
import logging
from typing import List, Optional, Tuple
from dataclasses import dataclass

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
    overlap_size: int = 50       # 重叠的token数量
    min_chunk_size: int = 50     # 最小块大小（避免过小片段）
    max_chunk_size: int = 1024   # 最大块大小（安全限制）
    
    # 分割边界优先级
    sentence_endings: Tuple[str, ...] = ('.', '。', '!', '！', '?', '？', ';', '；')
    paragraph_endings: Tuple[str, ...] = ('\n\n', '\r\n\r\n')
    
    # 中文标点（用于中文文本分割）
    chinese_punctuation: str = '。！？；：，、'
    
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


class TokenEstimator:
    """token估算器
    
    由于准确计算token需要调用模型，这里使用近似估算：
    - 英文：1 token ≈ 4字符
    - 中文：1 token ≈ 2字符
    - 混合文本：加权平均
    
    注意：这只是估算，实际token数量可能有所不同
    """
    
    @staticmethod
    def estimate_tokens(text: str) -> int:
        """
        估算文本的token数量
        
        Args:
            text: 输入文本
            
        Returns:
            估算的token数量
        """
        if not text:
            return 0
        
        # 统计中文字符和英文字符
        chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        other_chars = len(text) - chinese_chars
        
        # 估算token：中文1字符≈2token，英文1字符≈0.25token
        estimated_tokens = chinese_chars * 2 + other_chars * 0.25
        
        # 向上取整，确保安全边界
        return int(estimated_tokens) + 1
    
    @staticmethod
    def find_split_position(text: str, max_tokens: int) -> int:
        """
        在文本中找到最佳分割位置
        
        策略：
        1. 先找段落边界（\n\n）
        2. 再找句子边界（。！？.?!）
        3. 再找逗号边界（，,）
        4. 最后在空格处分割
        
        Args:
            text: 待分割文本
            max_tokens: 最大token数限制
            
        Returns:
            最佳分割位置（字符索引），-1表示不需要分割
        """
        # 估算当前文本token数
        current_tokens = TokenEstimator.estimate_tokens(text)
        if current_tokens <= max_tokens:
            return -1  # 不需要分割
        
        # 计算目标字符数（基于估算比例）
        target_chars = int(len(text) * max_tokens / max(current_tokens, 1))
        target_chars = min(target_chars, len(text) - 1)
        
        # 策略1：在段落边界分割
        split_pos = TokenEstimator._find_boundary(
            text, target_chars, ['\n\n', '\r\n\r\n']
        )
        if split_pos > 0:
            return split_pos
        
        # 策略2：在句子边界分割（中文+英文）
        sentence_boundaries = ['.', '。', '!', '！', '?', '？', ';', '；']
        split_pos = TokenEstimator._find_boundary(
            text, target_chars, sentence_boundaries
        )
        if split_pos > 0:
            return split_pos
        
        # 策略3：在逗号边界分割
        comma_boundaries = [',', '，', '、']
        split_pos = TokenEstimator._find_boundary(
            text, target_chars, comma_boundaries
        )
        if split_pos > 0:
            return split_pos
        
        # 策略4：在空格处分割
        split_pos = TokenEstimator._find_boundary(
            text, target_chars, [' ']
        )
        if split_pos > 0:
            return split_pos
        
        # 策略5：如果都找不到，在目标位置附近找第一个非中文字符
        for i in range(target_chars, max(0, target_chars - 100), -1):
            if i < len(text) and not ('\u4e00' <= text[i] <= '\u9fff'):
                return i
        
        # 最后手段：在目标位置分割
        return target_chars
    
    @staticmethod
    def _find_boundary(text: str, target_pos: int, boundaries: List[str]) -> int:
        """
        在目标位置附近查找边界
        
        Args:
            text: 文本
            target_pos: 目标位置
            boundaries: 边界字符列表
            
        Returns:
            边界位置，-1表示未找到
        """
        # 向前查找（优先）
        for i in range(target_pos, max(0, target_pos - 200), -1):
            if i < len(text):
                for boundary in boundaries:
                    if text[i:].startswith(boundary):
                        return i + len(boundary)
        
        # 向后查找
        for i in range(target_pos, min(len(text), target_pos + 200)):
            if i < len(text):
                for boundary in boundaries:
                    if text[i:].startswith(boundary):
                        return i + len(boundary)
        
        return -1


class SmartChunker:
    """智能分块器"""
    
    def __init__(self, config: Optional[ChunkConfig] = None):
        """
        初始化分块器
        
        Args:
            config: 分块配置，None使用默认配置
        """
        self.config = config or ChunkConfig()
        self.config.validate()
        self.token_estimator = TokenEstimator()
    
    def chunk_text(self, text: str, title: str = "") -> List[str]:
        """
        将长文本分割为多个块
        
        Args:
            text: 输入文本
            title: 文档标题（可选），每个块会带上标题信息
            
        Returns:
            文本块列表
            
        Raises:
            ValueError: 输入文本为空
        """
        if not text or not text.strip():
            raise ValueError("输入文本不能为空")
        
        # 清理文本：去除首尾空白，合并多个空白行
        text = self._clean_text(text)
        
        chunks = []
        remaining_text = text
        
        logger.info(f"开始分块，文本长度: {len(text)}字符，估算token: {self.token_estimator.estimate_tokens(text)}")
        
        while remaining_text:
            # 估算剩余文本的token数
            remaining_tokens = self.token_estimator.estimate_tokens(remaining_text)
            
            # 如果剩余文本小于最小块大小，直接作为最后一个块
            if remaining_tokens < self.config.min_chunk_size and chunks:
                # 将剩余文本合并到最后一个块（如果有）
                if chunks:
                    chunks[-1] = chunks[-1] + " " + remaining_text.strip()
                else:
                    chunks.append(remaining_text.strip())
                break
            
            # 如果剩余文本可以直接作为一个块
            if remaining_tokens <= self.config.chunk_size:
                chunks.append(remaining_text.strip())
                break
            
            # 需要分割：找到最佳分割位置
            split_pos = self.token_estimator.find_split_position(
                remaining_text, self.config.chunk_size
            )
            
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
            
            # 计算重叠部分：从当前块末尾向前取overlap_size个token的内容
            overlap_start = self._calculate_overlap_start(current_chunk)
            remaining_text = remaining_text[overlap_start:]
            
            logger.debug(f"分块进度: 已生成{len(chunks)}个块，剩余字符: {len(remaining_text)}")
        
        # 后处理：确保块大小在合理范围内
        chunks = self._postprocess_chunks(chunks)
        
        logger.info(f"分块完成: 共{len(chunks)}个块")
        for i, chunk in enumerate(chunks):
            chunk_tokens = self.token_estimator.estimate_tokens(chunk)
            logger.debug(f"  块{i+1}: {chunk_tokens} tokens, {len(chunk)}字符")
        
        return chunks
    
    def chunk_text_with_title(self, text: str, title: str = "") -> List[str]:
        """
        将长文本分割为多个块，每个块前面加上标题
        
        如果 title 为空，行为与 chunk_text 相同。
        如果 title 非空，每个块前面会加上 "[标题] {title}\\n" 前缀。
        
        Args:
            text: 输入文本
            title: 文档标题
            
        Returns:
            文本块列表（每个块已包含标题前缀）
        """
        if not title:
            return self.chunk_text(text)
        
        # 在文本前面加上标题，让分块时标题作为每个块的一部分
        # 但为了避免标题被分到单独的块里，先正常分块，再给每个块加标题前缀
        chunks = self.chunk_text(text)
        result = []
        for chunk in chunks:
            result.append(f"[标题] {title}\n{chunk}")
        return result
    
    def _clean_text(self, text: str) -> str:
        """清理文本"""
        # 去除首尾空白
        text = text.strip()
        
        # 合并多个空白行（3个以上换行合并为2个）
        text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)
        
        # 合并多个连续空格
        text = re.sub(r' {2,}', ' ', text)
        
        return text
    
    def _calculate_overlap_start(self, chunk: str) -> int:
        """
        计算重叠部分的起始位置
        
        从当前块末尾向前取overlap_size个token的内容
        尽量在句子或段落边界处开始
        
        Args:
            chunk: 当前文本块
            
        Returns:
            重叠起始位置（字符索引）
        """
        if self.config.overlap_size <= 0:
            return len(chunk)  # 无重叠
        
        # 估算overlap_size对应的字符数
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
            if any(chunk[i:].startswith(ending) for ending in self.config.sentence_endings):
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
    
    def chunk_documents(self, documents: List[str]) -> List[List[str]]:
        """
        批量处理多个文档
        
        Args:
            documents: 文档文本列表
            
        Returns:
            每个文档的分块列表
        """
        all_chunks = []
        
        for i, doc in enumerate(documents):
            if not doc or not doc.strip():
                logger.warning(f"文档{i+1}为空，跳过")
                all_chunks.append([])
                continue
            
            try:
                chunks = self.chunk_text(doc)
                all_chunks.append(chunks)
                logger.info(f"文档{i+1}分块完成: {len(chunks)}个块")
            except Exception as e:
                logger.error(f"文档{i+1}分块失败: {e}")
                all_chunks.append([])
        
        return all_chunks


# 全局函数接口
def chunk_text(text: str, chunk_size: int = 512, overlap_size: int = 50) -> List[str]:
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
    chunker = SmartChunker(config)
    return chunker.chunk_text(text)


if __name__ == "__main__":
    # 测试代码
    test_text = """
    这是一个测试文档。它包含多个段落。
    
    第一段有多个句子。这是第一段的第二句。这是第一段的第三句。
    
    第二段从这里开始。它也有多个句子。这是第二段的第二句。
    
    第三段是最后一段。它结束了这个文档。
    """
    
    print("测试分块功能...")
    chunks = chunk_text(test_text, chunk_size=50, overlap_size=10)
    
    print(f"生成 {len(chunks)} 个块:")
    for i, chunk in enumerate(chunks):
        print(f"\n--- 块 {i+1} ---")
        print(chunk[:200] + "..." if len(chunk) > 200 else chunk)