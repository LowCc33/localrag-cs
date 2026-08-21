# -*- coding: utf-8 -*-
"""
模板清理脚本：删除所有模板中的加微信话术和信息收集提问
保留核心答案内容
"""

import re
import sys
from pathlib import Path


# 加微信相关关键词（包含这些的句子要删）
WECHAT_KEYWORDS = [
    "加微信", "加我微信", "加个微信", "加您微信", "加她微信",
    "微信我", "微信吧", "微信聊", "微信联系", "微信发",
    "微信是", "微信号", "加我吧", "加个吧", "方便加",
    "加我", "加个", "加您",
    "微信{{wechat_id}}", "微信{{", "微信 1", "微信1",
    "加我发", "微信发给", "微信给您",
    "微信号发我", "微信给我", "加一下",
    "留个微信", "留个联系方式", "留个联系",
    "我加您微信", "我加你微信",
]

# 信息收集提问关键词（包含这些的问句要删）
INFO_QUESTION_KEYWORDS = [
    "您主要做哪些柜子", "做什么柜子", "做哪些柜子", "需要做哪些",
    "想定制什么", "想做什么柜子", "要做什么", "想做哪些",
    "多大面积", "多少平方", "多少平", "面积多大", "几个平方",
    "做多大", "大概多大", "面积大概", "多大呀", "多大呢",
    "看重哪方面", "更在意", "看重什么", "更看重", "重视什么",
    "您是哪个小区", "哪个小区的", "什么小区", "哪个小区",
    "哪小区", "小区叫什么", "叫什么小区",
    "装修到什么", "装修进度", "装到哪一步", "装修怎么样",
    "装修到哪", "装到什么", "房子装修",
    "量过房", "量房了", "有没有量房", "量房了吗", "量房没",
    "量过房了吗", "已经量房",
    "联系方式", "留个电话", "留个联系", "留个手机号",
    "电话方便", "手机号方便", "留个微信",
    "您电话是", "您的电话", "方便说一下电话", "留个电话吧",
    "留个联系方式", "留个联系电话",
    "您叫什么", "怎么称呼", "您贵姓", "您怎么称呼",
]


def is_wechat_sentence(sentence: str) -> bool:
    """判断一句话是不是加微信相关的"""
    s = sentence.strip()
    if not s:
        return False
    for kw in WECHAT_KEYWORDS:
        if kw in s:
            return True
    return False


def is_info_question(sentence: str) -> bool:
    """判断一句话是不是信息收集类提问"""
    s = sentence.strip()
    if not s:
        return False
    # 必须是问句（有问号或者"吗""呀""呢"结尾）
    is_question = "？" in s or "?" in s or s.endswith("吗") or s.endswith("呀") or s.endswith("呢")
    if not is_question:
        return False
    for kw in INFO_QUESTION_KEYWORDS:
        if kw in s:
            return True
    return False


def clean_text(text: str) -> str:
    """
    清理一段文本中的加微信话术和信息收集提问
    按句子粒度删除
    """
    if not text:
        return text

    # 按中英文句号、问号、感叹号、换行拆分句子
    # 保留分隔符，后面重建
    parts = re.split(r'([。？！\.!?\n])', text)

    result_parts = []
    skip_next_punct = False

    for i, part in enumerate(parts):
        if skip_next_punct:
            skip_next_punct = False
            continue

        # 如果是分隔符，直接保留（前提是前面的句子没被删）
        if part in '。？！.！?\n':
            if i > 0:  # 前面的句子如果保留了，分隔符也保留
                prev_idx = len(result_parts) - 1
                if prev_idx >= 0 and result_parts[prev_idx]:
                    result_parts.append(part)
            continue

        sentence = part.strip()
        if not sentence:
            result_parts.append(part)
            continue

        # 检查是不是要删除的句子
        if is_wechat_sentence(sentence):
            skip_next_punct = True  # 跳过后面跟着的标点
            continue

        if is_info_question(sentence):
            skip_next_punct = True
            continue

        result_parts.append(part)

    result = "".join(result_parts)

    # 清理多余的空行和空白
    result = re.sub(r'\n{3,}', '\n\n', result)
    result = result.strip()

    return result


def clean_yaml_file(filepath: Path) -> int:
    """
    清理一个 yaml 模板文件
    返回修改的行数
    """
    lines = filepath.read_text(encoding='utf-8').split('\n')
    modified = 0

    for i, line in enumerate(lines):
        # 只处理以 - 或 " 开头的模板行（即模板内容）
        stripped = line.strip()
        if not stripped:
            continue
        if not (stripped.startswith('-') or stripped.startswith('"') or stripped.startswith("'")):
            continue

        # 提取文本内容
        # 简单处理：清理整个行的文本部分
        original_line = line

        # 直接对整行做清理（包含jinja变量和yaml格式）
        # 更安全的方式：只清理引号之间的内容
        if '"' in line or "'" in line:
            # 找到第一个和最后一个引号之间的内容
            quote_char = '"' if '"' in line else "'"
            first_quote = line.find(quote_char)
            last_quote = line.rfind(quote_char)
            if first_quote != last_quote and first_quote >= 0:
                content = line[first_quote + 1:last_quote]
                cleaned = clean_text(content)
                # 只有内容变了才替换
                if cleaned != content:
                    # 重建整行
                    indent = line[:first_quote]
                    lines[i] = f"{indent}{quote_char}{cleaned}{quote_char}"
                    modified += 1
        else:
            # 没有引号的行（比如 yaml 列表项 "- 文本..."）
            # 找到 "- " 后面的内容
            list_match = re.match(r'^(\s*-\s+)(.*)', line)
            if list_match:
                indent = list_match.group(1)
                content = list_match.group(2)
                cleaned = clean_text(content)
                if cleaned != content:
                    lines[i] = f"{indent}{cleaned}"
                    modified += 1

    # 写回文件
    filepath.write_text('\n'.join(lines), encoding='utf-8')
    return modified


def main():
    files = [
        Path("customer_service/templates.yaml"),
        Path("customer_service/shops/weimusi_templates.yaml"),
    ]

    for f in files:
        if not f.exists():
            print(f"⚠️ 文件不存在: {f}")
            continue
        modified = clean_yaml_file(f)
        print(f"✅ {f.name}: 修改了 {modified} 行")


if __name__ == "__main__":
    main()
