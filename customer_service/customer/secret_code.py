# -*- coding: utf-8 -*-
"""
暗号生成工具
生成3-4位字母+数字组合的唯一暗号，用于客户归因
格式：1个大写字母 + 3个数字（如 A857、K392）
"""

import random


def generate_secret_code() -> str:
    """
    生成一个4位暗号（1个字母 + 3个数字）

    Returns:
        暗号字符串，如 "A857"
    """
    # 字母池：去掉容易混淆的 I、O、l、0、1 等
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    digits = "23456789"  # 去掉 0、1，避免和字母 O、I 混淆

    code = (
        random.choice(letters)
        + random.choice(digits)
        + random.choice(digits)
        + random.choice(digits)
    )
    return code
