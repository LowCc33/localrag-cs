#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件解析器模块（ingest版本）
复用ingestion.parsers的核心功能，适配SQLite存储

核心功能：
1. 支持多种文件格式：.txt, .md, .pdf, .docx, .csv
2. 自动编码检测，支持中文不乱码
3. 解析失败友好错误处理，不崩溃
4. 文件大小限制和格式验证

设计原则：
- 复用现有代码，减少重复开发
- 最小依赖，PDF/DOCX解析为可选功能
- 完整错误处理，单个文件失败不影响整体
- 支持中文字符编码
"""

import sys
import logging
from pathlib import Path
from typing import Optional

# 添加项目根目录到Python路径
sys.path.insert(0, str(Path(__file__).parent.parent))

# 尝试导入现有的解析器
try:
    from ingestion.parsers import (
        ParserFactory as IngestionParserFactory
    )
    HAS_INGESTION_PARSERS = True
except ImportError:
    HAS_INGESTION_PARSERS = False
    logger = logging.getLogger(__name__)
    logger.warning("ingestion.parsers模块不可用，将使用简化版解析器")

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class CSVTextParser:
    """CSV文件解析器（简化版）"""
    
    def __init__(self):
        self.supported_extensions = ['.csv']
    
    def parse(self, file_path: Path) -> Optional[str]:
        """
        解析CSV文件
        
        Args:
            file_path: CSV文件路径
            
        Returns:
            CSV文本内容，解析失败返回None
        """
        try:
            import csv
            
            content_parts = []
            
            with open(file_path, 'r', encoding='utf-8') as f:
                # 尝试检测编码
                try:
                    csv_reader = csv.reader(f)
                    
                    # 读取所有行
                    for row_num, row in enumerate(csv_reader, 1):
                        if row:
                            # 将行转换为文本
                            row_text = ' | '.join(str(cell).strip() for cell in row if str(cell).strip())
                            if row_text:
                                content_parts.append(row_text)
                        
                        # 每100行记录一次进度
                        if row_num % 100 == 0:
                            logger.debug(f"CSV解析进度: {row_num}行")
                
                except UnicodeDecodeError:
                    # 尝试其他编码
                    encodings = ['gbk', 'gb2312', 'latin-1']
                    for encoding in encodings:
                        try:
                            f.seek(0)  # 重置文件指针
                            csv_reader = csv.reader(f, encoding=encoding)
                            
                            content_parts = []
                            for row in csv_reader:
                                if row:
                                    row_text = ' | '.join(str(cell).strip() for cell in row if str(cell).strip())
                                    if row_text:
                                        content_parts.append(row_text)
                            
                            break  # 成功解析，退出循环
                        except UnicodeDecodeError:
                            continue
            
            if not content_parts:
                logger.warning(f"CSV文件无文本内容: {file_path.name}")
                return None
            
            # 合并所有内容
            full_content = '\n'.join(content_parts)
            logger.info(f"CSV解析完成: {file_path.name}")
            
            return full_content
            
        except ImportError:
            logger.error("csv模块不可用，无法解析CSV文件")
            return None
        except Exception as e:
            logger.error(f"解析CSV文件失败 {file_path}: {e}")
            return None


class IngestParserFactory:
    """ingest模块的解析器工厂"""
    
    _parsers = None
    
    @classmethod
    def get_parsers(cls):
        """获取所有解析器实例"""
        if cls._parsers is None:
            cls._parsers = []
            
            # 如果有ingestion.parsers，复用其解析器
            if HAS_INGESTION_PARSERS:
                ingestion_parsers = IngestionParserFactory.get_parsers()
                for parser in ingestion_parsers:
                    # 只添加需要的解析器
                    if hasattr(parser, 'supported_extensions'):
                        cls._parsers.append(parser)
                        logger.debug(f"复用解析器: {parser.__class__.__name__}")
            
            # 添加CSV解析器
            csv_parser = CSVTextParser()
            cls._parsers.append(csv_parser)
            logger.debug(f"添加解析器: {csv_parser.__class__.__name__}")
        
        return cls._parsers
    
    @classmethod
    def get_parser_for_file(cls, file_path: Path) -> Optional[object]:
        """
        根据文件扩展名获取合适的解析器
        
        Args:
            file_path: 文件路径对象
            
        Returns:
            匹配的解析器实例，无匹配返回None
        """
        for parser in cls.get_parsers():
            if hasattr(parser, 'supported_extensions'):
                if file_path.suffix.lower() in parser.supported_extensions:
                    return parser
        return None
    
    @classmethod
    def parse_file(cls, file_path: Path) -> Optional[str]:
        """
        解析单个文件
        
        Args:
            file_path: 文件路径对象
            
        Returns:
            文件文本内容，解析失败返回None
        """
        # 检查文件是否存在
        if not file_path.exists():
            logger.error(f"文件不存在: {file_path}")
            return None
        
        # 检查文件是否可读
        if not file_path.is_file():
            logger.error(f"不是文件: {file_path}")
            return None
        
        # 检查文件大小（避免处理超大文件）
        file_size = file_path.stat().st_size
        MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB限制
        
        if file_size > MAX_FILE_SIZE:
            logger.warning(f"文件过大 ({file_size/1024/1024:.1f}MB > 100MB): {file_path.name}")
            return None
        
        # 获取合适的解析器
        parser = cls.get_parser_for_file(file_path)
        if not parser:
            logger.warning(f"不支持的文件格式: {file_path.suffix}")
            return None
        
        # 解析文件
        logger.info(f"开始解析文件: {file_path.name}")
        content = parser.parse(file_path)
        
        if content:
            logger.info(f"文件解析成功: {file_path.name}，字符数: {len(content)}")
        else:
            logger.warning(f"文件解析失败: {file_path.name}")
        
        return content


# ========== 全局函数接口 ==========

def parse_file(file_path: str) -> Optional[str]:
    """
    解析单个文件（全局函数接口）
    
    Args:
        file_path: 文件路径字符串
        
    Returns:
        文件文本内容，解析失败返回None
    """
    return IngestParserFactory.parse_file(Path(file_path))


def get_supported_extensions() -> list:
    """获取所有支持的文件扩展名"""
    extensions = []
    for parser in IngestParserFactory.get_parsers():
        if hasattr(parser, 'supported_extensions'):
            extensions.extend(parser.supported_extensions)
    return list(set(extensions))


def is_supported_file(file_path: str) -> bool:
    """
    检查文件是否支持
    
    Args:
        file_path: 文件路径
        
    Returns:
        是否支持
    """
    file_ext = Path(file_path).suffix.lower()
    return file_ext in get_supported_extensions()


if __name__ == "__main__":
    # 测试代码
    print("测试ingest解析器模块...")
    
    # 获取支持的扩展名
    extensions = get_supported_extensions()
    print(f"支持的扩展名: {extensions}")
    
    # 测试文件支持检查
    test_files = [
        "/tmp/test.txt",
        "/tmp/test.md",
        "/tmp/test.pdf",
        "/tmp/test.docx",
        "/tmp/test.csv",
        "/tmp/test.unsupported"
    ]
    
    print("\n文件支持检查:")
    for test_file in test_files:
        supported = is_supported_file(test_file)
        print(f"  {test_file}: {'✅ 支持' if supported else '❌ 不支持'}")