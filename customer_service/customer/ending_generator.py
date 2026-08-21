# -*- coding: utf-8 -*-
"""
对话结尾统一生成模块
每次AI回答完问题后，调用此模块生成结尾内容
结尾由三部分组成：[卖点补充] + [信息收集提问] + [转化引导]

架构位置：customer_service/customer/ending_generator.py

状态表（跟 session_id 绑定，存在内存/Redis 中）：
- collected_info: 已收集信息表（布尔值，哪些字段已经有了）
- delivered_points: 已传达卖点表（布尔值，哪些卖点说过了）
- wechat_push_count: 加微信推送次数（计数器）
- round_count: 当前对话轮次
"""

import random
import logging
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class EndingGenerator:
    """
    对话结尾生成器
    策略：卖点补充 + 信息收集 + 转化引导，按规则动态组合
    """

    # ========== 卖点列表 ==========
    SELLING_POINTS = {
        "brand_intro": {
            "name": "品牌介绍",
            "templates": [
                "对了，我们{shop_name}做定制家具{years_in_business}年了，老客户特别多，口碑一直不错~",
                "顺便说一下，我们是本地的老店，做了{years_in_business}年了，师傅都是有十几年经验的老师傅。",
            ],
            "relevance_topics": ["品牌", "口碑", "靠不靠谱", "怎么样"],
        },
        "zero_formaldehyde": {
            "name": "零甲醛",
            "templates": [
                "我们用的都是{eco_level}级别的板材，零甲醛无异味，装完就能住，家里有老人孩子也放心。",
                "环保这块您放心，我们的板材都是{eco_level}级的，装完不用晾，直接就能住。",
            ],
            "relevance_topics": ["环保", "甲醛", "味道", "孩子", "宝宝", "孕妇", "安全"],
        },
        "moisture_proof": {
            "name": "防潮耐用",
            "templates": [
                "而且我们的板材防潮性能特别好，厨房卫生间用个几十年都不会变形发霉。",
                "防潮这块您放心，南方梅雨天气也不怕，板材泡在水里都不会膨胀变形。",
            ],
            "relevance_topics": ["防潮", "防水", "厨房", "卫生间", "潮湿", "梅雨", "变形"],
        },
        "durable": {
            "name": "结实耐用",
            "templates": [
                "我们的柜子特别结实，站个人上去都没问题，用个二三十年跟新的一样。",
                "质量您放心，我们的柜体都是加厚板材，五金用的是{hardware_brand}的，特别耐用。",
            ],
            "relevance_topics": ["质量", "耐用", "结实", "用多久", "寿命"],
        },
        "laser_edge": {
            "name": "激光封边",
            "templates": [
                "我们用的是{edge_band}封边技术，封得特别严实，既美观又不容易开胶。",
                "封边这块用的是{edge_band}，摸上去跟一体的一样，不会有毛刺感。",
            ],
            "relevance_topics": ["封边", "工艺", "做工", "细节"],
        },
        "factory_direct": {
            "name": "工厂直营",
            "templates": [
                "我们是自己的工厂直营，没有中间商赚差价，所以价格特别实在。",
                "都是工厂直接出货，省去了店面和层层代理，性价比特别高。",
            ],
            "relevance_topics": ["价格", "便宜", "优惠", "性价比", "为什么这么便宜"],
        },
        "hardware_brand": {
            "name": "五金品牌",
            "templates": [
                "我们的五金配件用的都是{hardware_brand}的，开关门手感特别好，保用十几年都不会坏。",
                "五金这块您放心，用的是{hardware_brand}，大品牌，质量有保障。",
            ],
            "relevance_topics": ["五金", "配件", "铰链", "拉手", "滑道"],
        },
        "price_range": {
            "name": "价格区间",
            "templates": [
                "对了，我们不同板材价格不一样，从几百到一千多一平都有，看您选哪种材料。",
                "价格这块丰俭由人，经济型的几百块一平，好点的一千多，都能选。",
            ],
            "relevance_topics": ["价格", "多少钱", "报价", "预算"],
        },
        "free_design": {
            "name": "免费设计",
            "templates": [
                "对了，我们提供免费上门量房设计服务，设计师会根据您家的实际情况出方案和报价，都是免费的~",
                "我们可以免费上门量房，给您出个详细的设计方案和报价，您看看合不合适再说。",
            ],
            "relevance_topics": ["设计", "方案", "效果图", "量房"],
        },
        "factory_visit": {
            "name": "工厂参观",
            "templates": [
                "有空也可以来我们工厂看看，车间、样板间都有，材料工艺都能亲眼看到。",
                "我们工厂就在本地，随时欢迎您过来参观考察，眼见为实嘛~",
            ],
            "relevance_topics": ["工厂", "实地", "看看", "考察", "参观"],
        },
    }

    # ========== 信息收集提问模板 ==========
    INFO_QUESTIONS = {
        "scenes": [
            "对了，您家里主要是想做哪些柜子呀？",
            "想问一下，您主要想定制什么柜子呢？",
            "对了，您家里需要做哪些地方的柜子？",
        ],
        "area": [
            "大概有多少平方呀？",
            "面积大概多大呢？",
            "大概多少平方便说一下吗？",
        ],
        "preference": [
            "您更看重哪方面呢？是性价比、品质还是环保？",
            "想问一下，您这边更在意环保、价格还是颜值呀？",
            "对了，您对柜子有什么特别的偏好吗？比如看重环保还是性价比？",
        ],
        "pricing_method": [
            "对了，您之前了解过计价方式吗？有按投影面积算的，也有按展开面积算的。",
            "想问一下，您倾向于哪种计价方式？投影面积还是展开面积？",
        ],
        "community": [
            "对了，您是哪个小区的呀？说不定我们有做过同款户型~",
            "方便问一下您家在哪个小区吗？",
            "您家住哪个小区呀？看看我们有没有做过你们那儿的案例。",
        ],
        "decoration_progress": [
            "对了，您家现在装修到什么阶段了？",
            "想问一下，房子装修进度怎么样了？",
            "您家现在装到哪一步了呀？",
        ],
        "has_measurement": [
            "对了，您家里有没有量过房呀？",
            "想问一下，已经量过房了吗？",
            "房子量过了吗？",
        ],
        "phone": [
            "方便留个联系方式吗？我让设计师跟您对接一下具体细节。",
            "您的手机号方便说一下吗？我给您发一份详细的报价单。",
            "留个电话吧，我让我们设计师给您回过去详细聊聊？",
        ],
    }

    # ========== 加微信话术模板 ==========
    WECHAT_TEMPLATES = [
        "这样吧，我加您微信发详细报价单和效果图吧，您加的时候备注一下 **【{secret_code}】**，我好给您发对应的材料和报价~",
        "您加我微信吧，我发一些实景案例和详细报价给您参考，微信是 {wechat_id}，备注 **【{secret_code}】** 就行~",
        "要不加个微信聊？我给您发点我们做过的案例图和价格表，您备注 **【{secret_code}】** 我就知道是您了~",
        "方便加个微信吗？详细的方案和报价我微信发给您，您加的时候备注 **【{secret_code}】** 哈~",
    ]

    def __init__(self, config: Dict = None):
        """
        初始化结尾生成器

        Args:
            config: 店铺配置（用于渲染模板变量）
        """
        self.config = config or {}

    # ========== 主入口：生成结尾 ==========

    # ========== 卖点反相关关键词（用户说这些话时，某些卖点绝对不能推） ==========
    SELLING_POINT_ANTI_TOPICS = {
        "price_range": [
            # 用户问价格/优惠时，不推价格区间卖点（避免显得价格很乱）
            "优惠", "便宜", "多少钱", "价格", "砍价", "再少", "打个折",
            "能不能少", "便宜点", "优惠点", "能不能便宜",
        ],
        "factory_direct": [
            # 用户要档次/品质/高端时，不推工厂直营/性价比卖点（拉低档次）
            "档次", "品质", "高端", "好一点", "最好", "顶级", "奢华",
            "上档次", "有面子", "好的", "贵一点", "不差钱",
        ],
        "moisture_proof": [
            # 用户问环保/甲醛时，不推防潮卖点（用户关心的不是这个）
            "环保", "甲醛", "味", "E0", "enf", "孩子", "宝宝", "孕妇",
        ],
        "durable": [
            # 用户问价格/便宜时，不推耐用卖点（牛头不对马嘴）
            "便宜", "优惠", "多少钱", "砍价", "性价比高", "省钱",
        ],
    }

    def generate(
        self,
        current_answer_tag: str,
        user_message: str,
        original_answer: str = "",
        collected_info: Dict = None,
        delivered_points: Dict = None,
        wechat_push_count: int = 0,
        round_count: int = 0,
        secret_code: str = "",
        last_round_had_wechat: bool = False,
    ) -> Tuple[str, Dict, int, bool]:
        """
        生成对话结尾

        策略（修复2：每轮最多2个模块，优先级 信息收集 > 加微信 > 卖点）：
        - 第1优先级：信息收集提问（有未收集的就问1个）
        - 第2优先级：加微信引导（满足条件且推过<3次且上一轮没推）
        - 第3优先级：卖点补充（前两个加起来<2个的时候才补，宁缺毋滥）

        Args:
            current_answer_tag: 当前回答的话术标签（用于判断是否该加微信）
            user_message: 用户当前的消息（用于判断卖点相关性）
            original_answer: 引擎返回的原始回答（用于检测是否已有加微信/信息提问，避免重复）
            collected_info: 已收集信息表 {field: 值}
            delivered_points: 已传达卖点表 {point_key: True/False}
            wechat_push_count: 已推送加微信的次数
            round_count: 当前对话轮次
            secret_code: 客户暗号
            last_round_had_wechat: 上一轮有没有推加微信（用于控制不连续推）

        Returns:
            (结尾文本, 更新后的delivered_points, 更新后的wechat_push_count, 本轮是否推了加微信)
        """
        ending_parts = []
        new_delivered = dict(delivered_points) if delivered_points else {}
        new_wechat_count = wechat_push_count
        this_round_had_wechat = False

        # 兼容旧调用：collected_info 为 None 时用空 dict
        if collected_info is None:
            collected_info = {}

        # 检测原回答里是否已经有加微信内容
        has_wechat_in_answer = self._has_wechat_content(original_answer)

        # 原回答里已经有加微信 → 结尾模块绝对不追加，连计数都不增加
        # （修复3：严格去重，原回答有就完全不插手）
        wechat_blocked = has_wechat_in_answer

        # ===== 第1优先级：信息收集提问 =====
        info_question = self._pick_info_question(collected_info, original_answer)
        if info_question:
            ending_parts.append(info_question)

        # ===== 第2优先级：加微信引导 =====
        if not wechat_blocked:
            wechat_text, should_push = self._should_push_wechat(
                current_answer_tag, user_message, collected_info,
                wechat_push_count, round_count, last_round_had_wechat
            )
            if should_push and wechat_text:
                ending_parts.append(wechat_text.format(
                    secret_code=secret_code,
                    wechat_id=self.config.get("wechat_id", ""),
                ))
                new_wechat_count += 1
                this_round_had_wechat = True

        # ===== 第3优先级：卖点补充（前两个加起来 < 2个的时候才补） =====
        # 宁缺毋滥：不相关的宁愿不推，也不乱推
        if len(ending_parts) < 2:
            # 卖点补充频率：每2-3轮补一次，不是每轮都补
            should_add_point = round_count >= 2 and (round_count % random.randint(2, 3) == 0)
            # 如果前面两个模块一个都没触发，哪怕没到频率也补一个（别太空了）
            if len(ending_parts) == 0:
                should_add_point = True

            if should_add_point:
                selling_point_text, point_key = self._pick_selling_point(
                    user_message, new_delivered, current_answer_tag
                )
                if selling_point_text:
                    ending_parts.append(selling_point_text)
                    new_delivered[point_key] = True

        # 保险：最终再检查一遍模块数量，确保 <= 2（修复2：控量硬限制）
        if len(ending_parts) > 2:
            ending_parts = ending_parts[:2]

        # 拼接结尾
        ending = "\n".join(ending_parts) if ending_parts else ""
        return ending, new_delivered, new_wechat_count, this_round_had_wechat

    # ========== 卖点补充逻辑 ==========

    def _pick_selling_point(
        self,
        user_message: str,
        delivered_points: Dict,
        current_answer_tag: str = "",
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        挑选一个还没说过的相关卖点
        先过滤反相关的，再从相关的里挑，都没有就不推（宁缺毋滥）

        Args:
            user_message: 用户消息（用于判断相关性）
            delivered_points: 已传达的卖点
            current_answer_tag: 当前回答标签（议价等场景下过滤价格类卖点）

        Returns:
            (卖点文本, 卖点key)，没有合适的返回 (None, None)
        """
        # 找出还没说过的卖点
        undelivered = [
            key for key in self.SELLING_POINTS.keys()
            if not delivered_points.get(key, False)
        ]
        if not undelivered:
            return None, None

        # ===== 修复4：反相关过滤（宁缺毋滥） =====
        # 先过滤掉不能推的卖点
        allowed = []
        for key in undelivered:
            anti_topics = self.SELLING_POINT_ANTI_TOPICS.get(key, [])
            if anti_topics and any(topic in user_message for topic in anti_topics):
                continue  # 反相关，跳过
            # 议价状态机里（在谈价格了），不推任何价格类卖点
            bargain_tags = ["bargain", "议价", "砍价", "还价"]
            if key in ("price_range", "factory_direct") and any(
                t in current_answer_tag.lower() for t in bargain_tags
            ):
                continue
            allowed.append(key)

        if not allowed:
            return None, None

        # 先挑跟当前问题正相关的
        relevant = []
        for key in allowed:
            point = self.SELLING_POINTS[key]
            if any(topic in user_message for topic in point["relevance_topics"]):
                relevant.append(key)

        # 有正相关的从相关里选，没有就从允许的里面随机选
        candidates = relevant if relevant else allowed
        if not candidates:
            return None, None

        chosen_key = random.choice(candidates)
        point = self.SELLING_POINTS[chosen_key]
        template = random.choice(point["templates"])

        # 渲染模板变量
        text = template.format(
            shop_name=self.config.get("shop_name", ""),
            years_in_business=self.config.get("years_in_business", ""),
            eco_level=self.config.get("eco_level", ""),
            edge_band=self.config.get("edge_band", ""),
            hardware_brand=self.config.get("hardware_brand", ""),
        )

        return text, chosen_key

    # ========== 信息收集提问逻辑 ==========

    # 每个字段对应的提问关键词（用于检测原回答里有没有已经问过了）
    FIELD_QUESTION_KEYWORDS = {
        "scenes": ["哪些柜子", "做什么柜", "做哪些", "什么柜子", "想定制", "要做", "需要做"],
        "area": ["多少平", "多大", "面积", "几平", "几个平方", "多少平方"],
        "preference": ["看重哪方面", "更在意", "偏好", "看重什么", "重视什么"],
        "pricing_method": ["计价方式", "投影面积", "展开面积", "按投影", "按展开"],
        "community": ["哪个小区", "什么小区", "哪个小区的", "小区叫", "哪的", "哪里的"],
        "decoration_progress": ["装修到什么", "装修进度", "装到哪一步", "装修怎么样", "装修到哪"],
        "has_measurement": ["量过房", "量房了", "有没有量房", "量房了吗"],
        "phone": ["联系方式", "电话", "手机号", "留个电话", "留个联系", "留个手机号"],
    }

    def _pick_info_question(self, collected_info: Dict, original_answer: str = "") -> Optional[str]:
        """
        按优先级挑一个还没收集的信息来问
        同时检测原回答里有没有已经问过了，避免重复

        Args:
            collected_info: 已收集信息表（值为非None/非空表示已收集）
            original_answer: 原回答文本（用于检测是否已经问过）

        Returns:
            提问文本，没的问了返回 None
        """
        from .info_extractor import InfoExtractor

        for field in InfoExtractor.FIELD_PRIORITY:
            value = collected_info.get(field)
            # 值为 None、空字符串、空列表都算没收集
            if value is None or value == "" or value == []:
                # 检查原回答里有没有已经在问这个字段
                if original_answer:
                    keywords = self.FIELD_QUESTION_KEYWORDS.get(field, [])
                    if keywords and any(kw in original_answer for kw in keywords):
                        continue  # 原回答已经问过了，跳过
                questions = self.INFO_QUESTIONS.get(field)
                if questions:
                    return random.choice(questions)

        return None

    # ========== 加微信检测 ==========

    def _has_wechat_content(self, text: str) -> bool:
        """
        检测文本中是否已经包含加微信相关的内容
        用于判断引擎原回答里有没有已经推过加微信，避免重复

        Args:
            text: 待检测文本

        Returns:
            True 表示已有加微信内容
        """
        wechat_keywords = [
            "加微信", "加我微信", "加个微信", "加您微信",
            "微信我", "微信吧", "微信聊", "微信联系",
            "微信发", "微信是", "微信号",
            "加我吧", "加个吧", "方便加",
        ]
        return any(kw in text for kw in wechat_keywords)

    # ========== 加微信推送逻辑 ==========

    def _should_push_wechat(
        self,
        current_answer_tag: str,
        user_message: str,
        collected_info: Dict,
        wechat_push_count: int,
        round_count: int,
        last_round_had_wechat: bool = False,
    ) -> Tuple[Optional[str], bool]:
        """
        判断是否应该推送加微信

        策略：
        - 前2轮对话不提加微信
        - 回答完价格/材料核心问题后，可以提1次
        - 收集到核心信息（场景+面积）后，可以提1次
        - 客户说"考虑考虑""再想想"，一定要提
        - 同一场对话加微信推送不超过3次
        - 相邻两轮不能连续推（修复3：至少隔1轮）

        Returns:
            (话术模板, 是否应该推送)
        """
        # 超过3次就不再推了
        if wechat_push_count >= 3:
            return None, False

        # 上一轮刚推过，这一轮不推（修复3：不连续轰炸）
        if last_round_had_wechat:
            return None, False

        # 前2轮不提
        if round_count <= 2:
            return None, False

        # 情况1：客户说"考虑考虑""再想想"，一定要提
        hesitate_keywords = [
            "考虑考虑", "再想想", "再看看", "先看看", "我想想",
            "考虑一下", "琢磨琢磨", "商量商量", "回去想想",
            "对比一下", "比较一下", "了解了解", "先了解一下",
        ]
        if any(kw in user_message for kw in hesitate_keywords):
            return random.choice(self.WECHAT_TEMPLATES), True

        # 情况2：回答完价格/材料核心问题后
        price_material_tags = [
            "price", "material", "bargain", "报价", "价格", "材料",
        ]
        if any(tag in current_answer_tag.lower() for tag in price_material_tags):
            # 至少推过1次后就不再因为这个原因推了
            if wechat_push_count < 1:
                return random.choice(self.WECHAT_TEMPLATES), True

        # 情况3：收集到核心信息（场景+面积都有了）
        has_scenes = bool(collected_info.get("scenes"))
        has_area = bool(collected_info.get("area"))
        if has_scenes and has_area:
            # 核心信息收集完后可以推第2次
            if wechat_push_count < 2:
                return random.choice(self.WECHAT_TEMPLATES), True

        # 情况4：对话比较多了（>=5轮），但还没推过，推一次
        if round_count >= 5 and wechat_push_count < 1:
            return random.choice(self.WECHAT_TEMPLATES), True

        return None, False
