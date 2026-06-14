#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据导入命令行接口
提供从文件到ES的完整处理流水线

功能：
1. 支持目录批量处理（--dir）
2. 支持单文件处理（--file）
3. 进度显示和详细日志
4. 结果统计和错误报告
5. 配置文件参数（分块大小、重叠大小等）

设计原则：
- 用户友好：清晰的命令行帮助和进度反馈
- 错误容忍：单个文件失败不影响整体流程
- 可配置性：支持多种参数调整
- 可恢复性：记录处理状态，支持断点续传
"""

import os
import sys
import argparse
import logging
from pathlib import Path

# 添加项目根目录到Python路径
sys.path.insert(0, str(Path(__file__).parent.parent))

# 导入项目配置
import config

# 导入流水线模块
from ingestion.pipeline import (
    process_directory, 
    process_single_file,
    ProcessingStats
)
from ingestion.parsers import get_supported_extensions
from ingestion.chunker import ChunkConfig


def setup_logging(verbose: bool = False) -> None:
    """配置日志级别"""
    level = logging.DEBUG if verbose else logging.INFO
    
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    # 设置第三方库的日志级别
    logging.getLogger('elasticsearch').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)


def print_banner() -> None:
    """打印程序横幅"""
    banner = """
    ╔══════════════════════════════════════════════════════════╗
    ║              LocalRAG-CS 数据导入工具                    ║
    ║                版本 1.0.0 - 命令行接口                   ║
    ╚══════════════════════════════════════════════════════════╝
    """
    print(banner)


def print_config_summary(args: argparse.Namespace) -> None:
    """打印配置摘要"""
    print("\n" + "=" * 60)
    print("配置摘要")
    print("=" * 60)
    print(f"处理模式: {'目录批量' if args.dir else '单文件'}")
    
    if args.dir:
        print(f"目录路径: {args.dir}")
    elif args.file:
        print(f"文件路径: {args.file}")
    
    print(f"ES索引: {args.index_name}")
    print(f"分块大小: {args.chunk_size} tokens")
    print(f"重叠大小: {args.overlap_size} tokens")
    print(f"详细日志: {'是' if args.verbose else '否'}")
    print(f"跳过错误: {'是' if args.skip_errors else '否'}")
    print("=" * 60 + "\n")


def print_supported_formats() -> None:
    """打印支持的格式"""
    extensions = get_supported_extensions()
    print("\n支持的文件格式:")
    for ext in sorted(extensions):
        print(f"  - {ext}")
    print()


def validate_directory(directory: str) -> bool:
    """验证目录是否有效"""
    path = Path(directory)
    
    if not path.exists():
        print(f"错误: 目录不存在: {directory}")
        return False
    
    if not path.is_dir():
        print(f"错误: 不是目录: {directory}")
        return False
    
    # 检查目录是否可读
    if not os.access(path, os.R_OK):
        print(f"错误: 目录不可读: {directory}")
        return False
    
    return True


def validate_file(file_path: str) -> bool:
    """验证文件是否有效"""
    path = Path(file_path)
    
    if not path.exists():
        print(f"错误: 文件不存在: {file_path}")
        return False
    
    if not path.is_file():
        print(f"错误: 不是文件: {file_path}")
        return False
    
    # 检查文件是否可读
    if not os.access(path, os.R_OK):
        print(f"错误: 文件不可读: {file_path}")
        return False
    
    # 检查文件扩展名
    extensions = get_supported_extensions()
    if path.suffix.lower() not in extensions:
        print(f"警告: 不支持的文件格式: {path.suffix}")
        print("支持格式:", ", ".join(extensions))
        # 不返回False，让用户决定是否继续
    
    return True


def process_with_progress(
    pipeline_func,
    *args,
    **kwargs
) -> ProcessingStats:
    """带进度显示的处理函数"""
    # 这里可以添加进度条，暂时使用简单日志
    return pipeline_func(*args, **kwargs)


def main() -> int:
    """主函数"""
    parser = argparse.ArgumentParser(
        description="LocalRAG-CS 数据导入工具 - 将文件导入到Elasticsearch",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 处理整个目录
  python -m ingestion.cli --dir ~/documents/
  
  # 处理单个文件
  python -m ingestion.cli --file ~/documents/manual.pdf
  
  # 自定义参数
  python -m ingestion.cli --dir ~/documents/ --chunk-size 256 --overlap 25
  
  # 查看支持的格式
  python -m ingestion.cli --list-formats
        """
    )
    
    # 互斥参数组：处理模式
    mode_group = parser.add_mutually_exclusive_group(required=False)
    mode_group.add_argument(
        '--dir',
        type=str,
        help='处理指定目录下的所有文件'
    )
    mode_group.add_argument(
        '--file',
        type=str,
        help='处理单个文件'
    )
    mode_group.add_argument(
        '--list-formats',
        action='store_true',
        help='列出支持的文件格式'
    )
    
    # 处理参数
    parser.add_argument(
        '--index-name',
        type=str,
        default=config.ES_INDEX_NAME,
        help=f'Elasticsearch索引名称（默认: {config.ES_INDEX_NAME}）'
    )
    parser.add_argument(
        '--chunk-size',
        type=int,
        default=512,
        help='分块大小（token数量，默认: 512）'
    )
    parser.add_argument(
        '--overlap-size',
        type=int,
        default=50,
        help='块重叠大小（token数量，默认: 50）'
    )
    parser.add_argument(
        '--min-chunk-size',
        type=int,
        default=50,
        help='最小块大小（token数量，默认: 50）'
    )
    parser.add_argument(
        '--skip-errors',
        action='store_true',
        help='跳过处理错误的文件继续处理其他文件'
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='显示详细日志'
    )
    parser.add_argument(
        '--version',
        action='version',
        version='LocalRAG-CS 数据导入工具 v1.0.0'
    )
    
    args = parser.parse_args()
    
    # 设置日志
    setup_logging(args.verbose)
    logger = logging.getLogger(__name__)
    
    # 打印横幅
    print_banner()
    
    # 列出支持的格式
    if args.list_formats:
        print_supported_formats()
        return 0
    
    # 检查必须指定处理模式
    if not args.dir and not args.file:
        parser.print_help()
        print("\n错误: 必须指定 --dir 或 --file 参数")
        return 1
    
    # 打印配置摘要
    print_config_summary(args)
    
    try:
        # 验证分块配置
        if args.chunk_size <= 0:
            print("错误: chunk-size 必须大于0")
            return 1
        if args.overlap_size < 0:
            print("错误: overlap-size 不能为负数")
            return 1
        if args.overlap_size >= args.chunk_size:
            print("错误: overlap-size 必须小于 chunk-size")
            return 1
        if args.min_chunk_size <= 0:
            print("错误: min-chunk-size 必须大于0")
            return 1
        
        # 创建分块配置（用于验证，实际在pipeline中使用）
        _ = ChunkConfig(
            chunk_size=args.chunk_size,
            overlap_size=args.overlap_size,
            min_chunk_size=args.min_chunk_size
        )
        
        # 处理目录
        if args.dir:
            if not validate_directory(args.dir):
                return 1
            
            print(f"开始处理目录: {args.dir}")
            print("正在扫描文件...")
            
            # 获取目录中的文件数量（预估）
            extensions = get_supported_extensions()
            file_count = 0
            for ext in extensions:
                file_count += len(list(Path(args.dir).glob(f"**/*{ext}")))
            
            if file_count == 0:
                print("警告: 目录中没有找到支持的文件")
                print_supported_formats()
                return 1
            
            print(f"找到 {file_count} 个支持的文件")
            print("开始处理...\n")
            
            # 处理目录
            stats = process_with_progress(
                process_directory,
                args.dir,
                index_name=args.index_name,
                chunk_size=args.chunk_size,
                overlap_size=args.overlap_size
            )
            
            # 显示结果
            print("\n" + "=" * 60)
            print("处理完成!")
            print("=" * 60)
            print(f"总文件数: {stats.total_files}")
            print(f"成功处理: {stats.processed_files}")
            print(f"处理失败: {stats.failed_files}")
            print(f"总块数: {stats.total_chunks}")
            print(f"成功块数: {stats.successful_chunks}")
            print(f"失败块数: {stats.failed_chunks}")
            print(f"成功率: {stats.success_rate:.1f}%")
            print(f"总耗时: {stats.elapsed_time:.2f}秒")
            print("=" * 60)
            
            if stats.failed_files > 0:
                print(f"\n警告: {stats.failed_files} 个文件处理失败")
                if not args.skip_errors:
                    print("使用 --skip-errors 参数可以跳过错误继续处理")
            
            return 0 if stats.failed_files == 0 or args.skip_errors else 1
        
        # 处理单个文件
        elif args.file:
            if not validate_file(args.file):
                return 1
            
            print(f"开始处理文件: {args.file}")
            print("正在解析文件...\n")
            
            # 处理文件
            result = process_with_progress(
                process_single_file,
                args.file,
                index_name=args.index_name,
                chunk_size=args.chunk_size,
                overlap_size=args.overlap_size
            )
            
            # 显示结果
            print("\n" + "=" * 60)
            print("处理完成!")
            print("=" * 60)
            print(f"文件: {Path(args.file).name}")
            print(f"状态: {result.status}")
            print(f"块数: {result.chunks_count}")
            print(f"成功块数: {result.successful_chunks}")
            print(f"失败块数: {result.failed_chunks}")
            print(f"处理时间: {result.processing_time:.2f}秒")
            
            if result.error:
                print(f"错误: {result.error}")
            
            print("=" * 60)
            
            return 0 if result.status == 'success' else 1
    
    except KeyboardInterrupt:
        print("\n\n处理被用户中断")
        return 130  # SIGINT退出码
    
    except Exception as e:
        logger.error(f"处理失败: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main())