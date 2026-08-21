# -*- coding: utf-8 -*-
"""
客户管理子模块
包含：客户档案数据库、CRUD操作、暗号生成、信息提取、结尾生成
"""
from .customer_db import CustomerDB, customer_db
from .secret_code import generate_secret_code
from .info_extractor import InfoExtractor
from .ending_generator import EndingGenerator

__all__ = [
    "CustomerDB",
    "customer_db",
    "generate_secret_code",
    "InfoExtractor",
    "EndingGenerator",
]
