#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据导入模块单元测试
测试文件解析、分块、流水线等核心功能
"""

import os
import sys
import tempfile
from pathlib import Path
import pytest

# 添加项目根目录到Python路径
sys.path.insert(0, str(Path(__file__).parent.parent))

# 导入测试模块
from ingestion.parsers import (
    parse_file, 
    get_supported_extensions,
    TextParser,
    MarkdownParser
)
from ingestion.chunker import chunk_text, SmartChunker, ChunkConfig
from ingestion.pipeline import IngestionPipeline, ProcessingStats, FileResult


class TestParsers:
    """文件解析器测试"""
    
    def test_supported_extensions(self):
        """测试支持的扩展名"""
        extensions = get_supported_extensions()
        assert isinstance(extensions, list)
        assert '.txt' in extensions
        assert '.md' in extensions
        assert '.markdown' in extensions
    
    def test_text_parser(self):
        """测试文本文件解析器"""
        parser = TextParser()
        assert parser.supported_extensions == ['.txt']
        
        # 创建测试文件
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("这是一个测试文本文件。\n第二行内容。")
            temp_path = f.name
        
        try:
            content = parser.parse(Path(temp_path))
            assert content is not None
            assert "测试文本文件" in content
            assert "第二行内容" in content
        finally:
            os.unlink(temp_path)
    
    def test_markdown_parser(self):
        """测试Markdown文件解析器"""
        parser = MarkdownParser()
        assert '.md' in parser.supported_extensions
        assert '.markdown' in parser.supported_extensions
        
        # 创建测试Markdown文件
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write("# 标题\n\n**粗体** 和 *斜体* 文本。\n\n[链接](http://example.com)")
            temp_path = f.name
        
        try:
            content = parser.parse(Path(temp_path))
            assert content is not None
            assert "标题" in content
            assert "粗体" in content  # 应该保留文本
            assert "斜体" in content  # 应该保留文本
            assert "链接" in content  # 应该保留链接文本
            assert "**" not in content  # 应该去除Markdown标记
            assert "*" not in content   # 应该去除Markdown标记
            assert "http://example.com" not in content  # 应该去除URL
        finally:
            os.unlink(temp_path)
    
    def test_parse_file_function(self):
        """测试parse_file全局函数"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("全局函数测试")
            temp_path = f.name
        
        try:
            content = parse_file(temp_path)
            assert content == "全局函数测试"
        finally:
            os.unlink(temp_path)
    
    def test_markdown_extract_title(self):
        """测试Markdown解析器提取标题"""
        parser = MarkdownParser()
        
        # 测试：从内容中提取第一个 # 标题
        content = "# 雇主责任险理赔流程\n\n这是文档内容。"
        title = parser.extract_title(Path("test.md"), content)
        assert title == "雇主责任险理赔流程"
        
        # 测试：无标题时回退到文件名
        content = "没有标题的文档内容。"
        title = parser.extract_title(Path("无标题文档.md"), content)
        assert title == "无标题文档"
        
        # 测试：多个 # 标题，只取第一个
        content = "# 第一个标题\n\n## 第二个标题\n\n内容"
        title = parser.extract_title(Path("test.md"), content)
        assert title == "第一个标题"
        
        # 测试：ParserFactory 的 extract_title 接口
        from ingestion.parsers import ParserFactory
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write("# 工厂测试标题\n\n内容")
            temp_path = f.name
        try:
            title = ParserFactory.extract_title(Path(temp_path), "# 工厂测试标题\n\n内容")
            assert title == "工厂测试标题"
        finally:
            os.unlink(temp_path)
    
    def test_text_parser_extract_title(self):
        """测试文本解析器提取标题（回退到文件名）"""
        parser = TextParser()
        title = parser.extract_title(Path("理赔说明.txt"), "文档内容")
        assert title == "理赔说明"
    
    def test_chunk_text_with_title(self):
        """测试带标题的分块"""
        from ingestion.chunker import SmartChunker, ChunkConfig
        config = ChunkConfig(chunk_size=100, overlap_size=10)
        chunker = SmartChunker(config)
        
        # 短文本（不分块），带标题
        text = "这是文档内容。"
        chunks = chunker.chunk_text_with_title(text, title="测试文档")
        assert len(chunks) == 1
        assert "[标题] 测试文档" in chunks[0]
        assert "这是文档内容" in chunks[0]
        
        # 空标题时行为与 chunk_text 相同
        chunks_no_title = chunker.chunk_text_with_title(text, title="")
        assert chunks_no_title == chunker.chunk_text(text)
        
        # 长文本分块，每个块都带标题
        long_text = "这是文档内容。" * 50
        chunks = chunker.chunk_text_with_title(long_text, title="长文档")
        assert len(chunks) > 1
        for chunk in chunks:
            assert "[标题] 长文档" in chunk


