# -*- coding: utf-8 -*-
"""
客户信息提取器
从用户对话中提取结构化信息，更新客户档案
MVP 阶段：关键词匹配 + 简单规则，覆盖 80% 常见场景
架构位置：customer_service/customer/info_extractor.py

字段映射（任务文件 → 实际可提取内容）：
- scenes: 使用场景/做哪些柜子
- area: 面积大小
- preference: 偏好（性价比/品质/环保/颜值）
- pricing_method: 计价方式（投影/展开）
- phone: 手机号
- community: 小区/位置
- decoration_progress: 装修进度
- has_measurement: 有没有量房
"""

import re
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class InfoExtractor:
    """
    客户信息提取器
    从用户消息中提取结构化字段
    """

    # 信息字段优先级（决定先问什么）
    FIELD_PRIORITY = [
        "scenes",           # 使用场景/做哪些柜子（最高优先级）
        "area",             # 面积大小
        "preference",       # 偏好
        "pricing_method",   # 计价方式
        "community",        # 小区/位置
        "decoration_progress",  # 装修进度
        "has_measurement",  # 有没有量房
        "phone",            # 手机号（最后问）
    ]

    # 场景关键词映射
    SCENES_KEYWORDS = {
        "衣柜": ["衣柜", "衣橱", "衣帽间", "主卧柜", "次卧柜"],
        "橱柜": ["橱柜", "厨柜", "厨房柜", "厨房柜子"],
        "鞋柜": ["鞋柜", "鞋架"],
        "酒柜": ["酒柜"],
        "书柜": ["书柜", "书架"],
        "电视柜": ["电视柜", "背景墙柜"],
        "榻榻米": ["榻榻米", "地台"],
        "玄关柜": ["玄关柜", "入户柜", "进门柜"],
        "阳台柜": ["阳台柜", "洗衣柜"],
        "全屋": ["全屋定制", "整屋", "全套", "家里全部", "整套"],
    }

    # 偏好关键词映射
    PREFERENCE_KEYWORDS = {
        "性价比": ["便宜", "实惠", "性价比", "预算有限", "省钱", "划算", "经济"],
        "品质": ["品质", "质量好", "耐用", "结实", "高端", "上档次", "好一点"],
        "环保": ["环保", "零甲醛", "无甲醛", "E0", "enf", "孩子", "宝宝", "孕妇"],
        "颜值": ["好看", "颜值", "美观", "漂亮", "设计感", "好看", "高级感", "风格"],
    }

    # 计价方式关键词
    PRICING_KEYWORDS = {
        "projection": ["投影面积", "投影", "按投影", "投影算"],
        "expand": ["展开面积", "展开", "按展开", "展开算"],
    }

    # 装修进度关键词
    DECORATION_PROGRESS_KEYWORDS = {
        "未开始": ["还没装", "刚开始", "准备装", "打算装", "还没开始", "毛坯"],
        "水电阶段": ["水电", "改水电", "做水电"],
        "瓦工阶段": ["贴砖", "瓦工", "铺砖", "泥工"],
        "木工阶段": ["木工", "吊顶", "打柜子"],
        "油工阶段": ["刷漆", "油工", "腻子"],
        "安装阶段": ["安装", "装完了", "快装完了", "收尾"],
        "已入住": ["入住了", "已经住了", "搬进去了"],
    }

    @classmethod
    def extract(cls, text: str, current_info: Dict = None) -> Dict:
        """
        从用户消息中提取信息

        Args:
            text: 用户消息文本
            current_info: 当前已收集的信息（用于判断是否需要更新）

        Returns:
            提取到的新信息字典 {field: value}
        """
        if current_info is None:
            current_info = {}

        new_info = {}

        # 1. 场景/柜子类型
        scenes = cls._extract_scenes(text)
        if scenes:
            # 合并已有场景（去重）
            existing = current_info.get("scenes", "")
            if existing:
                existing_list = [s.strip() for s in existing.split(",") if s.strip()]
                for s in scenes:
                    if s not in existing_list:
                        existing_list.append(s)
                new_scenes = ",".join(existing_list)
            else:
                new_scenes = ",".join(scenes)
            if new_scenes != existing:
                new_info["scenes"] = new_scenes

        # 2. 面积
        area = cls._extract_area(text)
        if area and current_info.get("area") != area:
            new_info["area"] = area

        # 3. 偏好
        preference = cls._extract_preference(text)
        if preference and current_info.get("preference") != preference:
            new_info["preference"] = preference

        # 4. 计价方式
        pricing = cls._extract_pricing_method(text)
        if pricing and current_info.get("pricing_method") != pricing:
            new_info["pricing_method"] = pricing

        # 5. 手机号
        phone = cls._extract_phone(text)
        if phone and current_info.get("phone") != phone:
            new_info["phone"] = phone

        # 6. 小区
        community = cls._extract_community(text)
        if community and current_info.get("community") != community:
            new_info["community"] = community

        # 7. 装修进度
        progress = cls._extract_decoration_progress(text)
        if progress and current_info.get("decoration_progress") != progress:
            new_info["decoration_progress"] = progress

        # 8. 有没有量房
        has_measurement = cls._extract_has_measurement(text)
        if has_measurement is not None:
            current_val = current_info.get("has_measurement")
            if has_measurement != current_val:
                new_info["has_measurement"] = has_measurement

        return new_info

    @classmethod
    def _extract_scenes(cls, text: str) -> list:
        """提取使用场景/柜子类型"""
        found = []
        for scene, keywords in cls.SCENES_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                found.append(scene)
        return found

    @classmethod
    def _extract_area(cls, text: str) -> Optional[str]:
        """提取面积"""
        # 模糊范围：7-8平 → 取平均值
        fuzzy_area = re.search(
            r'(\d+)(?:\s+|[-~到至])(\d+)\s*(?:个)?\s*(?:平方|平米|平|㎡)',
            text
        )
        if fuzzy_area:
            low = float(fuzzy_area.group(1))
            high = float(fuzzy_area.group(2))
            avg = round((low + high) / 2, 1)
            return f"{avg}平"

        # 精确数字 + 单位
        area_match = re.search(
            r'(\d+(?:\.\d+)?)\s*(?:个)?\s*(?:平方|平米|平|㎡)',
            text
        )
        if area_match:
            return f"{area_match.group(1)}平"

        return None

    @classmethod
    def _extract_preference(cls, text: str) -> Optional[str]:
        """提取偏好类型"""
        for pref, keywords in cls.PREFERENCE_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                return pref
        return None

    @classmethod
    def _extract_pricing_method(cls, text: str) -> Optional[str]:
        """提取计价方式"""
        for method, keywords in cls.PRICING_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                return method
        return None

    @classmethod
    def _extract_phone(cls, text: str) -> Optional[str]:
        """提取手机号"""
        phone_match = re.search(r'(?<!\d)1[3-9]\d{9}(?!\d)', text)
        if phone_match:
            return phone_match.group(0)
        return None

    @classmethod
    def _extract_community(cls, text: str) -> Optional[str]:
        """提取小区名称"""
        community_suffixes = [
            "小区", "花园", "苑", "府", "城", "园", "里", "邨", "湾",
            "郡", "公寓", "家园", "华庭", "名邸", "山庄", "大厦",
        ]
        for suffix in community_suffixes:
            if suffix in text:
                idx = text.index(suffix)
                # 从后缀往前找最近的2-10个汉字/字母/数字
                start = idx
                while start > 0 and (
                    text[start-1].isalnum()
                    or '\u4e00' <= text[start-1] <= '\u9fa5'
                ):
                    start -= 1
                comm_name = text[start:idx + len(suffix)]

                # 过滤掉明显的前缀词
                bad_prefixes = [
                    "我家在", "你家在", "他家在", "在", "是", "有",
                    "去", "到", "我", "你", "他", "这个", "那个",
                    "你们", "我们", "你家", "我家", "他家",
                    "叫", "叫什么", "什么", "哪个",
                ]
                for prefix in bad_prefixes:
                    if comm_name.startswith(prefix):
                        comm_name = comm_name[len(prefix):]
                        break

                if len(comm_name) >= 3:
                    return comm_name
                break

        return None

    @classmethod
    def _extract_decoration_progress(cls, text: str) -> Optional[str]:
        """提取装修进度"""
        for progress, keywords in cls.DECORATION_PROGRESS_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                return progress
        return None

    @classmethod
    def _extract_has_measurement(cls, text: str) -> Optional[int]:
        """提取是否量房"""
        # 明确说量过了
        yes_keywords = [
            "量过了", "已经量了", "量房了", "量过房了", "来量过了",
            "已经量过", "量完了",
        ]
        if any(kw in text for kw in yes_keywords):
            return 1

        # 明确说没量过
        no_keywords = [
            "没量过", "还没量", "没有量", "还没量房", "没量房",
            "没量呢", "还没量呢",
        ]
        if any(kw in text for kw in no_keywords):
            return 0

        # 提出要量房（暗示还没量，但不确定，不提取）
        return None
