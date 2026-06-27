#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LocalRAG-CS 数据导入模块（SQLite版本）
基于SQLite + SQLAlchemy + llama-cpp-python的独立数据导入系统

核心功能：
1. 文件解析：支持.txt, .md, .pdf, .docx, .csv格式
2. 文本分块：按token数分块，支持重叠
3. 向量嵌入：使用llama-cpp-python加载bge-small-zh-v1.5模型
4. 本地存储：SQLite数据库，支持文件哈希去重
5. 命令行接口：支持单文件和目录批量导入

设计原则：
- 纯Python实现，不依赖外部服务
- 最小依赖，尽量复用现有代码
- 完整错误处理和降级逻辑
- 幂等设计：重复导入同一文件不产生重复数据

模块结构：
- cli.py: 命令行接口
- parsers.py: 文件解析器（复用ingestion.parsers）
- chunker.py: 文本分块器（复用ingestion.chunker）
- embedder.py: 嵌入模型（llama-cpp-python）
- storage.py: SQLite存储（SQLAlchemy）
- pipeline.py: 处理流水线

版本: 1.0.0
作者: LocalRAG-CS Team
"""

__version__ = "1.0.0"
__author__ = "LocalRAG-CS Team"