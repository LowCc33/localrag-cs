# -*- coding: utf-8 -*-
"""
回答清洗器
从引擎返回的核心回答中，移除加微信话术和信息收集类提问
让结尾统一由 ending_generator 管控

架构位置：customer_service/customer/answer_cleaner.py

为什么不用修改模板的方式：
- 模板文件多（2个）、数量大（~2000行）
- 手动清理容易误删核心答案
- 脚本清理容易误伤特殊格式（jinja2变量、yaml结构）
- 清洗逻辑集中在一处，好调试、可开关、可随时回退
"""

import re
import logging

logger = logging.getLogger(__name__)


class AnswerCleaner:
    """
    回答清洗器
    从引擎返回的回答中过滤掉：
    1. 加微信相关话术
    2. 信息收集类提问
    只保留核心答案内容
    """

    # ===== 加微信相关关键词（句子里包含这些就删） =====
    WECHAT_KEYWORDS = [
        "加微信", "加我微信", "加个微信", "加您微信", "加她微信",
        "微信我", "微信吧", "微信聊", "微信联系", "微信发",
        "微信是", "微信号", "加我吧", "加个吧", "方便加",
        "微信发给", "微信给您", "加您微信",
        "我加您", "您加我", "加个微信",
        "留个微信", "加我微信", "加我发",
    ]

    # 微信ID相关正则（直接匹配"微信是 1xxx" / "微信号: xxx"之类的整句）
    WECHAT_ID_PATTERNS = [
        re.compile(r'微信[是号为:： ]{0,3}1[3-9]\d{9}'),
        re.compile(r'微信[是号为:： ]{0,3}[a-zA-Z][a-zA-Z0-9_-]{5,}'),
    ]

    # ===== 信息收集提问关键词（问句里包含这些就删） =====
    INFO_QUESTION_KEYWORDS = [
        # 场景类
        "哪些柜子", "做什么柜", "做哪些柜", "什么柜子",
        "想定制什么", "想做什么柜", "要做什么柜", "需要做哪些",
        "主要想做", "想做些什么", "做些什么柜子",
        # 面积类
        "多少平", "多大面积", "面积多大", "几平",
        "几个平方", "多少平方", "做多大", "大概多大",
        "面积大概", "多大呀", "多大呢", "多少平方便",
        # 偏好类
        "看重哪方面", "更在意", "更看重", "看重什么",
        "重视什么", "偏好", "更倾向于",
        # 计价方式
        "计价方式", "投影面积算", "展开面积算",
        "按投影", "按展开", "倾向于哪种",
        # 小区类
        "哪个小区", "什么小区", "哪个小区的", "小区叫",
        "哪的房子", "哪里的", "住哪个", "在哪小区",
        # 装修进度
        "装修到什么", "装修进度", "装到哪一步", "装修怎么样",
        "装修到哪", "装到什么", "房子装修", "装修阶段",
        # 量房
        "量过房", "量房了", "有没有量房", "量房了吗",
        "量房没", "量过房了吗", "已经量房",
        # 联系方式
        "联系方式", "留个电话", "留个联系", "留个手机号",
        "电话方便", "手机号方便", "留个微信", "您电话是",
        "您的电话", "方便说一下电话", "留个电话吧",
        "留个联系方式", "留个联系电话",
        # 称呼
        "怎么称呼", "您贵姓", "您怎么称呼", "您叫什么",
    ]

    @classmethod
    def clean(cls, text: str) -> str:
        """
        清洗回答文本：移除加微信话术和信息收集提问

        Args:
            text: 原始回答

        Returns:
            清洗后的回答
        """
        if not text:
            return text

        # 按句子拆分（中文句号、问号、感叹号、换行作为分隔）
        # 保留标点符号用于后续拼接
        sentences = re.split(r'([。？！\.!?\n；;])', text)

        result = []
        skip_next_punct = False

        for i, part in enumerate(sentences):
            if skip_next_punct:
                skip_next_punct = False
                continue

            # 标点符号：看前一个句子保留没有，保留就带标点
            if part in '。？！.！?\n；;':
                if result and result[-1]:  # 前面有内容，带上标点
                    result.append(part)
                continue

            sentence = part.strip()
            if not sentence:
                result.append(part)
                continue

            # 检查1：是不是加微信话术
            if cls._is_wechat_sentence(sentence):
                skip_next_punct = True
                continue

            # 检查2：是不是信息收集类提问
            if cls._is_info_question(sentence):
                skip_next_punct = True
                continue

            result.append(part)

        # 拼接结果
        cleaned = "".join(result)

        # 二次清理：修正一些因为删句子导致的不通顺
        cleaned = cls._post_clean(cleaned)

        return cleaned

    @classmethod
    def _is_wechat_sentence(cls, sentence: str) -> bool:
        """判断一句话是不是加微信相关的"""
        s = sentence.strip()
        if not s:
            return False

        # 关键词匹配
        for kw in cls.WECHAT_KEYWORDS:
            if kw in s:
                return True

        # 微信ID正则匹配
        for pattern in cls.WECHAT_ID_PATTERNS:
            if pattern.search(s):
                return True

        return False

    @classmethod
    def _is_info_question(cls, sentence: str) -> bool:
        """判断一句话是不是信息收集类提问"""
        s = sentence.strip()
        if not s:
            return False

        # 是否是问句：有问号，或以"吗/呀/呢"结尾（包括后面带问号的情况）
        has_question = (
            "？" in s
            or "?" in s
            or s.rstrip("？?。. ").endswith("吗")
            or s.rstrip("？?。. ").endswith("呀")
            or s.rstrip("？?。. ").endswith("呢")
        )

        if not has_question:
            return False

        # 关键词匹配
        for kw in cls.INFO_QUESTION_KEYWORDS:
            if kw in s:
                return True

        return False

    @classmethod
    def _post_clean(cls, text: str) -> str:
        """
        二次清理：修正删句子后产生的小问题
        """
        if not text:
            return text

        # 1. 多个空行合并成最多2个
        text = re.sub(r'\n{3,}', '\n\n', text)

        # 2. 行首的"对了"、"顺便说一下"之类的连接词，如果前面已经删了句子导致突兀，保留也没关系
        #    （这些词本身无害，只是衔接用的）

        # 3. 行尾多余的空格
        lines = text.split('\n')
        lines = [line.rstrip() for line in lines]
        text = '\n'.join(lines)

        # 4. 去掉连续的句号、逗号等
        text = re.sub(r'[，,]{2,}', '，', text)
        text = re.sub(r'[。.]{2,}', '。', text)

        # 5. 去掉开头的连接词如果孤零零的（前面的句子被删了）
        # 比如 "对了，您看这样行吗？" → 前面的句子被删了，"对了"还在 → 保留也行，不影响理解

        # 6. 去掉首尾空白
        text = text.strip()

        return text