class TestChunker:
    """分块器测试"""
    
    def test_chunk_text_basic(self):
        """测试基本分块功能"""
        text = "这是一个测试文本。" * 20  # 创建长文本
        chunks = chunk_text(text, chunk_size=50, overlap_size=10)
        
        assert isinstance(chunks, list)
        assert len(chunks) > 0
        
        # 检查每个块都不为空
        for chunk in chunks:
            assert chunk and chunk.strip()
        
        # 检查总文本长度
        total_chars = sum(len(chunk) for chunk in chunks)
        assert total_chars >= len(text) * 0.8  # 允许一些重叠和边界调整
    
    def test_chunker_config(self):
        """测试分块器配置"""
        config = ChunkConfig(
            chunk_size=100,
            overlap_size=20,
            min_chunk_size=30
        )
        
        chunker = SmartChunker(config)
        assert chunker.config.chunk_size == 100
        assert chunker.config.overlap_size == 20
        assert chunker.config.min_chunk_size == 30
    
    def test_small_text(self):
        """测试小文本分块"""
        text = "这是一个短文本。"
        chunks = chunk_text(text, chunk_size=100, overlap_size=20)
        
        assert len(chunks) == 1
        assert chunks[0] == text
    
    def test_empty_text(self):
        """测试空文本"""
        with pytest.raises(ValueError):
            chunk_text("", chunk_size=100, overlap_size=20)
        
        with pytest.raises(ValueError):
            chunk_text("   ", chunk_size=100, overlap_size=20)


class TestPipeline:
    """流水线测试"""
    
    def test_processing_stats(self):
        """测试处理统计"""
        stats = ProcessingStats()
        stats.total_files = 10
        stats.processed_files = 8
        stats.failed_files = 2
        stats.total_chunks = 100
        stats.successful_chunks = 95
        stats.failed_chunks = 5
        
        stats.start()
        stats.end()
        
        assert stats.elapsed_time > 0
        assert stats.success_rate == 80.0  # 8/10 * 100
    
    def test_file_result(self):
        """测试文件处理结果"""
        result = FileResult(
            file_path=Path("/tmp/test.txt"),
            status="success",
            error=None,
            chunks_count=10,
            successful_chunks=10,
            failed_chunks=0,
            processing_time=1.5
        )
        
        assert result.status == "success"
        assert result.chunks_count == 10
        assert result.successful_chunks == 10
        assert result.failed_chunks == 0
        assert result.processing_time == 1.5
    
    def test_pipeline_initialization(self):
        """测试流水线初始化"""
        # 注意：这个测试需要ES服务可用，所以可能会跳过
        try:
            pipeline = IngestionPipeline()
            assert pipeline is not None
            assert hasattr(pipeline, 'es_client')
            assert hasattr(pipeline, 'embedding_client')
            assert hasattr(pipeline, 'chunker')
        except Exception as e:
            # 如果ES不可用，跳过测试
            pytest.skip(f"ES服务不可用: {e}")


class TestCLI:
    """命令行接口测试"""
    
    def test_cli_import(self):
        """测试CLI模块导入"""
        # 确保可以导入CLI模块
        import ingestion.cli
        assert hasattr(ingestion.cli, 'main')
    
    def test_config_import(self):
        """测试配置模块导入"""
        import config
        assert hasattr(config, 'ES_INDEX_NAME')
        assert hasattr(config, 'ES_HOST')


if __name__ == "__main__":
    pytest.main([__file__, "-v"])