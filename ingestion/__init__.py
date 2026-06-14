#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LocalRAG-CS 数据导入模块
提供从多种格式文件到ES索引的完整处理流水线

主要功能：
1. 文件解析：支持 .md, .txt, .pdf, .docx 格式
2. 智能分块：512 tokens + 50 tokens 重叠
3. 向量化：复用现有 embedding 客户端
4. 索引写入：复用现有 ES 客户端
5. 命令行接口：支持目录批量处理和单文件处理

模块结构：
- cli.py: 命令行入口
- parsers.py: 文件解析器
- chunker.py: 智能分块器
- pipeline.py: 处理流水线
"""

__version__ = "1.0.0"
__author__ = "LocalRAG-CS Team"