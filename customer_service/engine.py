# -*- coding: utf-8 -*-
"""
全屋定制客服引擎 v2.0（话术体系扩展版）
职责：高频问题关键词命中 → 工艺能力判断 → LLM四分类意图识别（兜底）
      → 模板随机选取 → 变量填充（卖点/钩子/让利/紧迫感随机抽取）
      → 议价多轮状态机 → 上下文记忆（最多 3 轮）
      → 软收尾触发 + 对话结束判断
架构位置：独立 MVP，不依赖 LocalRAG-CS，纯命令行调用
v2.0 新增：
    - shop_config 扩展：工艺能力/付款方式/工期/品牌信任
    - hot_questions 大扩展：8大类50+场景约100个模板
    - 工艺能力判断：匹配工艺关键词 → 查配置 → 走能做/不能做话术
    - 议价状态机：摸底 → 分档（小/中/大） → 升级
"""

import os
import random
import json
import requests
import yaml
from jinja2 import Template

# 火山引擎 DeepSeek 配置（抄自 LocalRAG-CS config.py）
API_URL = os.getenv("DEEPSEEK_API_URL", "https://ark.cn-beijing.volces.com/api/v3/chat/completions")
API_KEY = os.getenv("DEEPSEEK_API_KEY", "ark-8c848111-eaee-49f1-8d7c-66a2ba64d6f1-b38c9")
MODEL = os.getenv("DEEPSEEK_MODEL", "ep-20260630143620-87j6b")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 商家配置加载器（支持多商家私有配置）
from customer_service.shop_config_loader import (
    load_shop_config, get_price_range, get_materials_list_text,
    get_material_price, get_material_name, get_material_brand
)


class CustomerServiceEngine:
    """客服引擎 v2.0"""

    def __init__(self, shop_id: str = None):
        """初始化：加载模板库与商家配置，准备上下文与计数器

        Args:
            shop_id: 商家ID，不传则使用默认配置（向后兼容）
        """
        self.shop_id = shop_id
        # 1) 加载公用模板库
        with open(os.path.join(BASE_DIR, "templates.yaml"), encoding="utf-8") as f:
            self.templates = yaml.safe_load(f) or {}
        # 2) 加载公用推荐矩阵
        with open(os.path.join(BASE_DIR, "recommendation_matrix.yaml"), encoding="utf-8") as f:
            self.rec_matrix = yaml.safe_load(f) or {}
        # 3) 商家私有模板覆盖（如果有 shop_id 且对应文件存在）
        self.private_rec_matrix = None  # 先初始化，确保属性一定存在
        if shop_id:
            self._apply_shop_templates(shop_id)
            self._apply_shop_rec_matrix(shop_id)
            self._load_private_rec_matrix(shop_id)
        # 4) 根据 shop_id 加载对应商家配置（None 则用默认配置）
        self.config = load_shop_config(shop_id)
        self.history = []                # 上下文记忆，最多留 3 轮
        self.chat_streak = 0             # 连续闲扯计数器（软收尾用）
        self.end_streak = 0              # 连续结束语计数器（对话结束判断用）
        self.bargain_step = 0            # 议价状态机：0=未开始 1=报区间 2=报实价 3=摸底 4=已优惠 5+=升级
        self.bargain_pullback_count = 0  # 拉回正题计数器，连续2轮不正面回答就引导加微信
        self.selected_material = None    # 当前选中/推荐的材料key（Step2及以后有值）
        self.llm_fallback_streak = 0     # LLM兜底连续次数（盲区检测用）
        # —— 已收集用户信息（去重用）——
        # 只增不改原则：置信度够才存，宁可不存也不乱存；用户明确改口则覆盖
        self.collected_info = {
            "name": None,          # 姓名
            "phone": None,         # 电话
            "wechat": None,        # 微信
            "contact": None,       # 通用联系方式（分不清电话还是微信时用）
            "cabinet_type": None,  # 柜子类型：衣柜/橱柜/全屋等
            "style_preference": None,  # 风格偏好：现代/北欧/中式等
            "material": None,      # 材质偏好：颗粒板/多层板/欧松板等
            "area": None,          # 面积（数字，单位：平方）
            "budget": None,        # 预算
            "community": None,     # 小区
            "city": None,          # 城市
        }

    # ---------- 通用辅助：抽取变量 + 渲染模板 ----------
    def _vars(self):
        """
        每次渲染前组装变量：硬参数 + 嵌套配置对象 + 4个随机抽取池
        嵌套对象直接传入，模板里用点号访问（如 {{payment_terms.deposit}}）
        """
        # 工期动态计算（懒加载，算一次就够了）
        if not hasattr(self, "_cached_delivery_days"):
            self._cached_delivery_days = self._calc_delivery_days()
        days = self._cached_delivery_days

        return {
            # —— 店铺基础信息 ——
            "shop_name": self.config["shop_name"],
            "boss_name": self.config["boss_name"],
            "service_name": self._get_service_name(),
            "years_in_business": self.config["years_in_business"],
            "city": self.config["city"],
            "shop_location": self.config["shop_location"],
            "wechat_id": self.config["wechat_id"],
            # —— 产品硬参数 ——
            "board_brand": self.config["board_brand"],
            "edge_band": self.config["edge_band"],
            "hardware_brand": self.config["hardware_brand"],
            "eco_level": self.config["eco_level"],
            "warranty_years": self.config.get("warranty_years", "5"),
            # —— 嵌套配置对象（模板里用点号访问） ——
            "process_capability": self.config.get("process_capability", {}),
            "payment_terms": self.config.get("payment_terms", {}),
            "production_cycle": self.config.get("production_cycle", {}),
            "trust_points": self.config.get("trust_points", {}),
            "pricing": self.config.get("_pricing", {}),
            # —— 随机抽取池（每次渲染抽一个） ——
            "concessions": random.choice(self.config.get("concessions", [""])),
            "urgency": random.choice(self.config.get("urgency_factors", [""])),
            "selling_point": random.choice(self.config.get("selling_points", [""])),
            # —— 留资钩子（先渲染钩子模板里的 {{wechat_id}}，再传入外层模板） ——
            "hook": self._render_lead_hook(),
            # —— 兼容旧模板的 warranty 字段 ——
            "warranty": self.config.get("warranty", ""),
            # —— 工期动态计算（默认估算，任何模板都能安全用，不会空值）——
            "total_days": str(days["total"]),
            "design_days": str(days["design"]),
            "production_days": str(days["production"]),
            "install_days": str(days["install"]),
        }

    def _get_material_selling_point(self, material_key, scene_name=""):
        """
        生成材料的推荐理由（安全可控，从配置和矩阵里取，不用LLM）
        策略：
        1. 先从推荐矩阵里找该场景+recommend_me的理由（如果材料对得上）
        2. 找不到就用通用卖点（零甲醛+防潮耐用等）
        """
        matrix = self.rec_matrix.get("matrix", {})
        
        # 先尝试从推荐矩阵的recommend_me里找
        for scene_key, scene_data in matrix.items():
            rec_me = scene_data.get("recommend_me", {})
            if isinstance(rec_me, dict) and rec_me.get("material") == material_key:
                reason = rec_me.get("reason", "")
                if reason:
                    return reason
        
        # 兜底：从材料名和通用卖点拼
        name_map = self.config.get("_board_name_map", {})
        mat_name = name_map.get(material_key, "这款材料")
        
        # 场景化卖点（厨房/卫生间→防潮，卧室→环保，儿童房→零甲醛）
        scene = scene_name or ""
        if any(s in scene for s in ["厨房", "卫生间", "阳台", "厨卫"]):
            scene_selling = "防水防潮不发霉，用几十年都不会变形"
        elif any(s in scene for s in ["儿童", "孩子", "宝宝"]):
            scene_selling = "零甲醛无异味，装完就能住，孩子住着放心"
        elif any(s in scene for s in ["卧室", "衣柜", "主卧", "次卧"]):
            scene_selling = "零甲醛环保，天天待着也放心"
        else:
            scene_selling = "零甲醛环保，耐用不变形"
        
        return f"{mat_name}{scene_selling}"

    def _render_lead_hook(self):
        """渲染单个留资钩子（钩子模板里也有 {{wechat_id}} 需要先渲染）"""
        hook_template = random.choice(self.config.get("lead_hooks", [""]))
        # 钩子模板里只有 wechat_id 变量，直接替换
        return hook_template.replace("{{wechat_id}}", self.config.get("wechat_id", ""))

    def _get_service_name(self):
        """
        获取客服称呼
        - 优先用配置里的 customer_service_name 字段
        - 没有就用"小+老板名第一个字"（如老板叫宋姐→小宋）
        - 都没有就返回"客服"
        """
        name = self.config.get("customer_service_name", "")
        if name:
            return name
        boss_name = self.config.get("boss_name", "")
        if boss_name:
            return f"小{boss_name[0]}"
        return "客服"

    # ---------- 商家私有模板覆盖 ----------
    def _apply_shop_templates(self, shop_id: str):
        """
        加载商家私有模板并覆盖到公用模板上（商家优先级更高）
        支持两种格式：
        1) 与公用模板同结构（顶层bargain/complain/hot_questions等）→ 直接覆盖
        2) 商家自定义格式（price_inquiry_templates等模块名）→ 自动映射到对应hot_questions category
        商家没有的键，继续用公用的
        """
        shop_tpl_path = os.path.join(BASE_DIR, "shops", f"{shop_id}_templates.yaml")
        if not os.path.exists(shop_tpl_path):
            return
        try:
            with open(shop_tpl_path, encoding="utf-8") as f:
                shop_templates = yaml.safe_load(f) or {}
        except Exception as e:
            print(f"[模板加载] 商家{shop_id}模板加载失败: {e}")
            return

        # 商家模板 → 公用模板hot_questions category 的映射表
        # key: 商家模板里的字段名, value: 对应hot_questions的category
        category_mapping = {
            "price_inquiry_templates": "bargain_price_range",
            "material_price_templates": "bargain_material_price",
            "material_list_templates": "bargain_material_list",
            "material_compare_templates": "material_compare",
            "value_building_templates": "bargain_value_build",
            "aluminum_vs_wood_templates": "aluminum_vs_wood",
            "eco_zero_formaldehyde_templates": "eco_level",
            "shop_info_templates": "shop_info",
            "price_probe_templates": "bargain_probe",
            "kitchen_recommend_templates": "material_recommend_kitchen",
            "bathroom_recommend_templates": "material_recommend_bathroom",
            "bedroom_recommend_templates": "material_recommend_bedroom",
            "kids_room_recommend_templates": "material_recommend_kids_room",
            "shoe_cabinet_recommend_templates": "material_recommend_shoe_cabinet",
            "tatami_recommend_templates": "material_recommend_tatami",
        }

        # 收集要覆盖到 hot_questions 的条目
        shop_hot_overrides = {}  # category -> templates列表

        # 遍历商家模板所有顶层键
        for key, value in shop_templates.items():
            if key == "recommendation_matrix":
                # 推荐矩阵单独处理（在 _apply_shop_rec_matrix 里）
                continue
            elif key == "hot_questions":
                # 直接给hot_questions列表的，走原来的合并逻辑
                self._merge_hot_questions(value)
                continue
            elif key in ["bargain", "complain", "consult", "chat", "unknown_question",
                         "confirm_yes_templates", "confirm_no_templates"]:
                # 跟公用模板顶层同名的，直接覆盖
                self.templates[key] = value
            elif key in category_mapping:
                # 商家自定义模块名 → 映射到对应hot_questions category
                cat = category_mapping[key]
                if isinstance(value, list) and value:
                    shop_hot_overrides[cat] = value
            # 其他key暂时忽略

        # 应用 hot_questions 覆盖
        if shop_hot_overrides:
            self._override_hot_question_templates(shop_hot_overrides)

        print(f"[模板加载] 已应用商家 {shop_id} 私有模板")

    def _override_hot_question_templates(self, overrides: dict):
        """
        覆盖hot_questions中指定category的templates列表
        overrides: {category_name: [new_templates_list]}
        - category已存在：替换其templates
        - category不存在：新增一条hot_question条目（关键词留空，靠LLM分类触发）
        """
        hot_questions = self.templates.get("hot_questions", [])
        if not hot_questions:
            hot_questions = []

        for category, templates_list in overrides.items():
            found = False
            for hq in hot_questions:
                if hq.get("category") == category:
                    hq["templates"] = templates_list
                    found = True
                    break
            if not found:
                # 新增一个条目（关键词留空，主要靠LLM分类触发）
                hot_questions.append({
                    "category": category,
                    "keywords": [],
                    "templates": templates_list
                })

        self.templates["hot_questions"] = hot_questions

    def _merge_hot_questions(self, shop_hot_questions: list):
        """
        合并商家 hot_questions 到公用模板
        - 相同 category 的，商家覆盖公用
        - 新 category 的，直接追加
        - 同 category 有多条的，按 category 全替换
        """
        if not shop_hot_questions:
            return
        # 收集商家所有 category
        shop_categories = {}
        for hq in shop_hot_questions:
            cat = hq.get("category", "")
            if cat not in shop_categories:
                shop_categories[cat] = []
            shop_categories[cat].append(hq)

        # 过滤掉公用模板中被商家覆盖的 category
        public_hot = self.templates.get("hot_questions", [])
        merged = [hq for hq in public_hot if hq.get("category", "") not in shop_categories]

        # 追加商家的 hot_questions
        for cat_hqs in shop_categories.values():
            merged.extend(cat_hqs)

        self.templates["hot_questions"] = merged

    def _apply_shop_rec_matrix(self, shop_id: str):
        """
        加载商家私有推荐矩阵并覆盖到公用矩阵上
        商家推荐矩阵可以放在两个地方（按优先级）：
        1) shops/{shop_id}_templates.yaml 中的 recommendation_matrix 字段（推荐）
        2) 独立文件 shops/{shop_id}_rec_matrix.yaml
        - scene_names / preference_names / scene_follow_up：整体覆盖（商家有就用商家的）
        - matrix：按场景逐层覆盖（商家有这个场景就全用商家的，没有就用公用的）
        """
        shop_matrix_data = None

        # 先尝试从商家模板文件里读 recommendation_matrix 字段
        shop_tpl_path = os.path.join(BASE_DIR, "shops", f"{shop_id}_templates.yaml")
        if os.path.exists(shop_tpl_path):
            try:
                with open(shop_tpl_path, encoding="utf-8") as f:
                    shop_templates = yaml.safe_load(f) or {}
                if "recommendation_matrix" in shop_templates:
                    shop_matrix_data = {"matrix": shop_templates["recommendation_matrix"]}
            except Exception as e:
                print(f"[推荐矩阵] 从模板文件读取商家{shop_id}矩阵失败: {e}")

        # 如果模板文件里没有，再试试独立矩阵文件
        if not shop_matrix_data:
            shop_matrix_path = os.path.join(BASE_DIR, "shops", f"{shop_id}_rec_matrix.yaml")
            if not os.path.exists(shop_matrix_path):
                return
            try:
                with open(shop_matrix_path, encoding="utf-8") as f:
                    shop_matrix_data = yaml.safe_load(f) or {}
            except Exception as e:
                print(f"[推荐矩阵] 商家{shop_id}矩阵加载失败: {e}")
                return

        if not shop_matrix_data:
            return

        # 顶层简单键直接覆盖
        for key in ["scene_names", "preference_names", "scene_follow_up"]:
            if key in shop_matrix_data:
                self.rec_matrix[key] = shop_matrix_data[key]

        # matrix 按场景逐层覆盖
        if "matrix" in shop_matrix_data:
            public_matrix = self.rec_matrix.get("matrix", {})
            shop_matrix = shop_matrix_data["matrix"] or {}
            merged_matrix = {**public_matrix, **shop_matrix}
            self.rec_matrix["matrix"] = merged_matrix

        print(f"[推荐矩阵] 已应用商家 {shop_id} 私有推荐矩阵")

    def _load_private_rec_matrix(self, shop_id: str):
        """
        加载商家私有全场景推荐矩阵（全铝风格，12场景×4板材结构）
        文件：shops/{shop_id}_recommend_matrix.json
        加载成功则存到 self.private_rec_matrix，失败静默跳过
        """
        matrix_path = os.path.join(BASE_DIR, "shops", f"{shop_id}_recommend_matrix.json")
        if not os.path.exists(matrix_path):
            self.private_rec_matrix = None
            return
        try:
            with open(matrix_path, encoding="utf-8") as f:
                self.private_rec_matrix = json.load(f)
            print(f"[私有推荐矩阵] 已加载商家 {shop_id} 私有推荐矩阵")
        except Exception as e:
            print(f"[私有推荐矩阵] 商家{shop_id}矩阵加载失败: {e}")
            self.private_rec_matrix = None

    def _render(self, raw):
        """用 jinja2 渲染单条模板字符串"""
        return Template(raw).render(**self._vars())

    # ---------- 1) 高频问题前置命中（最高优先级，不走 LLM） ----------
    # ========== 指代词 + 上下文板材 智能匹配 ==========
    # 板材类型关键词映射（用于从回答文本中提取提到了哪些板材）
    MATERIAL_NAME_KEYWORDS = {
        "spc": ["SPC", "spc蜂窝", "SPC蜂窝"],
        "honeycomb": ["全铝蜂窝", "蜂窝板", "铝蜂窝"],
        "welded": ["焊接大板", "全铝焊接", "焊接板"],
        "carbon": ["碳脂板", "碳脂", "碳酯板"],
    }

    # 明确指向2种的指代词
    EXACT_TWO_PRONOUNS = ["这两种", "这两款", "这两个", "这俩", "那两种", "那两款", "那两个"]
    # 数量模糊的指代词（可能2种，也可能全部）
    AMBIGUOUS_PRONOUNS = ["这些", "它们", "他们", "这几种", "这几样", "那几种"]
    # 单数指代词（指向1种）
    SINGLE_PRONOUNS = ["这个板", "这个板材", "这种板", "这种板材", "这款", "这款板", "这款板材", "那个板", "那种板"]
    # 对比词（说明用户在问区别）
    COMPARE_WORDS = ["区别", "哪个好", "怎么选", "对比", "差别", "不一样", "优缺点", "有啥区别", "什么区别"]

    def _extract_materials_from_text(self, text):
        """从文本中提取提到了哪些板材，返回板材key列表（去重，按出现顺序）"""
        if not text:
            return []
        found = []
        for mat_key, keywords in self.MATERIAL_NAME_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                found.append(mat_key)
        return found

    def _get_context_materials(self):
        """
        从最近一条提到板材的消息中提取上下文板材
        往前遍历最近5轮对话，每轮的bot消息和用户消息都查
        找到第一条提到板材的消息就返回（只取那一条里的，不跨越多条合并）
        用户说'这两种'指的是刚讨论的那一组，不是历史上所有提过的混在一起
        遍历完5轮都没找到 → 返回空列表
        """
        if not self.history:
            return []
        # 往前找最近5轮（从最新的开始）
        checked = 0
        for entry in reversed(self.history):
            if checked >= 5:
                break
            checked += 1
            # 先查bot消息（优先级更高，因为是系统说的）
            bot_text = entry.get("bot", "")
            if bot_text:
                mats = self._extract_materials_from_text(bot_text)
                if mats:
                    return mats
            # 再查用户消息
            user_text = entry.get("user", "")
            if user_text:
                mats = self._extract_materials_from_text(user_text)
                if mats:
                    return mats
        return []

    def _detect_pronoun_type(self, text):
        """检测指代词类型：返回 'exact_two' / 'ambiguous' / None"""
        if not text:
            return None
        # 先检查明确2种的（优先级更高）
        if any(kw in text for kw in self.EXACT_TWO_PRONOUNS):
            return "exact_two"
        # 再检查模糊型
        if any(kw in text for kw in self.AMBIGUOUS_PRONOUNS):
            return "ambiguous"
        return None

    def _has_compare_word(self, text):
        """检测有没有对比相关的词"""
        return any(kw in text for kw in self.COMPARE_WORDS)

    def _resolve_pronoun_material_compare(self, text):
        """
        指代词+上下文板材智能解析：
        用户用"这两种/它们/这个板"等指代问板材问题时，结合上一轮回答的内容判断。
        返回 (category, answer) 或 None（无法解析则走原有关键词匹配）

        【对比型问题】（有区别/哪个好/对比等词）：
        - 明确2种型（这两种）+ 上文正好2种 → 精准对比
        - 明确2种型 + 上文≠2种 → 反问
        - 模糊型（它们/这些）+ 上文正好2种 → 精准对比
        - 模糊型 + 上文3-4种 → 四款总览（和原来一样）
        - 模糊型 + 上文1种 → 该板材详情

        【详情型问题】（单数指代词，没有对比词）：
        - 上文正好1种 → 答该板材详情
        """
        # ===== 详情型问题：单数指代词 + 没有对比词 =====
        is_single_pronoun = any(kw in text for kw in self.SINGLE_PRONOUNS)
        if is_single_pronoun and not self._has_compare_word(text):
            context_materials = self._get_context_materials()
            if len(context_materials) == 1:
                detail_result = self._get_material_detail_answer(context_materials[0])
                if detail_result:
                    return detail_result
            return None  # 上下文不是1种或没有详情模板，走原有逻辑

        pronoun_type = self._detect_pronoun_type(text)
        if pronoun_type is None:
            return None  # 没有指代词，走原有关键词匹配

        # 有指代词，但没有对比词 → 不是在问对比，走原有逻辑
        if not self._has_compare_word(text):
            return None

        context_materials = self._get_context_materials()
        num_context = len(context_materials)

        # === 明确2种型 ===
        if pronoun_type == "exact_two":
            if num_context == 2:
                # 正好2种 → 先尝试精准对比
                result = self._render_material_compare_for(context_materials)
                if result:
                    return result
                # 有2种但没有专门的对比模板 → 反问确认，别瞎答
                reply = "您说的是哪两种板材呀？我们有SPC蜂窝板、全铝蜂窝板、焊接大板和碳脂板四种，您指的是哪两种的对比？"
                return "material/pronoun_clarify", reply
            else:
                # 不是正好2种 → 反问，别瞎猜
                reply = "您说的是哪两种板材呀？我们有SPC蜂窝板、全铝蜂窝板、焊接大板和碳脂板四种，您指的是哪两种的对比？"
                return "material/pronoun_clarify", reply

        # === 模糊型 ===
        if pronoun_type == "ambiguous":
            if num_context == 1:
                # 只有1种 → 答该板材详情
                # 找对应详情模板
                mat = context_materials[0]
                detail_result = self._get_material_detail_answer(mat)
                if detail_result:
                    return detail_result
                # 找不到详情模板就返回None走原有逻辑
                return None
            elif num_context == 2:
                # 正好2种 → 精准对比
                return self._render_material_compare_for(context_materials)
            else:
                # 3-4种 → 四款总览（和原逻辑一样，返回None走关键词匹配）
                # 但这里可以直接返回，省得再匹配一遍
                # 走原有关键词匹配，确保和原来的四款总览模板一致
                return None  # 交给原有关键词匹配处理

        return None

    def _render_material_compare_for(self, mat_keys):
        """根据具体的2种板材，返回对应的精准对比回答
        目前只支持 welded+carbon 的精准对比，其他组合返回None走总览
        """
        if len(mat_keys) != 2:
            return None

        m_set = set(mat_keys)

        # 焊接大板 vs 碳脂板 → 用welded_vs_carbon模板
        if m_set == {"welded", "carbon"}:
            for hot_q in self.templates.get("hot_questions", []):
                if hot_q.get("category") == "welded_vs_carbon":
                    tpl_list = hot_q.get("templates", [])
                    if tpl_list:
                        import random as _rand
                        answer = self._render(_rand.choice(tpl_list))
                        return "material/compare_welded_carbon", answer

        # 其他组合 → 暂时没有精准模板，返回None走总览兜底
        return None

    def _get_material_detail_answer(self, mat_key):
        """获取单种板材的详情介绍"""
        # 碳脂板 → 用carbon_detail模板
        if mat_key == "carbon":
            for hot_q in self.templates.get("hot_questions", []):
                if hot_q.get("category") == "carbon_detail":
                    tpl_list = hot_q.get("templates", [])
                    if tpl_list:
                        import random as _rand
                        answer = self._render(_rand.choice(tpl_list))
                        return "material/detail_carbon", answer

        # 其他板材 → 暂时返回None，走原有关键词匹配或知识库
        return None

    def _match_hot_question(self, text):
        """
        关键词命中就返回（分类标签, 渲染好的话术），否则返回 None
        跳过工艺类问题（有 process_key 的）和议价专属模板（bargain_only）
        匹配度最高优先（匹配关键词数多的优先，相同则关键词总长度长的优先）
        """
        # ===== 指代词 + 上下文板材 智能匹配 =====
        # 用户说"这两种有什么区别"时，结合上一轮提到的板材来判断
        pronoun_result = self._resolve_pronoun_material_compare(text)
        if pronoun_result is not None:
            return pronoun_result

        best_match = None
        best_count = 0
        best_len = 0

        for hot_q in self.templates.get("hot_questions", []):
            # 工艺类问题跳过，由专门的工艺判断方法处理
            if hot_q.get("process_key"):
                continue
            # 议价专属模板跳过，由议价状态机处理
            if hot_q.get("bargain_only"):
                continue

            matched_kws = [kw for kw in hot_q.get("keywords", []) if kw in text]
            if matched_kws:
                count = len(matched_kws)
                total_len = sum(len(kw) for kw in matched_kws)
                # 匹配数更多的优先；数相同则总长度更长的更具体，优先
                if count > best_count or (count == best_count and total_len > best_len):
                    best_match = hot_q
                    best_count = count
                    best_len = total_len

        if best_match is not None:
            templates_list = best_match.get("templates", [])
            if templates_list:
                category = best_match.get("category", "hot_question")
                template_str = random.choice(templates_list)
                # 工期类问题 → 动态计算天数并注入变量
                if category == "total_cycle":
                    days = self._calc_delivery_days()
                    extra = {
                        "total_days": str(days["total"]),
                        "design_days": str(days["design"]),
                        "production_days": str(days["production"]),
                        "install_days": str(days["install"]),
                    }
                    answer = self._render_with_extra(template_str, extra)
                else:
                    answer = self._render(template_str)
                return category, answer
        return None

    # ---------- 2) 工艺能力判断 ----------
    def _match_private_special_shape(self, text):
        """
        私有矩阵特殊造型检测（洞洞板/格栅/圆弧/异形/倒角 等）
        命中后直接返回对应话术，优先级高于普通工艺判断
        返回：(tag, answer) 或 None
        """
        if not self.private_rec_matrix:
            return None

        # 1. 特殊造型（必须碳脂板）：优先级最高
        special = self.private_rec_matrix.get("special_shapes", {})
        special_items = special.get("items", [])
        special_hits = [item for item in special_items if item in text]
        if special_hits:
            shape_list = "、".join(special_hits)
            tpl = special.get("answer_template", "")
            answer = tpl.replace("{shape_list}", shape_list)
            answer = self._render(answer)  # 渲染 {{wechat_id}} 等变量
            return "process/special_shape", answer

        # 2. 焊接大板简单造型（倒角/切角）
        welded = self.private_rec_matrix.get("welded_simple_shapes", {})
        welded_items = welded.get("items", [])
        # 用关键词匹配：倒角、切角
        welded_keywords = ["倒角", "切角"]
        if any(kw in text for kw in welded_keywords):
            tpl = welded.get("answer_template", "")
            answer = self._render(tpl)
            return "process/welded_simple_shape", answer

        # 3. 铝型材圆弧
        arc = self.private_rec_matrix.get("aluminum_profile_arc", {})
        arc_keywords = ["圆弧", "圆角", "弧度"]
        if any(kw in text for kw in arc_keywords):
            tpl = arc.get("answer_template", "")
            answer = self._render(tpl)
            return "process/aluminum_arc", answer

        return None

    def _get_merged_process_templates(self):
        """
        获取合并后的工艺模板列表（系统内置 + 商家自定义）
        商家自定义的优先级更高，同 key 覆盖系统内置的
        结果缓存，不用每次合并
        """
        if hasattr(self, "_merged_process_templates"):
            return self._merged_process_templates

        # 1) 收集系统内置的工艺模板（带 process_key 的）
        builtin = {}
        for hot_q in self.templates.get("hot_questions", []):
            process_key = hot_q.get("process_key")
            if process_key:
                builtin[process_key] = hot_q

        # 2) 收集商家自定义的工艺模板
        from customer_service.shop_config_loader import convert_processes_to_templates
        shop_processes = self.config.get("_processes", [])
        shop_templates = convert_processes_to_templates(shop_processes)
        shop_dict = {}
        for tpl in shop_templates:
            key = tpl.get("process_key")
            if key:
                shop_dict[key] = tpl

        # 3) 合并：商家的覆盖系统的
        merged = dict(builtin)
        merged.update(shop_dict)

        # 转成列表返回
        result = list(merged.values())
        self._merged_process_templates = result
        return result

    def _match_process_question(self, text):
        """
        匹配工艺关键词，识别客户问的是哪个工艺
        匹配源：系统内置工艺模板 + 商家自定义工艺（商家的优先级高）
        匹配度最高优先（匹配关键词数多的优先）
        查 shop_config 的 process_capability
        能做 → 渲染 yes_templates
        不能做 → 渲染 no_templates（替代方案话术）
        匹配不上 → 返回 None，走正常流程
        """
        # 先查私有矩阵特殊造型（优先级最高）
        private_shape = self._match_private_special_shape(text)
        if private_shape:
            return private_shape

        best_match = None
        best_count = 0
        best_len = 0

        process_templates = self._get_merged_process_templates()
        for hot_q in process_templates:
            process_key = hot_q.get("process_key")
            if not process_key:
                continue

            matched_kws = [kw for kw in hot_q.get("keywords", []) if kw in text]
            if matched_kws:
                count = len(matched_kws)
                total_len = sum(len(kw) for kw in matched_kws)
                if count > best_count or (count == best_count and total_len > best_len):
                    best_match = hot_q
                    best_count = count
                    best_len = total_len

        if best_match is not None:
            process_key = best_match["process_key"]
            # 查配置判断能不能做（默认能做，避免配置缺失导致不说话）
            can_do = self.config.get("process_capability", {}).get(process_key, True)
            if can_do:
                templates_list = best_match.get("yes_templates", [])
            else:
                templates_list = best_match.get("no_templates", [])
            if templates_list:
                answer = self._render(random.choice(templates_list))
                return f"process/{process_key}/{can_do}", answer
        return None

    # ---------- 3) 议价多轮状态机 ----------
    def _detect_order_size(self, text):
        """
        根据关键词判断单值大小
        返回：(size, input_type, raw_quantity) 元组
          - size: 'large'/'medium'/'small'/None
          - input_type: 'area'（面积）/'count'（柜子数量）/'whole_house'（全屋）/None
          - raw_quantity: 提取到的原始数量，如 '10'、'3'，用于模板动态插入
        优先级：面积数字（精确值） > 柜子数量（降级估算） > 全屋类（按large估算） > 不知道
        面积分档：≤5平=小单，6-29平=中单，≥30平=大单
        柜子估算：每柜按3.5㎡换算（衣柜2.4m高×1.5m宽左右）
        全屋估算：按25㎡估算（中单偏大，走medium档）
        """
        import re

        # 1. 面积检测（最高优先级）：精确值 + 模糊范围，按匹配优先级从高到低
        # 支持：精确数字 / 7-8平 / 7 8个平 / 七八平 / 十来个平方 / 20多平 / 30来平 等

        # 投影面积检测：文本中出现投影相关词汇 → 按投影面积×2.5换算成展开面积
        # ponytail: 投影是用户习惯说法，实际计价都是按展开面积
        projection_keywords = ["投影", "投影面积", "按投影算", "按投影", "投影算"]
        has_projection = any(kw in text for kw in projection_keywords)
        projection_ratio = 2.5  # 投影转展开的系数

        def _area_to_size(area, raw):
            """辅助函数：面积数值→分档 + 返回三元组
            分档（按展开面积）：
            - small: <15平
            - medium: 15-30平
            - large: 30-50平
            - xlarge: ≥50平
            """
            if has_projection:
                real_area = area * projection_ratio
                raw_display = f"投影{raw}平(展开约{real_area:.0f}平)"
            else:
                real_area = area
                raw_display = raw
            if real_area >= 50:
                return ("xlarge", "area", raw_display)
            elif real_area >= 30:
                return ("large", "area", raw_display)
            elif real_area >= 15:
                return ("medium", "area", raw_display)
            else:
                return ("small", "area", raw_display)

        # 1.1 阿拉伯数字模糊范围（优先级高于单个数字）："7 8个平" "7-8平" "7~8个平方" 这种
        # ponytail: 两个数字之间必须有空格或分隔符，防止"10"被误拆成"1-0"
        fuzzy_area_match = re.search(r'(\d+)(?:\s+|[-~到至])(\d+)\s*(?:个)?\s*(?:平方|平米|平|㎡)', text)
        if fuzzy_area_match:
            low = float(fuzzy_area_match.group(1))
            high = float(fuzzy_area_match.group(2))
            avg_area = (low + high) / 2
            raw_desc = f"{int(low)}-{int(high)}"
            return _area_to_size(avg_area, raw_desc)

        # 1.2 单个数字（精确/模糊值）
        # 支持：20平 / 20多平 / 30来平 / 15左右平 / 大概20个平方
        area_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:多|来|左右|大概)?\s*(?:个)?\s*(?:平方|平米|平|㎡)', text)
        if area_match:
            raw = area_match.group(1)
            area = float(raw)
            # 如果是"XX多平"，面积向上取整一点（20多平≈22平，保守估算）
            if "多" in area_match.group(0) or "来" in area_match.group(0):
                area = area * 1.1  # 粗略加10%
            return _area_to_size(area, raw)

        # 1.3 汉字数字模糊范围："七八个平" "五六平" "十来个平方" 这种
        # ponytail: 按长度从长到短排，避免"十几"误匹配"二十几"；长字符串优先
        cn_fuzzy_area_patterns = [
            ("二十几", 22), ("三十几", 32), ("二三十", 25),
            ("一两", 1.5), ("二三", 2.5), ("三四", 3.5), ("四五", 4.5),
            ("五六", 5.5), ("六七", 6.5), ("七八", 7.5), ("八九", 8.5),
            ("九十", 9.5), ("十来", 10), ("十几", 12),
        ]
        for pattern, avg_val in cn_fuzzy_area_patterns:
            # 前面不是汉字数字，避免"二十几"被"十几"匹配
            if re.search(r'(?<![一二三四五六七八九十两])' + pattern + r'\s*(?:个)?\s*(?:平方|平米|平|㎡)', text):
                return _area_to_size(avg_val, pattern)

        # 2. 柜子数量检测（降级估算面积，每柜按3.5㎡换算）
        # ponytail: 柜子数量降级为面积估算，标记input_type=count，话术里带"大概"
        # 正则匹配：数字（汉字或阿拉伯） + 个 + 柜子/衣柜/鞋柜/书柜/酒柜/橱柜 等
        import re as _re
        # 阿拉伯数字 + 个 + 家具名
        count_match = _re.search(r'(\d+)\s*(?:个|套|组)\s*(?:柜子|衣柜|鞋柜|书柜|酒柜|橱柜|吊柜|地柜|榻榻米|定制)?', text)
        if count_match:
            count_num = int(count_match.group(1))
            est_area = count_num * 3.5  # 每柜按3.5㎡估算
            if est_area >= 50:
                return ("xlarge", "count", count_match.group(1))
            elif est_area >= 30:
                return ("large", "count", count_match.group(1))
            elif est_area >= 15:
                return ("medium", "count", count_match.group(1))
            else:
                return ("small", "count", count_match.group(1))

        # 汉字数字匹配：一两/两三/三四/四五 + 个/柜子/衣柜 等
        cn_count_patterns = [
            ("一两个", 1.5), ("一两", 1.5), ("一二个", 1.5),
            ("两三个", 2.5), ("两三", 2.5),
            ("三四个", 3.5), ("三四", 3.5),
            ("四五个", 4.5), ("四五", 4.5),
            ("五六个", 5.5), ("五六", 5.5),
            ("六七个", 6.5), ("六七", 6.5),
            ("七八个", 7.5), ("七八", 7.5),
            ("十几个", 10), ("十多个", 10),
        ]
        for pattern, cnt_val in cn_count_patterns:
            if pattern in text and any(suffix in text for suffix in ["柜子", "衣柜", "鞋柜", "书柜", "酒柜", "橱柜", "个", ""]):
                est_area = cnt_val * 3.5
                raw_desc = pattern.replace("个", "").replace("柜子", "")
                if est_area >= 50:
                    return ("xlarge", "count", raw_desc)
                elif est_area >= 30:
                    return ("large", "count", raw_desc)
                elif est_area >= 15:
                    return ("medium", "count", raw_desc)
                else:
                    return ("small", "count", raw_desc)

        # 单个汉字数词 + 个 + 家具名（一个衣柜/两个鞋柜/三个书柜...）
        cn_nums = {
            "一": 1, "两": 2, "二": 2, "三": 3, "四": 4,
            "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10
        }
        _cn_match = _re.search(r'([一两二三四五六七八九十])\s*个\s*(?:柜子|衣柜|鞋柜|书柜|酒柜|橱柜|吊柜|地柜|榻榻米|定制)', text)
        if _cn_match:
            num_char = _cn_match.group(1)
            count_num = cn_nums.get(num_char, 1)
            est_area = count_num * 3.5
            raw_desc = str(count_num)
            if est_area >= 50:
                return ("xlarge", "count", raw_desc)
            elif est_area >= 30:
                return ("large", "count", raw_desc)
            elif est_area >= 15:
                return ("medium", "count", raw_desc)
            else:
                return ("small", "count", raw_desc)

        # 关键词匹配柜子数量（无明确数字时）
        # 按3.5平/柜估算，对应新面积分档：<15平=small, 15-30=medium, 30-50=large, ≥50=xlarge
        small_keywords = [
            "一个柜子", "两个柜子", "三个柜子", "四个柜子",
            "就一个", "就做一个", "就衣柜", "就鞋柜",
            "一二个", "一两个", "两三个", "三四个", "二三", "三四",
            "只做一个", "1个", "2个", "3个", "4个",
            "就一个柜子", "单个", "一个房间", "两个房间",
        ]
        medium_keywords = [
            "五个柜子", "六个柜子", "七个柜子", "八个柜子",
            "四五个", "五六", "六七", "七八", "5个", "6个", "7个", "8个",
            "几个柜子", "三四个柜子", "四五个柜子",
        ]
        large_keywords_count = ["九个柜子", "十个柜子", "很多柜子", "十几个", "十来个"]

        if any(kw in text for kw in large_keywords_count):
            return ("large", "count", "十几")
        if any(kw in text for kw in medium_keywords):
            if "四五个" in text or "四五" in text:
                return ("medium", "count", "四五")
            elif "5个" in text:
                return ("medium", "count", "5")
            elif "6个" in text:
                return ("medium", "count", "6")
            elif "七八个" in text or "七八" in text:
                return ("medium", "count", "七八")
            return ("medium", "count", "五六")
        if any(kw in text for kw in small_keywords):
            if "三四个" in text or "三四" in text:
                return ("small", "count", "三四")
            elif "两三个" in text or "二三" in text:
                return ("small", "count", "两三")
            elif "一个" in text or "1个" in text or "就一个" in text:
                return ("small", "count", "1")
            elif "两个" in text or "2个" in text:
                return ("small", "count", "2")
            return ("small", "count", "一两个")

        # 3. 全屋类（最低优先级，按large估算）
        whole_house_keywords = [
            "全屋", "整体", "全部", "整套", "所有房间", "全屋定制",
            "整套房子", "全房", "所有柜子", "全部做", "全套"
        ]
        if any(kw in text for kw in whole_house_keywords):
            return ("xlarge", "whole_house", None)

        return (None, None, None)

    def _gen_order_desc(self, size_val, input_type, raw_quantity):
        """
        根据订单信息生成描述字符串，用于模板动态插入
        - 面积输入 → "10个平方"（精确值，不带大概）
        - 柜子数量输入 → "大概3个柜子"（估算值，带大概）
        - 全屋类输入 → "大概全屋"（估算值，带大概）
        - 判断不出 → "您这单"
        """
        if input_type == "area" and raw_quantity:
            return f"{raw_quantity}个平方"
        elif input_type == "count" and raw_quantity:
            return f"大概{raw_quantity}个柜子"
        elif input_type == "whole_house":
            return "大概全屋"
        else:
            return "您这单"

    def _calc_delivery_days(self, area=None):
        """
        动态计算工期（天）
        能从历史里拿到面积就用真实面积算，拿不到按20平（中单）估算
        柜子数量按1柜≈2平换算，全屋按30平算
        返回：dict 含 design/chai/production/install/total/area
        """
        import math

        # 从历史里找面积/柜子数量/全屋
        if area is None:
            for h in reversed(self.history):
                s_val, s_type, s_raw = self._detect_order_size(h["user"])
                if s_val and s_type == "area" and s_raw:
                    try:
                        area = float(s_raw)
                        break
                    except ValueError:
                        pass
                # 柜子数量 → 按1柜≈2平换算
                elif s_val and s_type == "count" and s_raw:
                    try:
                        area = float(s_raw) * 2
                        break
                    except ValueError:
                        pass
                # 全屋 → 按30平算
                elif s_type == "whole_house":
                    area = 30
                    break
        # 还是没有就按20平估算
        if area is None:
            area = 20

        prod_config = self.config.get("production", {})
        if not prod_config:
            # 配置不全就返回默认
            return {"design": 3, "chai": 2, "production": 15, "install": 1, "total": 21, "area": area}

        design_output = prod_config.get("design_output_per_day", 30)
        install_output = prod_config.get("install_output_per_day", 20)
        chai_days = prod_config.get("chai_dan_days", 2)
        prod_base = prod_config.get("production_base_days", 15)
        prod_per_10 = prod_config.get("production_per_10sqm_days", 1)
        design_min = prod_config.get("design_min_days", 3)
        install_min = prod_config.get("install_min_days", 1)

        design_days = max(design_min, math.ceil(int(area) / design_output))
        production_days = prod_base + (int(area) // 10) * prod_per_10
        install_days = max(install_min, math.ceil(int(area) / install_output))
        total = design_days + chai_days + production_days + install_days

        return {
            "design": design_days,
            "chai": chai_days,
            "production": production_days,
            "install": install_days,
            "total": total,
            "area": area,
        }

    def _format_delivery_text(self, days_info):
        """把工期计算结果格式化成口语化回答"""
        d = days_info
        return f"从签合同到装完，大概{d['total']}天左右。设计{d['design']}天，拆单排产{d['chai']}天，生产{d['production']}天，安装{d['install']}天。"

    def _get_order_info(self):
        """
        从历史记录里找最近的订单信息，用于模板渲染
        返回：(size_val, input_type, raw_quantity, order_desc)
        """
        # 先从历史里找
        for h in reversed(self.history):
            size_val, input_type, raw_qty = self._detect_order_size(h["user"])
            if size_val:
                order_desc = self._gen_order_desc(size_val, input_type, raw_qty)
                return size_val, input_type, raw_qty, order_desc
        # 找不到就返回默认（兜底）
        return "medium", None, None, "您这单"


    def _handle_bargain(self, text):
        """
        议价状态机（4步流程 + 闭环兜底：报价→选材料→摸底→优惠→升级）
        返回：(阶段名, 话术内容)

        状态定义：
          0 = 未开始
          1 = 已报价格区间（等用户选材料）
          2 = 已报选中材料的实价（等用户问优惠）
          3 = 已发摸底反问（等用户报面积/数量）
          4 = 已给优惠报价
          5+ = 升级话术

        核心原则：每一步都有闭环，接不住也有兜底，绝对不跑丢
        """
        is_bargain = self._is_bargain_question(text)
        is_end = self._is_end_conversation(text)
        size_val, input_type, raw_qty = self._detect_order_size(text)
        order_desc = self._gen_order_desc(size_val, input_type, raw_qty)
        material = self._detect_material_choice(text)
        preference = self._detect_preference_type(text)

        # 从历史里找线索（本轮判断不出就看历史）
        if size_val is None:
            for h in self.history:
                s_val, s_type, s_raw = self._detect_order_size(h["user"])
                if s_val:
                    size_val, input_type, raw_qty = s_val, s_type, s_raw
                    order_desc = self._gen_order_desc(size_val, input_type, raw_qty)
                    break
        if material is None:
            for h in self.history:
                mat = self._detect_material_choice(h["user"])
                if mat:
                    material = mat
                    break

        # ========== 结束对话检测（任何阶段都可能触发）==========
        if is_end:
            return "lead_wechat", self._render_bargain_template("bargain_lead_wechat")

        # ========== 特殊场景：一步到位（既说了材料又说了数量）==========
        if material and size_val and self.bargain_step <= 2:
            self.bargain_step = 4
            self.bargain_pullback_count = 0
            ans = self._render_bargain_template(
                f"bargain_{size_val}", material=material, order_desc=order_desc
            )
            ans = self._append_lead_hook(ans)
            return size_val, ans

        # ========== Step 0：初次进入 ==========
        # 纯问价 → 先报价格区间（step1）
        # 砍价/要优惠 → 先摸底反问（step3），不直接报价
        if self.bargain_step == 0:
            # 砍价意图优先 → 摸底（但已有面积就跳过摸底，直接报价）
            if is_bargain:
                if self._has_collected("area"):
                    # 已有面积 → 跳过摸底，直接报区间（走step1流程）
                    self.bargain_step = 1
                    self.bargain_pullback_count = 0
                    return "price_range", self._render_bargain_template("bargain_price_range")
                self.bargain_step = 3
                self.bargain_pullback_count = 0
                return "probe", self._render_bargain_template("bargain_probe")
            # 纯问价格 → 报价格区间（step1）
            self.bargain_step = 1
            self.bargain_pullback_count = 0
            return "price_range", self._render_bargain_template("bargain_price_range")

        # ========== 公共拦截：报过价之后（step>=1），先检测嫌贵/竞品回怼 ==========
        # 命中就返回对应话术，状态不推进（砍价信号还是交给各Step自己判断是否推进）
        if self.bargain_step >= 1:
            pushback_result = self._detect_bargain_pushback(text)
            if pushback_result is not None:
                pushback_type, pushback_ans = pushback_result
                # 嫌贵/竞品不算跑偏，不加 pullback 计数
                return f"pushback/{pushback_type}", pushback_ans

        # ========== Step 1：等用户选材料 ==========
        if self.bargain_step == 1:
            # 明确选了材料 → 报实价，进step2
            if material:
                self.bargain_step = 2
                self.selected_material = material
                self.bargain_pullback_count = 0
                return "material_price", self._render_bargain_template(
                    "bargain_material_price", material=material
                )

            # ===== 二维推荐矩阵（场景 × 偏好）=====
            # 优先级最高：用户说了场景或偏好，就直接走智能推荐
            rec_result = self._get_recommendation(text)
            if rec_result:
                mat_key = rec_result["material"]
                reason = rec_result["reason"]
                follow_up = rec_result["follow_up"]
                scene_key = rec_result["scene_key"]
                # 记录选中的材料
                self.selected_material = mat_key
                # 推进到 step 2
                self.bargain_step = 2
                self.bargain_pullback_count = 0

                # 私有推荐矩阵 → 用私有格式组装话术
                if rec_result.get("is_private"):
                    mat_name = rec_result["material_name"]
                    mat_price = rec_result["material_price"]
                    mix_match = rec_result.get("mix_match", "")
                    recommend_parts = [
                        f"根据您的情况，我推荐用【{mat_name}】，{mat_price}一平。",
                        reason,
                    ]
                    if mix_match:
                        recommend_parts.append(mix_match)
                    recommend_text = "\n".join(recommend_parts)
                else:
                    # 渲染推荐话术（变量化模板）+ 场景化跟进提问
                    recommend_text = self._render_bargain_template(
                        "bargain_recommendation_v2",
                        material=mat_key,
                        extra_vars={
                            "recommend_reason": reason,
                            "recommended_material_name": rec_result["material_name"],
                            "recommended_material_price": str(rec_result["material_price"]),
                            "board_brand": self.config.get("board_brand", ""),
                            "eco_level": self.config.get("eco_level", ""),
                            "hardware_brand": self.config.get("hardware_brand", ""),
                            "edge_banding": self.config.get("edge_band", ""),
                        }
                    )
                # 拼接场景化跟进提问
                full_answer = recommend_text + "\n" + follow_up
                return f"recommend/{scene_key}", full_answer

            # 只说了面积/数量没说材料 → 按默认材料报价，进step2
            if size_val:
                default_mat = self.config.get("default_material", "particle_board")
                self.bargain_step = 2
                self.selected_material = default_mat
                self.bargain_pullback_count = 0
                return "material_price", self._render_bargain_template(
                    "bargain_material_price", material=default_mat
                )

            # 直接砍价来的 → 再报一次价格区间（换个说法）
            if is_bargain:
                self.bargain_pullback_count += 1
                if self.bargain_pullback_count >= 2:
                    return "lead_wechat", self._render_bargain_template("bargain_lead_wechat")
                return "price_range", self._render_bargain_template("bargain_price_range")

            # 以上都没命中 → 用原有一维逻辑兜底（问环保还是性价比）
            self.bargain_pullback_count += 1
            if self.bargain_pullback_count >= 2:
                return "lead_wechat", self._render_bargain_template("bargain_lead_wechat")
            # 不推进状态，返回追问话术（您关注环保还是性价比？）
            return "pullback", self._render_pullback_template(step=1)

        # ========== Step 2：等用户问优惠 ==========
        if self.bargain_step == 2:
            # 问优惠/砍价/嫌贵 → 摸底，进step3
            if is_bargain:
                self.bargain_step = 3
                self.bargain_pullback_count = 0
                return "probe", self._render_bargain_template("bargain_probe")

            # 确认/认可 → 顺势问需求，进step3摸底
            confirm_keywords = ["行", "可以", "还行", "好的", "没问题", "嗯", "ok", "OK"]
            if any(kw in text for kw in confirm_keywords):
                self.bargain_step = 3
                self.bargain_pullback_count = 0
                return "probe", self._render_bargain_template("bargain_probe")

            # 又换了另一种材料 → 换材料报价
            if material:
                self.bargain_pullback_count = 0
                return "material_price", self._render_bargain_template(
                    "bargain_material_price", material=material
                )

            # 补充新场景推荐：用户提到新场景（客厅/卧室等）但没说具体材料 → 沿用已选材料，给补充场景话术
            # ponytail: v2兜底，防止LLM选不对动作时直接拉回正题
            scene_key, scene_name = self._detect_room_type(text)
            if scene_key and self.selected_material:
                # 查推荐矩阵，获取这个场景用当前材料的理由
                rec_reason = f"{scene_name}也用{get_material_name(self.selected_material, self.config)}就行，整体风格统一，施工也方便"
                matrix = self.rec_matrix.get("matrix", {})
                scene_config = matrix.get(scene_key, {})
                if scene_config:
                    # 找一个最接近的偏好的理由（recommend_me 或 balanced）
                    for pref in ["recommend_me", "balanced", "eco_friendly", "cost_effective"]:
                        if pref in scene_config and scene_config[pref].get("material") == self.selected_material:
                            rec_reason = scene_config[pref].get("reason", rec_reason)
                            break
                self.bargain_pullback_count = 0
                extra_vars = {
                    "recommend_reason": rec_reason,
                    "scene_name": scene_name,
                }
                reply = self._render_bargain_template(
                    "bargain_supplement_scene",
                    material=self.selected_material,
                    extra_vars=extra_vars
                )
                return "supplement_scene", reply

            # 说了面积/数量 → 按材料+数量直接给优惠价，跳step4
            if size_val:
                self.bargain_step = 4
                self.bargain_pullback_count = 0
                ans = self._render_bargain_template(
                    f"bargain_{size_val}",
                    material=material or self.config.get("default_material", "particle_board"),
                    order_desc=order_desc
                )
                ans = self._append_lead_hook(ans)
                return size_val, ans

            # 其他情况 → 拉回正题
            self.bargain_pullback_count += 1
            if self.bargain_pullback_count >= 2:
                return "lead_wechat", self._render_bargain_template("bargain_lead_wechat")
            return "pullback", self._render_pullback_template(step=2)

        # ========== Step 3：等用户报面积/数量 ==========
        if self.bargain_step == 3:
            # 安全兜底：如果 collected_info 里已有面积但 size_val 没检测出来，直接用
            if not size_val and self._has_collected("area"):
                area = self.collected_info["area"]
                if area >= 30:
                    size_val = "large"
                elif area >= 6:
                    size_val = "medium"
                else:
                    size_val = "small"
                order_desc = f"大约{area}平"
            # 有面积/数量线索 → 给优惠价，进step4
            if size_val:
                self.bargain_step = 4
                self.bargain_pullback_count = 0
                ans = self._render_bargain_template(
                    f"bargain_{size_val}",
                    material=material or self.config.get("default_material", "particle_board"),
                    order_desc=order_desc
                )
                ans = self._append_lead_hook(ans)
                return size_val, ans

            # 说不知道/没量过 → 直接引导加微信 + 活动钩子，不继续追问
            unknown_keywords = ["不知道", "没量", "没算过", "不清楚", "大概吧", "还没", "不确定", "不知道多少", "没量过", "还没量"]
            if any(kw in text for kw in unknown_keywords):
                self.bargain_pullback_count = 0
                return "lead_wechat", self._render_bargain_template("bargain_unknown_area_lead")

            # 不正面回答（让先报价）→ 拉回正题
            dodge_keywords = ["你先报", "先报个价", "合适再说", "你说说", "报个价"]
            if any(kw in text for kw in dodge_keywords):
                self.bargain_pullback_count += 1
                if self.bargain_pullback_count >= 2:
                    return "lead_wechat", self._render_bargain_template("bargain_lead_wechat")
                return "pullback", self._render_pullback_template(step=3)

            # 检测不出 → 默认走中单（不跑丢，继续推进）
            self.bargain_step = 4
            self.bargain_pullback_count = 0
            ans = self._render_bargain_template(
                "bargain_medium",
                material=material or self.config.get("default_material", "particle_board"),
                order_desc="您这单"
            )
            ans = self._append_lead_hook(ans)
            return "medium", ans

        # ========== Step 4 及以后 ==========
        # bargain_step >= 4
        # 继续砍价 → 升级话术
        if is_bargain:
            self.bargain_step += 1
            self.bargain_pullback_count = 0
            return "upgrade", self._render_bargain_template("bargain_upgrade")

        # 其他情况 → 拉回逼单或引导留资
        self.bargain_pullback_count += 1
        if self.bargain_pullback_count >= 2:
            return "lead_wechat", self._render_bargain_template("bargain_lead_wechat")
        return "pullback", self._render_pullback_template(step=4)


    def _append_lead_hook(self, answer):
        """
        在分档报价结尾追加留资钩子 + 引导话题（只在第一次报价时调用）
        去重逻辑：已收集到微信/电话 → 换邀约话术，不重复索要联系方式
        """
        # 已有微信 → 换邀约话术
        if self._has_collected("wechat"):
            hook = "您加我了是吧？稍后我通过一下，详细报价单和案例我发您微信上。"
            follow_up = "有什么想了解的随时说哈，板材、工艺、安装啥的都行。"
            return answer + "\n\n" + hook + "\n" + follow_up
        # 已有电话 → 换邀约话术
        if self._has_collected("phone"):
            hook = "您的电话我记下了，稍后我让设计师跟您联系，免费给您出个方案和报价。"
            follow_up = "有什么想了解的随时说哈，板材、工艺、安装啥的都行。"
            return answer + "\n\n" + hook + "\n" + follow_up
        lead_hooks = [
            "对了，您加我微信{{wechat_id}}吧，我给您发份详细报价单，您回去也好对比对比。",
            "方便加个微信不{{wechat_id}}？后面有什么变动我直接跟您说。",
            "您加微信{{wechat_id}}我把材料图和实景案例给您发过去，您先看看效果。",
        ]
        hook = self._render(random.choice(lead_hooks))
        follow_up = "有什么想了解的随时说哈，板材、工艺、安装啥的都行。"
        return answer + "\n\n" + hook + "\n" + follow_up

    def _render_pullback_template(self, step):
        """
        渲染拉回正题话术（根据当前step选对应话术）
        bargain_pullback 模板组有4条，分别对应 step 1-4
        去重逻辑：如果该step对应的询问字段已经收集到，就换一个推进方向
        """
        for hot_q in self.templates.get("hot_questions", []):
            if hot_q.get("category") == "bargain_pullback":
                templates_list = hot_q.get("templates", [])
                if templates_list:
                    # step 1 对应索引0，step 2 对应索引1...
                    idx = max(0, min(step - 1, len(templates_list) - 1))
                    # —— 去重判断 ——
                    # step=1（问材料/偏好）：已有材质偏好 → 用下一条（问面积/报价）
                    if step == 1 and self._has_collected("material"):
                        idx = min(idx + 1, len(templates_list) - 1)
                    # step=2（问报价/确认）：已有面积 → 用下一条（问优惠/量房）
                    elif step == 2 and self._has_collected("area"):
                        idx = min(idx + 1, len(templates_list) - 1)
                    # step=3（问面积）：已有面积 → 用上一条（确认配置）
                    elif step == 3 and self._has_collected("area"):
                        idx = max(0, idx - 1)
                    return self._render(templates_list[idx])
        # 兜底
        return "咱先把这事定下来？"

    def _render_bargain_template(self, category_name, material=None, order_desc=None, extra_vars=None, max_index=None):
        """
        从 hot_questions 中找到指定分类的模板，随机选一个渲染
        material 参数：选中的材料key，用于计算 material_name/material_price 等变量
        order_desc 参数：订单描述字符串，用于 {{order_desc}} 占位符
        extra_vars 参数：额外的模板变量字典
        max_index 参数：只从前 N 条模板里随机选（用于不同场景选不同话术池）
        """
        for hot_q in self.templates.get("hot_questions", []):
            if hot_q.get("category") == category_name:
                templates_list = hot_q.get("templates", [])
                if templates_list:
                    # max_index 限制选取范围（从前面几条里选）
                    if max_index and max_index < len(templates_list):
                        template_str = random.choice(templates_list[:max_index])
                    else:
                        template_str = random.choice(templates_list)
                    # 如果有材料信息，组装额外变量
                    vars_dict = {}
                    if extra_vars:
                        vars_dict.update(extra_vars)
                    if order_desc:
                        vars_dict["order_desc"] = order_desc
                    if material:
                        vars_dict["material_name"] = get_material_name(material, self.config)
                        vars_dict["material_price"] = get_material_price(self.config, material)
                        vars_dict["price_method"] = self.config.get("_base_price_method", "投影面积")
                        vars_dict["hardware"] = self.config.get("hardware_brand", "")
                        vars_dict["edge_band"] = self.config.get("edge_band", "")
                        vars_dict["price_range_low"], vars_dict["price_range_high"] = get_price_range(self.config)
                        vars_dict["materials_list"] = get_materials_list_text(self.config)
                        # 当前材料的品牌（覆盖全局 board_brand，避免价值塑造/详情回答里显示错材料）
                        mat_brand = get_material_brand(material, self.config)
                        if mat_brand:
                            vars_dict["board_brand"] = mat_brand

                    # 厨房/橱柜场景特殊计价：延米 + 台面另算
                    # ponytail: 从历史消息和当前上下文中检测厨房场景，动态替换计价方式
                    kitchen_keywords = ["厨房", "橱柜", "厨柜", "灶台", "厨房柜", "厨房柜子"]
                    has_kitchen = False
                    # 检查extra_vars里有没有场景信息
                    scene_name = vars_dict.get("scene_name", "")
                    if scene_name and any(kw in scene_name for kw in kitchen_keywords):
                        has_kitchen = True
                    # 检查历史消息
                    if not has_kitchen:
                        for h in self.history:
                            if any(kw in h.get("user", "") for kw in kitchen_keywords):
                                has_kitchen = True
                                break
                    if has_kitchen:
                        kitchen_cfg = self.config.get("_kitchen_pricing", {})
                        if kitchen_cfg:
                            vars_dict["price_method"] = kitchen_cfg.get("price_method", "延米")
                            vars_dict["price_unit"] = "一延米"
                            ct_material = kitchen_cfg.get("countertop_material", "石英石台面")
                            ct_price = kitchen_cfg.get("countertop_price", 680)
                            vars_dict["countertop_note"] = f"\n对了，橱柜是柜体价，{ct_material}{ct_price}元/米另算哈。"
                        else:
                            vars_dict["price_unit"] = "一平"
                            vars_dict["countertop_note"] = ""
                    else:
                        vars_dict["price_unit"] = "一平"
                        vars_dict["countertop_note"] = ""
                    # 价格区间模板也需要这些变量
                    if category_name == "bargain_price_range":
                        vars_dict["price_range_low"], vars_dict["price_range_high"] = get_price_range(self.config)
                        vars_dict["materials_list"] = get_materials_list_text(self.config)
                    return self._render_with_extra(template_str, vars_dict)
        # 找不到兜底
        return "价格好商量，您具体有什么需求？"

    def _render_with_extra(self, template_str, extra_vars):
        """用额外变量渲染模板（在 _vars 基础上再加）"""
        from jinja2 import Template
        vars_dict = self._vars()
        if extra_vars:
            vars_dict.update(extra_vars)
        return Template(template_str).render(**vars_dict)

    def _is_price_question(self, text):
        """判断是不是纯问价类问题（多少钱/怎么卖/价格/报价等）"""
        price_keywords = [
            "多少钱", "怎么卖", "什么价", "价格", "价位",
            "报价", "费用", "怎么收费", "咋卖", "多钱",
            "价钱", "单价", "多少钱一平", "多少钱一米",
        ]
        return any(kw in text for kw in price_keywords)

    def _detect_material_choice(self, text):
        """
        识别用户选择了哪种材料
        返回材料key，识别不到返回 None
        关键词从配置的 _board_keywords_map 动态读取，不写死
        """
        keywords_map = self.config.get("_board_keywords_map", {})
        if not keywords_map:
            # ponytail: 兼容旧配置，没有动态映射就用硬编码兜底
            keywords_map = {
                "particle_board": ["颗粒板", "刨花板"],
                "multi_layer_board": ["多层板", "胶合板"],
                "osb_board": ["OSB板", "欧松板", "osb", "OSB"],
                "ecological_board": ["生态板", "免漆板"],
                "solid_wood": ["实木", "原木板"],
            }

        best_material = None
        best_count = 0
        for mat_key, keywords in keywords_map.items():
            count = sum(1 for kw in keywords if kw in text)
            if count > best_count:
                best_count = count
                best_material = mat_key

        return best_material if best_count > 0 else None

    def _is_confirmation_question(self, text):
        """
        判断是不是确认类追问（"是X吗"、"你说的X吧"、"对吗"这类）
        核心特征：用户在确认某个具体东西（材料/品牌等），而不是问开放式事实问题
        是返回 True，否则返回 False
        """
        # 模式1："你说的...吗/吧" 明确确认
        if "你说的" in text and ("吗" in text or "吧" in text):
            return True
        
        # 模式2："是X吗/是X吧" 开头是确认
        if text.startswith("是") and ("吗" in text or "吧" in text):
            return True
        
        # 模式3："对吗/是不是/是吧/对吧" 纯确认词
        confirm_words = ["对吗", "是不是", "是吧", "对吧", "没错吧", "不？", "不?"]
        if any(w in text for w in confirm_words):
            return True
        
        # 模式4：材料名 + 吗/吧 → 确认材料（比如"多层板吗"、"颗粒板吧"）
        # ponytail: 只有确认具体材料才算，纯属性提问（环保吗/贵吗）不算
        mat = self._detect_material_choice(text)
        if mat and ("吗" in text or "吧" in text or "？" in text or "?" in text):
            return True
        
        return False

    def _extract_confirm_item(self, text):
        """
        从确认类追问中提取用户问的是什么东西
        返回：{"type": "material", "key": "xxx", "name": "xxx"} 或 None
        目前支持：材料类确认
        """
        # 先试试材料
        mat_key = self._detect_material_choice(text)
        if mat_key:
            return {
                "type": "material",
                "key": mat_key,
                "name": get_material_name(mat_key, self.config),
            }
        # 环保类确认
        if "环保" in text:
            return {"type": "eco", "key": "eco", "name": "环保"}
        # 封边类确认
        if "封边" in text:
            return {"type": "edge_band", "key": "edge_band", "name": "封边"}
        # 五金类确认
        if "五金" in text:
            return {"type": "hardware", "key": "hardware", "name": "五金"}
        return None

    def _bot_mentioned_item(self, bot_text, item):
        """
        判断上一轮客服回答中是否提到过某个东西
        bot_text: 上一轮客服回答文本
        item: _extract_confirm_item 返回的字典
        返回 True/False
        """
        if not bot_text or not item:
            return False
        item_type = item["type"]
        if item_type == "material":
            # 材料名关键词都算提到过（中文名、价格数字等）
            mat_name = item["name"]
            if mat_name in bot_text:
                return True
            # 价格数字也算（比如"799"对应颗粒板）
            mat_price = str(get_material_price(self.config, item["key"]))
            if mat_price and mat_price in bot_text:
                return True
            return False
        elif item_type == "eco":
            return "环保" in bot_text or "ENF" in bot_text or "E0" in bot_text
        elif item_type == "edge_band":
            return "封边" in bot_text
        elif item_type == "hardware":
            return "五金" in bot_text
        return False

    def _get_confirm_answer(self, item, is_yes):
        """
        渲染确认类追问的回答
        item: _extract_confirm_item 返回的字典
        is_yes: True=肯定回答，False=否定回答
        返回回答字符串
        """
        tpl_list = self.templates.get("confirm_yes_templates" if is_yes else "confirm_no_templates", [])
        if not tpl_list:
            # ponytail: 模板没配置就用兜底话术，不报错
            if is_yes:
                return f"对的，就是{item['name']}。"
            return "不是的，我说的不是这个。"
        template_str = random.choice(tpl_list)
        # 否定回答需要纠正项（用上一轮推荐的材料或默认材料）
        extra_vars = {"item": item["name"]}
        if not is_yes:
            # 用当前选中的材料作为纠正项，没有就用主推款
            correct_mat = self.selected_material or self.config.get("main_material", "multi_layer_board")
            correct_name = get_material_name(correct_mat, self.config)
            extra_vars["correct_item"] = correct_name
        return Template(template_str).render(**self._vars(), **extra_vars)

    def _detect_room_type(self, text):
        """
        检测用户说的是哪个房间/使用场景（场景化材料推荐用）
        返回：(scene_key, scene_name)，检测不到返回 (None, None)
        优先级高的在前（比如"儿童衣柜"应该命中儿童房而不是卧室）
        """
        # 按优先级排序（更具体的场景放前面）
        # ponytail: 12个场景key必须和推荐矩阵完全一致（weimusi_recommend_matrix.json）
        room_patterns = [
            ("whole_house", [
                "全屋定制", "全屋", "整套", "整体", "全套", "整屋", "家里全部",
            ]),
            ("kids_room", [
                "儿童房", "小孩房", "宝宝房", "孩子房间", "儿童衣柜", "孩子用",
                "儿童", "小孩", "宝宝", "儿子房", "女儿房",
            ]),
            ("kitchen", [
                "厨房", "橱柜", "厨柜", "厨房柜子", "灶台", "吊柜",
            ]),
            ("bathroom", [
                "卫生间", "洗手间", "卫浴柜", "浴室柜", "洗手台", "卫浴",
            ]),
            ("balcony", [
                "阳台", "阳台柜", "洗衣柜", "洗衣机柜", "晾衣架",
            ]),
            ("basement", [
                "地下室", "室外储物柜", "户外柜", "室外柜", "地下室储物柜",
                "露台柜", "庭院柜",
            ]),
            ("tv_cabinet", [
                "电视柜", "电视背景墙", "背景墙柜", "影视墙",
            ]),
            ("dining", [
                "餐边柜", "酒柜", "餐厅柜", "茶水柜", "餐厅酒柜",
            ]),
            ("bookcase", [
                "书柜", "书架", "展示柜", "陈列柜", "文件柜",
            ]),
            ("living_room", [
                "客厅柜", "客厅储物柜", "客厅收纳柜",
            ]),
            ("shoe_cabinet", [
                "鞋柜", "门厅柜", "入户柜",
            ]),
            ("entrance", [
                "玄关柜", "玄关", "入户玄关",
            ]),
            ("wardrobe", [
                "卧室", "主卧", "次卧", "衣柜", "大衣柜", "衣帽间",
                "大衣橱", "衣橱", "卧室衣柜", "主卧衣柜", "次卧衣柜",
            ]),
        ]

        for room_key, keywords in room_patterns:
            if any(kw in text for kw in keywords):
                scene_name = self._get_scene_name(room_key)
                return room_key, scene_name
        return None, None

    def _detect_room_types(self, text):
        """
        检测用户提到的所有房间/使用场景（返回列表，支持多场景）
        返回：list of (scene_key, scene_name)，按命中顺序排列
        - 去重：同一个场景不重复返回
        - 数量上限：4个（太多了话术太长）
        - 全屋优先：如果命中了 whole_house，直接返回全屋单个场景（不拆）
        """
        # ponytail: 12个场景key必须和推荐矩阵完全一致（weimusi_recommend_matrix.json）
        # 顺序：全屋优先 > 儿童房 > 厨卫阳台 > 具体柜子 > 鞋柜玄关 > 衣柜
        room_patterns = [
            ("whole_house", [
                "全屋定制", "全屋", "整套", "整体", "全套", "整屋", "家里全部",
            ]),
            ("kids_room", [
                "儿童房", "小孩房", "宝宝房", "孩子房间", "儿童衣柜", "孩子用",
                "儿童", "小孩", "宝宝", "儿子房", "女儿房",
            ]),
            ("kitchen", [
                "厨房", "橱柜", "厨柜", "厨房柜子", "灶台", "吊柜",
            ]),
            ("bathroom", [
                "卫生间", "洗手间", "卫浴柜", "浴室柜", "洗手台", "卫浴",
            ]),
            ("balcony", [
                "阳台", "阳台柜", "洗衣柜", "洗衣机柜", "晾衣架",
            ]),
            ("basement", [
                "地下室", "室外储物柜", "户外柜", "室外柜", "地下室储物柜",
                "露台柜", "庭院柜",
            ]),
            ("tv_cabinet", [
                "电视柜", "电视背景墙", "背景墙柜", "影视墙",
            ]),
            ("dining", [
                "餐边柜", "酒柜", "餐厅柜", "茶水柜", "餐厅酒柜",
            ]),
            ("bookcase", [
                "书柜", "书架", "展示柜", "陈列柜", "文件柜",
            ]),
            ("living_room", [
                "客厅柜", "客厅储物柜", "客厅收纳柜",
            ]),
            ("shoe_cabinet", [
                "鞋柜", "门厅柜", "入户柜",
            ]),
            ("entrance", [
                "玄关柜", "玄关", "入户玄关",
            ]),
            ("wardrobe", [
                "卧室", "主卧", "次卧", "衣柜", "大衣柜", "衣帽间",
                "大衣橱", "衣橱", "卧室衣柜", "主卧衣柜", "次卧衣柜",
            ]),
        ]

        results = []
        seen_keys = set()

        for room_key, keywords in room_patterns:
            if any(kw in text for kw in keywords):
                # 全屋优先，命中了就直接返回单个全屋
                if room_key == "whole_house":
                    scene_name = self._get_scene_name(room_key)
                    return [(room_key, scene_name)]
                if room_key not in seen_keys:
                    scene_name = self._get_scene_name(room_key)
                    results.append((room_key, scene_name))
                    seen_keys.add(room_key)
                    if len(results) >= 10:
                        break

        return results

    def _detect_room_types_attrs(self, text):
        """
        场景检测增强版：返回场景+属性（支持LLM兜底）
        返回：list of dict，每个dict含 {scene_key, scene_name, attributes: []}
        attributes 可能的值："outdoor"（室外）、"kids"（儿童用）、"wet"（潮湿环境）等
        
        三层架构：
        1. 关键词快速匹配（覆盖常见说法）
        2. LLM语义识别兜底（长尾说法 + 属性识别）
        3. 降级：LLM失败只用关键词结果
        """
        # 第一层：关键词匹配（快速通道）
        keyword_results = self._detect_room_types(text)
        results = []
        seen_keys = set()
        for scene_key, scene_name in keyword_results:
            results.append({
                "scene_key": scene_key,
                "scene_name": scene_name,
                "attributes": [],  # 先置空，下面统一补全
            })
            seen_keys.add(scene_key)

        # 关键词级别的属性补全（室外/户外/儿童等常见修饰词）
        # ponytail: 不管走不走LLM兜底，先把明显的属性用关键词标上，避免漏标
        # 注意：不能给所有场景都加属性，只有修饰词直接关联的场景才加
        outdoor_prefixes = ["室外", "户外", "露天"]
        kids_prefixes = ["小孩房", "儿童房", "孩子的", "小孩的"]

        # basement（地下室/室外储物柜）默认带outdoor属性（本身就是室外场景）
        for r in results:
            if r["scene_key"] == "basement" and "outdoor" not in r["attributes"]:
                r["attributes"].append("outdoor")

        # 遍历每个场景，检查原文中是否有"室外+场景名"这样的搭配
        for r in results:
            scene_name = r["scene_name"]
            scene_key = r["scene_key"]

            # outdoor: 检查是否有"室外+场景名"或"场景名+室外"的搭配
            for prefix in outdoor_prefixes:
                if prefix + scene_name in text or scene_name + prefix in text:
                    if "outdoor" not in r["attributes"]:
                        r["attributes"].append("outdoor")
                    break
            # 阳台场景的特殊修饰词（也得是短语匹配，不能文本里有室外就给阳台加）
            if scene_key == "balcony":
                balcony_outdoor_phrases = ["户外阳台", "露天阳台", "室外阳台", "露台阳台"]
                for phrase in balcony_outdoor_phrases:
                    if phrase in text:
                        if "outdoor" not in r["attributes"]:
                            r["attributes"].append("outdoor")
                        break

            # kids: 检查是否有"小孩+场景"或场景是kids_room且文本有儿童相关词
            if scene_key == "kids_room":
                if "kids" not in r["attributes"]:
                    r["attributes"].append("kids")
            else:
                for prefix in kids_prefixes:
                    if prefix + scene_name in text:
                        if "kids" not in r["attributes"]:
                            r["attributes"].append("kids")
                        break

        # 关键词命中 >=3个 → 直接返回，不调LLM（够多了，省时间）
        if len(results) >= 3:
            return results[:10]

        # 第二层：LLM语义兜底（补全漏识别的场景 + 识别属性）
        scene_prompt = """你是一个全屋定制客服的场景识别器。根据用户说的话，识别出提到的所有家具使用场景，以及每个场景的特殊属性。

可选场景（scene_key）：
- bedroom_wardrobe - 卧室/衣柜/衣帽间
- kitchen - 厨房/橱柜
- bathroom - 卫生间/浴室柜/洗手台
- balcony - 阳台/阳台柜/洗衣柜
- shoe_cabinet - 鞋柜/玄关柜/入户柜
- living_room - 客厅/电视柜/餐边柜
- kids_room - 儿童房/小孩房
- tatami - 榻榻米/地台/书房
- whole_house - 全屋定制/整套/整体
- basement - 地下室/室外储物柜（户外用）

可选属性（attributes，多选）：
- outdoor - 室外/户外/露天/风吹雨淋
- kids - 儿童用/孩子用/宝宝用
- wet - 潮湿环境/卫生间/阳台/厨房
- none - 无特殊属性

输出格式：JSON数组，每个元素包含 scene_key 和 attributes（数组）。不要输出其他文字。
示例：
输入："室外鞋柜"
输出：[{"scene_key":"shoe_cabinet","attributes":["outdoor"]}]

输入："小孩房衣柜和阳台柜"
输出：[{"scene_key":"kids_room","attributes":["kids"]},{"scene_key":"balcony","attributes":["wet","outdoor"]}]

输入："做橱柜和衣柜"
输出：[{"scene_key":"kitchen","attributes":["wet"]},{"scene_key":"bedroom_wardrobe","attributes":[]}]

注意：
- 如果用户说"全屋/整套/所有"，只返回 whole_house 一个场景
- 厨房、卫生间、阳台默认带 wet 属性
- 室外/户外/露天 的场景带 outdoor 属性
- 儿童/小孩/宝宝 的场景带 kids 属性
- 不要输出不在列表中的scene_key
只输出JSON数组，不要解释，不要其他文字。
"""

        try:
            import json
            llm_raw = self._llm_classify_raw(text, scene_prompt, cache_prefix="scene", timeout=8)
            if llm_raw:
                # 清理可能的markdown标记
                llm_raw = llm_raw.replace("```json", "").replace("```", "").strip()
                # 尝试找JSON数组
                import re
                m = re.search(r'\[.*\]', llm_raw, re.DOTALL)
                if m:
                    json_str = m.group()
                    llm_scenes = json.loads(json_str)
                    # 合法场景key列表
                    valid_scenes = [
                        "bedroom_wardrobe", "kitchen", "bathroom", "balcony",
                        "shoe_cabinet", "living_room", "kids_room", "tatami",
                        "whole_house", "basement",
                    ]
                    valid_attrs = ["outdoor", "kids", "wet"]
                    for item in llm_scenes:
                        sk = item.get("scene_key", "")
                        if sk not in valid_scenes:
                            continue
                        attrs = [a for a in item.get("attributes", []) if a in valid_attrs]
                        # 全屋优先
                        if sk == "whole_house":
                            scene_name = self._get_scene_name(sk)
                            return [{"scene_key": sk, "scene_name": scene_name, "attributes": attrs}]
                        if sk not in seen_keys:
                            scene_name = self._get_scene_name(sk)
                            results.append({
                                "scene_key": sk,
                                "scene_name": scene_name,
                                "attributes": attrs,
                            })
                            seen_keys.add(sk)
                            if len(results) >= 4:
                                break
                        else:
                            # 已有关键词命中的场景 → 合并属性
                            for r in results:
                                if r["scene_key"] == sk:
                                    for a in attrs:
                                        if a not in r["attributes"]:
                                            r["attributes"].append(a)
                                    break
                print(f"  [场景检测LLM兜底] LLM返回{len(llm_scenes) if 'llm_scenes' in dir() else 0}个场景，当前共{len(results)}个")
        except Exception as e:
            print(f"  [场景检测LLM兜底] 失败: {e}，使用关键词结果")

        return results[:4]

    def _llm_classify_raw(self, text, system_prompt, cache_prefix="llmraw", timeout=8):
        """
        LLM原始输出工具：返回LLM的原始文本（用于JSON输出等复杂分类）
        失败/超时返回None
        """
        import hashlib

        prev_user = ""
        prev_bot = ""
        if self.history:
            last = self.history[-1]
            prev_user = last.get("user", "")
            prev_bot = last.get("bot", "")

        cache_content = text.strip() + "|" + prev_user + "|" + prev_bot
        cache_key = f"{cache_prefix}:" + hashlib.md5(cache_content.encode("utf-8")).hexdigest()
        redis_client = self._get_redis()
        if redis_client:
            try:
                cached = redis_client.get(cache_key)
                if cached:
                    return cached
            except Exception:
                pass

        if prev_user and prev_bot:
            user_msg = (
                f"上一轮对话：\n"
                f"用户：{prev_user}\n"
                f"客服：{prev_bot}\n\n"
                f"当前用户说：{text}\n"
                f"输出："
            )
        else:
            user_msg = f"用户说：{text}\n输出："

        try:
            resp = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    "temperature": 0,
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()

            if redis_client and content:
                try:
                    redis_client.setex(cache_key, 7 * 24 * 3600, content)
                except Exception:
                    pass

            return content
        except Exception as e:
            print(f"[LLM原始输出降级] prefix={cache_prefix}, error={e}")
            return None

    def _detect_preference_type(self, text):
        """
        检测用户在选材料阶段的偏好类型
        返回：cost_effective / eco_friendly / balanced / quality / recommend_me / None
        优先级从高到低：环保 > 品质 > 性价比 > 求推荐 > 随便
        """
        # 环保关键词（优先级最高，安全第一）
        eco_keywords = [
            "环保", "孩子", "小孩", "孕妇", "无甲醛", "健康",
            "环保的", "环保点", "甲醛", "宝宝", "怀孕",
        ]
        # 品质关键词
        quality_keywords = [
            "好点的", "用好料", "品质好的", "高端", "最好的", "顶级", "一步到位",
            "好点", "好的", "高档", "上档次", "最好", "顶配",
            "档次高", "档次高点", "高档次", "档次高点的",
            "品质高点", "品质高点的", "品质高的",
            "好一点", "好一点的", "好点儿", "好点儿的",
            "要好的", "要高端", "要高档",
        ]
        # 性价比关键词
        cost_keywords = [
            "便宜", "性价比", "预算", "实惠", "省钱", "划算",
            "便宜点", "便宜的", "经济", "实惠点", "预算有限",
            "一般的就行", "普通的", "凑合用", "能用就行", "一般就行",
        ]
        # 求推荐关键词（用户主动让推荐，比"随便"更积极）
        recommend_keywords = [
            "你推荐", "推荐一下", "哪个好", "给点建议", "有什么推荐",
            "推荐个", "帮我选", "给推荐",
        ]
        # 平衡/随便关键词（用户无所谓，优先级最低）
        balanced_keywords = [
            "都行", "你看着办", "均衡", "都可以",
            "卖得最好", "最火的", "热门", "你觉得",
            "差不多", "随便", "都要", "差不多就行", "别太差", "都行你定", "一般吧",
        ]

        if any(kw in text for kw in eco_keywords):
            return "eco_friendly"
        if any(kw in text for kw in quality_keywords):
            return "quality"
        if any(kw in text for kw in cost_keywords):
            return "cost_effective"
        if any(kw in text for kw in recommend_keywords):
            return "recommend_me"
        if any(kw in text for kw in balanced_keywords):
            return "balanced"

        # 关键词没命中 → LLM语义兜底（二级防线）
        pref_prompt = """你是一个全屋定制客服的用户偏好识别器。根据用户说的话，判断用户在选材料时的偏好类型。

分类定义：
- cost_effective - 追求性价比、便宜、预算有限、划算、经济实惠
- eco_friendly - 关注环保、甲醛、健康、孩子/孕妇/宝宝用
- quality - 追求品质、高端、好点的、上档次、耐用、要好的、不想用太差的
- recommend_me - 用户主动让推荐、问哪个好、给点建议
- balanced - 用户说随便、都行、看着办、无所谓、都可以
- none - 完全看不出偏好倾向

只返回一个分类英文标签（小写），不要解释，不要其他文字。合法值：cost_effective / eco_friendly / quality / recommend_me / balanced / none
"""
        valid_prefs = ["cost_effective", "eco_friendly", "quality", "recommend_me", "balanced", "none"]
        llm_result = self._llm_classify(text, pref_prompt, valid_prefs, cache_prefix="pref", timeout=8)
        if llm_result and llm_result != "none" and llm_result in valid_prefs:
            print(f"  [偏好检测LLM兜底] 关键词未命中，LLM识别为: {llm_result}")
            return llm_result

        return None

    def _get_recommendation(self, text):
        """
        二维推荐主函数：场景 × 偏好 → 推荐材料 + 理由 + 场景跟进提问
        
        逻辑：
        1. 检测场景 _detect_room_type(text)
        2. 检测偏好 _detect_preference_type(text)
        3. 场景和偏好都有 → 查矩阵
        4. 只有场景，没有偏好 → 用 recommend_me 作为默认偏好
        5. 只有偏好，没有场景 → 用 default 作为默认场景（通用推荐，不脑补具体房间）
        6. 都没有 → 返回 None
        
        返回：dict 或 None
        {
            "material": "particle_board",      # 推荐材料key
            "material_name": "颗粒板",          # 材料中文名
            "material_price": 799,              # 材料单价
            "reason": "xxx",                    # 推荐理由
            "scene_key": "default",              # 场景key
            "scene_name": "卧室衣柜",            # 场景中文名
            "preference": "cost_effective",     # 偏好key
            "follow_up": "您衣柜大概做多大？",   # 场景化跟进提问
        }
        """
        # 有私有推荐矩阵 → 先走私有矩阵逻辑
        if self.private_rec_matrix:
            private_rec = self._get_private_recommendation(text)
            if private_rec:
                return private_rec

        scene_key, scene_name = self._detect_room_type(text)
        preference = self._detect_preference_type(text)

        # 都没有 → 走原有逻辑
        if not scene_key and not preference:
            return None

        # 默认场景：default（通用推荐，不脑补具体房间）
        if not scene_key:
            scene_key = "default"
            scene_name = self._get_scene_name(scene_key)
            if scene_name == "default":
                scene_name = "通用"

        # 默认偏好：recommend_me（用户让我们推荐）
        if not preference:
            preference = "recommend_me"

        # 查推荐矩阵
        matrix = self.rec_matrix.get("matrix", {})
        scene_config = matrix.get(scene_key, {})
        pref_config = scene_config.get(preference)

        # 矩阵里找不到，降级用推荐款偏好
        if not pref_config:
            pref_config = scene_config.get("recommend_me")
        if not pref_config:
            return None

        material_key = pref_config.get("material")
        reason = pref_config.get("reason", "")

        if not material_key:
            return None

        # 组装场景化跟进提问
        follow_ups = self.rec_matrix.get("scene_follow_up", {})
        follow_up = follow_ups.get(scene_key, follow_ups.get("default", "您大概做几个柜子啊？"))

        return {
            "material": material_key,
            "material_name": get_material_name(material_key, self.config),
            "material_price": get_material_price(self.config, material_key),
            "reason": reason,
            "scene_key": scene_key,
            "scene_name": scene_name,
            "preference": preference,
            "follow_up": follow_up,
        }

    # ---------- 私有推荐矩阵（全铝风格）----------
    # 引擎场景key → 私有矩阵场景key 的映射
    # ponytail: 现在引擎的12个scene_key和矩阵完全一致，大部分是1:1映射
    PRIVATE_SCENE_MAP = {
        "kitchen": "kitchen",
        "bathroom": "bathroom",
        "balcony": "balcony",
        "basement": "basement",
        "tv_cabinet": "tv_cabinet",
        "dining": "dining",
        "bookcase": "bookcase",
        "living_room": "living_room",
        "shoe_cabinet": "shoe_cabinet",
        "entrance": "entrance",
        "wardrobe": "wardrobe",
        "kids_room": "kids_room",
        "whole_house": "whole_house",
    }

    def _map_to_private_scene(self, scene_key):
        """把引擎场景key映射到私有矩阵场景key，映射不到返回None"""
        return self.PRIVATE_SCENE_MAP.get(scene_key)

    def _get_scene_name(self, scene_key):
        """
        获取场景中文名，优先从私有矩阵取，其次从公用矩阵取
        确保场景名和推荐矩阵一致，不会出现key当名字用的情况
        """
        # 优先从私有矩阵的scenes配置里取（最准确，和矩阵一致）
        if self.private_rec_matrix:
            scenes = self.private_rec_matrix.get("scenes", {})
            for scene_name, cfg in scenes.items():
                if cfg.get("scene_key") == scene_key:
                    return scene_name
        # 兜底：从公用rec_matrix取
        return self.rec_matrix.get("scene_names", {}).get(scene_key, scene_key)

    def _get_private_scene_config(self, scene_key):
        """从私有矩阵里拿到指定场景的配置，拿不到返回(None, None)"""
        if not self.private_rec_matrix:
            return None, None
        private_key = self._map_to_private_scene(scene_key)
        if not private_key:
            return None, None
        scenes = self.private_rec_matrix.get("scenes", {})
        for scene_name, cfg in scenes.items():
            if cfg.get("scene_key") == private_key:
                return cfg, scene_name
        return None, None

    def _get_private_material_info(self, mat_key):
        """从私有矩阵meta里拿材料名和价格"""
        if not self.private_rec_matrix:
            return None, None
        mats = self.private_rec_matrix.get("meta", {}).get("materials", {})
        info = mats.get(mat_key)
        if not info:
            return None, None
        return info.get("name"), info.get("price")

    def _pick_private_material(self, scene_cfg, preference):
        """
        根据场景配置 + 用户偏好，从私有矩阵里选主推板材
        返回：(mat_key, reason)
        偏好映射：
        - cost_effective → can_do/recommend 里最便宜的
        - eco_friendly / balanced → main_push
        - quality → carbon（碳脂板，前提是 recommend 或 strong_recommend）
        - recommend_me → main_push
        """
        if not scene_cfg:
            return None, ""

        main_push = scene_cfg.get("main_push")
        reason = scene_cfg.get("reason", "")

        # 品质/颜值偏好 → 只有carbon是strong_recommend才首推碳脂板（门面三件套）
        # carbon=recommend的场景（实用区）→ 还是主推焊接大板
        if preference == "quality":
            carbon_level = scene_cfg.get("carbon", "not_recommend")
            if carbon_level == "strong_recommend":
                return "carbon", reason
            # recommend 及以下 → 主推款（焊接大板等）
            if main_push:
                return main_push, reason
            return None, ""

        # 性价比偏好 → 找 recommend/strong_recommend 级别里最便宜的（can_do只是能做，不主动推荐）
        if preference == "cost_effective":
            mats = self.private_rec_matrix.get("meta", {}).get("materials", {})
            candidates = []
            for mat_key in ["spc", "honeycomb", "welded", "carbon"]:
                level = scene_cfg.get(mat_key, "not_recommend")
                if level in ("recommend", "strong_recommend"):
                    price = mats.get(mat_key, {}).get("price", 99999)
                    candidates.append((price, mat_key))
            if candidates:
                candidates.sort()
                chosen = candidates[0][1]
                # ponytail: 性价比选出来的可能不是主推款，不能硬套主推款的reason
                # 如果选的不是主推款，用通用理由，避免材料和理由对不上
                if chosen == main_push:
                    return chosen, reason
                mat_name = mats.get(chosen, {}).get("name", chosen)
                generic_reason = f"{mat_name}性价比不错，各方面都够用，家用挺合适的"
                return chosen, generic_reason
            # 兜底：如果没有recommend级别的，再看can_do
            for mat_key in ["spc", "honeycomb", "welded", "carbon"]:
                level = scene_cfg.get(mat_key, "not_recommend")
                if level == "can_do":
                    price = mats.get(mat_key, {}).get("price", 99999)
                    candidates.append((price, mat_key))
            if candidates:
                candidates.sort()
                chosen = candidates[0][1]
                if chosen == main_push:
                    return chosen, reason
                mat_name = mats.get(chosen, {}).get("name", chosen)
                generic_reason = f"{mat_name}价格实惠，预算有限可以选这个"
                return chosen, generic_reason
            # 再兜底用主推
            if main_push:
                return main_push, reason
            return None, ""

        # 环保/耐用/推荐我/平衡/没偏好 → 都用主推款
        if main_push:
            return main_push, reason
        return None, ""

    def _pick_private_material_v2(self, scene_cfg, preference, is_outdoor=False):
        """
        私有矩阵多档推荐（用于品质偏好下的两档方案）
        返回：list of dict，按推荐优先级排序
        [
            {"material": "welded", "level": "main", "reason": "..."},
            {"material": "carbon", "level": "upgrade", "reason": "..."},
        ]
        level: main（主力推荐）/ upgrade（升级推荐）
        
        注意：室外场景（is_outdoor=True）不能升级碳脂板，只返回main一档
        """
        if not scene_cfg:
            return []

        main_push = scene_cfg.get("main_push")
        reason = scene_cfg.get("reason", "")
        mats = self.private_rec_matrix.get("meta", {}).get("materials", {})

        result = []

        # 主力档：主推款（所有场景都有）
        if main_push:
            main_name = mats.get(main_push, {}).get("name", main_push)
            result.append({
                "material": main_push,
                "material_name": main_name,
                "level": "main",
                "reason": reason,
            })

        # 升级档：品质偏好 + 非室外 + 碳脂板在recommend级以上
        if preference == "quality" and not is_outdoor:
            carbon_level = scene_cfg.get("carbon", "not_recommend")
            if carbon_level in ("strong_recommend", "recommend"):
                carbon_name = mats.get("carbon", {}).get("name", "碳脂板")
                result.append({
                    "material": "carbon",
                    "material_name": carbon_name,
                    "level": "upgrade",
                    "reason": "颜值更高，质感更好，追求品质的客户首选",
                })

        return result

    def _get_private_recommendation(self, text):
        """
        私有矩阵单场景推荐入口
        返回格式与 _get_recommendation 保持一致，方便上层调用
        """
        if not self.private_rec_matrix:
            return None

        scene_key, scene_name = self._detect_room_type(text)
        preference = self._detect_preference_type(text)

        # 室外场景检测：如果带outdoor属性，强制映射到basement（室外储物柜）
        # ponytail: 室外环境只能用焊接大板，不能让碳脂板出场，这个逻辑写死
        room_attrs = self._detect_room_types_attrs(text)
        if room_attrs:
            first = room_attrs[0]
            attrs = first.get("attributes", [])
            if "outdoor" in attrs:
                scene_key = "basement"
                scene_name = "室外储物柜"

        # 都没有 → 返回None
        if not scene_key and not preference:
            return None

        # 没有场景 → 用全屋作为默认场景
        if not scene_key:
            scene_key = "whole_house"
            scene_name = "全屋定制"

        # 没有偏好 → 默认推荐我
        if not preference:
            preference = "recommend_me"

        scene_cfg, private_scene_name = self._get_private_scene_config(scene_key)
        if not scene_cfg:
            # 场景映射不到私有矩阵，返回None让上层走公用逻辑
            return None

        mat_key, reason = self._pick_private_material(scene_cfg, preference)
        if not mat_key:
            return None

        mat_name, mat_price = self._get_private_material_info(mat_key)
        if not mat_name:
            return None

        # 跟进提问（用价格摸底的话术，推进到Step2）
        follow_up = f"您家{private_scene_name}大概多大面积呀？我给您算个详细报价。"

        # 混搭方案
        mix_match = scene_cfg.get("mix_match", "")

        return {
            "material": mat_key,
            "material_name": mat_name,
            "material_price": mat_price,
            "reason": reason,
            "scene_key": scene_key,
            "scene_name": private_scene_name,
            "preference": preference,
            "follow_up": follow_up,
            "mix_match": mix_match,
            "is_private": True,
        }

    # ---------- 多场景分场景推荐 ----------
    def _join_scene_names(self, scene_names):
        """
        拼接场景名列表为口语化字符串
        1个：直接返回
        2个：A和B
        3-4个：A、B和C
        5个以上：A、B等N个地方
        """
        n = len(scene_names)
        if n == 0:
            return ""
        if n == 1:
            return scene_names[0]
        if n == 2:
            return f"{scene_names[0]}和{scene_names[1]}"
        if n <= 4:
            return "、".join(scene_names[:-1]) + f"和{scene_names[-1]}"
        # 5个以上
        return f"{scene_names[0]}、{scene_names[1]}等{n}个地方"

    def _get_scene_recommendation(self, scene_key, preference="cost_effective", attributes=None):
        """
        查单个场景的推荐板材
        Args:
            scene_key: 场景key
            preference: 偏好类型
            attributes: 场景属性列表（如["outdoor"]），室外场景强制映射到basement
        返回：(material_key, material_name, material_price, reason) 或 (None, None, None, None)
        """
        from customer_service.shop_config_loader import get_material_name, get_material_price

        # 室外场景 → 强制映射到basement（室外储物柜），只能用焊接大板
        # ponytail: 室外环境只能用焊接大板，普通材料扛不住，这个逻辑写死
        effective_scene_key = scene_key
        if attributes and "outdoor" in attributes and self.private_rec_matrix:
            effective_scene_key = "basement"

        # 有私有矩阵 → 先走私有矩阵逻辑
        if self.private_rec_matrix:
            scene_cfg, _ = self._get_private_scene_config(effective_scene_key)
            if scene_cfg:
                mat_key, reason = self._pick_private_material(scene_cfg, preference)
                if mat_key:
                    mat_name, mat_price = self._get_private_material_info(mat_key)
                    if mat_name:
                        return mat_key, mat_name, mat_price, reason
            # 私有矩阵查不到 → 继续往下走公用矩阵逻辑

        matrix = self.rec_matrix.get("matrix", {})
        scene_cfg = matrix.get(scene_key, {})
        pref_cfg = scene_cfg.get(preference)

        if not pref_cfg:
            # 没有这个偏好的配置，降级用 cost_effective
            pref_cfg = scene_cfg.get("cost_effective")

        if not pref_cfg:
            return None, None, None, None

        material_key = pref_cfg.get("material")
        reason = pref_cfg.get("reason", "")
        material_name = get_material_name(material_key, self.config)
        material_price = get_material_price(self.config, material_key)

        return material_key, material_name, material_price, reason

    def _build_multi_scene_answer(self, scene_data_list, has_selected=False):
        """
        根据多场景推荐结果，生成口语化回答话术
        三种模式：全部一致 / 部分推翻 / 全部推翻
        分级输出：1-2个逐个说，3个以上按板材分组，5个以上精简归类

        scene_data_list: list of dict，每个包含:
            scene_key, scene_name, recommended_material, material_name,
            material_price, is_same_as_selected, reason
        has_selected: bool，用户是否已经选了板材（决定话术语气）

        返回：完整话术字符串
        """
        if not scene_data_list:
            return ""

        n = len(scene_data_list)
        has_upgrade = any(not s.get("is_same_as_selected", True) for s in scene_data_list)

        # 场景名列表
        scene_names = [s["scene_name"] for s in scene_data_list]

        # ===== 1-2个场景：逐个推荐 =====
        if n <= 2:
            lines = []
            for i, s in enumerate(scene_data_list):
                if has_selected and s["is_same_as_selected"]:
                    # 适合已选板材
                    line = f"{s['scene_name']}用{s['material_name']}没问题，{s['reason']}。"
                elif has_selected and not s["is_same_as_selected"]:
                    # 不适合，推荐换
                    line = (
                        f"但{s['scene_name']}我建议用{s['material_name']}，"
                        f"{s['material_price']}一平，{s['reason']}。"
                    )
                else:
                    # 没有已选，直接推荐
                    line = (
                        f"{s['scene_name']}用{s['material_name']}，"
                        f"{s['material_price']}一平，{s['reason']}。"
                    )
                lines.append(line)

            answer = "\n".join(lines)

        # ===== 3个及以上：按板材分组 =====
        elif n <= 4:
            # 按推荐板材分组
            groups = {}
            for s in scene_data_list:
                mk = s["recommended_material"]
                if mk not in groups:
                    groups[mk] = {
                        "material_name": s["material_name"],
                        "material_price": s["material_price"],
                        "scenes": [],
                        "reason": s["reason"],
                    }
                groups[mk]["scenes"].append(s["scene_name"])

            lines = ["我给您分两类说："]
            for _, g in groups.items():
                scenes_text = "、".join(g["scenes"])
                reason = g["reason"]
                # 同组多个场景用更通用的理由
                if len(g["scenes"]) > 1:
                    reason = self._get_generic_reason(g["material_name"], scene_data_list)
                lines.append(
                    f"· {g['material_name']}（{g['material_price']}/平）：{scenes_text}用这个，{reason}"
                )

            answer = "\n".join(lines)

        # ===== 5个及以上：精简输出，按功能归类 =====
        else:
            # 简单分成两类：适合颗粒板的（干燥地方）和需要多层板的（潮湿地方）
            # 这里用板材价格区分：低价=干燥地方用，高价=潮湿地方用
            dry_scenes = []
            wet_scenes = []
            dry_material = None
            wet_material = None
            dry_price = None
            wet_price = None

            for s in scene_data_list:
                if s["recommended_material"] in ("particle_board", "osb", "ecological_board"):
                    dry_scenes.append(s["scene_name"])
                    if not dry_material:
                        dry_material = s["material_name"]
                        dry_price = s["material_price"]
                else:
                    wet_scenes.append(s["scene_name"])
                    if not wet_material:
                        wet_material = s["material_name"]
                        wet_price = s["material_price"]

            lines = ["您这柜子不少啊，我给您简单说下："]

            if dry_scenes:
                # 只说前两个代表，后面说等
                if len(dry_scenes) <= 3:
                    dry_text = "、".join(dry_scenes)
                else:
                    dry_text = f"{dry_scenes[0]}、{dry_scenes[1]}等干燥的地方"
                lines.append(f"· {dry_material}（{dry_price}/平）：{dry_text}都够用，性价比最高")

            if wet_scenes:
                if len(wet_scenes) <= 3:
                    wet_text = "、".join(wet_scenes)
                else:
                    wet_text = f"{wet_scenes[0]}等潮湿的地方"
                lines.append(f"· {wet_material}（{wet_price}/平）：{wet_text}得用这个，防潮耐用")

            lines.append("具体的等设计师上门给您细算，保证给您搭配好。")
            answer = "\n".join(lines)

        # ===== 结尾：确认 + 推进 =====
        if has_selected and has_upgrade:
            follow_up = "\n您看这个搭配行不？大概做多大？"
        elif has_selected and not has_upgrade:
            follow_up = f"\n{self._join_scene_names(scene_names)}用这个材料都没问题。您看可以不？大概做多大？"
        else:
            follow_up = "\n您看这个搭配可以不？大概做多大？"

        # ponytail: 话术拼接后统一清洗，避免重复句号、逗号接句号等小瑕疵
        full = answer + follow_up
        full = full.replace("。。", "。").replace("，。", "。")
        return full

    def _build_multi_scene_quality_answer(self, scene_data_list):
        """
        品质偏好多场景推荐话术生成（门面三件套首推碳脂板，其他主推焊接大板）

        三组分类：
        - face_scenes（颜值门面区）：carbon=strong_recommend 且 非室外 → 首推碳脂板
        - must_welded_scenes（必须焊接大板）：室外 或 carbon=can_do/not_recommend → 只能焊接大板
        - normal_scenes（常规实用区）：carbon=recommend → 主推焊接大板，可升级碳脂板

        话术结构：先颜值区（碳脂板首推），再实用区（焊接大板主推），
        最后室外单独强调 + 混搭建议
        """
        if not scene_data_list:
            return ""

        mats = self.private_rec_matrix.get("meta", {}).get("materials", {}) if self.private_rec_matrix else {}
        welded_name = mats.get("welded", {}).get("name", "全铝焊接大板")
        welded_price = mats.get("welded", {}).get("price", "980")
        carbon_name = mats.get("carbon", {}).get("name", "碳脂板")
        carbon_price = mats.get("carbon", {}).get("price", "1180")

        # === 三组分类 ===
        face_scenes = []       # 颜值门面区：carbon=strong_recommend + 非室外
        must_welded_scenes = [] # 必须焊接大板：室外 或 carbon<=can_do
        normal_scenes = []     # 常规实用区：carbon=recommend

        for s in scene_data_list:
            attrs = s.get("attributes", [])
            is_outdoor = "outdoor" in attrs
            scene_cfg, _ = self._get_private_scene_config(s["scene_key"])
            carbon_level = "not_recommend"
            if scene_cfg:
                carbon_level = scene_cfg.get("carbon", "not_recommend")

            scene_info = {
                "key": s["scene_key"],
                "name": s["scene_name"],
                "carbon_level": carbon_level,
                "is_outdoor": is_outdoor,
            }

            if is_outdoor or carbon_level in ("can_do", "not_recommend"):
                must_welded_scenes.append(scene_info)
            elif carbon_level == "strong_recommend":
                face_scenes.append(scene_info)
            else:  # recommend
                normal_scenes.append(scene_info)

        lines = []

        # === 开头 ===
        lines.append("追求品质的话，我给您说下怎么搭配最合适：")

        # === 第一档：颜值门面区 → 首推碳脂板 ===
        if face_scenes:
            face_names = "、".join([s["name"] for s in face_scenes])
            lines.append(f"· 【{carbon_name}】{carbon_price}一平：{face_names}这些门面位置我首推这个，质感接近实木，做出来效果最上档次，也是现在高端客户选得最多的。")

        # === 第二档：实用区 → 主推焊接大板 ===
        welded_scenes = normal_scenes + must_welded_scenes
        if welded_scenes:
            welded_names = "、".join([s["name"] for s in welded_scenes])
            if face_scenes:
                lines.append(f"剩下的{welded_names}这些我推荐用【{welded_name}】，{welded_price}一平，结实耐用防潮，零甲醛环保，性价比最高。")
            else:
                lines.append(f"· 【{welded_name}】{welded_price}一平：{welded_names}这些我推荐用这个，结实耐用防潮，零甲醛环保，性价比最高。")

        # === 室外场景单独强调 ===
        outdoor_only = [s for s in must_welded_scenes if s["is_outdoor"]]
        if outdoor_only:
            outdoor_names = "、".join([s["name"] for s in outdoor_only])
            # ponytail: 地下室和室外的卖点不一样，话术要区分
            has_outdoor = any("室外" in s["name"] or "户外" in s["name"] for s in outdoor_only)
            has_basement = any("地下室" in s["name"] for s in outdoor_only)
            if has_outdoor and not has_basement:
                detail = "风吹雨淋日晒的，只有焊接大板能扛得住，不建议用碳脂板，日晒容易老化"
            elif has_basement and not has_outdoor:
                detail = "常年潮湿不通风，木质的容易发霉变形，焊接大板最靠谱"
            else:
                detail = "潮湿+户外环境都得扛得住，只能用焊接大板才放心，其它板容易出问题"
            lines.append(f"  特别是{outdoor_names}，{detail}。")

        # === 混搭建议（实用区的场景都可以提混搭） ===
        # ponytail: 混搭范围扩大，不只是厨房浴室阳台，衣柜书柜等都可以
        mix_scenes = [s["name"] for s in normal_scenes]
        if mix_scenes:
            if len(mix_scenes) > 3:
                mix_text = "、".join(mix_scenes[:3]) + "等"
            else:
                mix_text = "、".join(mix_scenes)
            lines.append(f"  像{mix_text}这些地方，很多客户是柜体用{welded_name}+柜门用{carbon_name}混搭，既耐造又有颜值，性价比也不错。")

        # === 结尾跟进 ===
        lines.append("您看这个搭配可以不？大概做多大面积？")

        # ponytail: 话术拼接后统一清洗，避免重复句号、逗号接句号等小瑕疵
        # ponytail: 不用换行，改成一段式，避免飞书端每行首字被吃掉的显示bug
        # 每行去掉行首的列表符号/空格/缩进，用句号连接成一整段
        cleaned_segments = []
        for line in lines:
            # 去掉行首的列表符号、空格、全角空格、中间点等
            s = line.lstrip(" ·　")
            if s:
                cleaned_segments.append(s)
        answer = "。".join(cleaned_segments)
        # 清洗：问句结尾不用句号改、重复句号、句号+逗号等
        answer = answer.replace("？。", "？").replace("！。", "！")
        answer = answer.replace("。。", "。").replace("，。", "。")
        answer = answer.replace("：。", "：")
        return answer

    def _get_generic_reason(self, material_name, scene_data_list):
        """
        给同组板材生成一个通用理由（多个场景共用时）
        根据板材名简单判断
        """
        material_key = ""
        for s in scene_data_list:
            if s["material_name"] == material_name:
                material_key = s["recommended_material"]
                break

        reason_map = {
            "particle_board": "性价比最高，预算有限首选",
            "osb": "结构稳定，承重力强",
            "multi_layer_board": "防潮耐用，哪个房间都能用",
            "ecological_board": "环保等级最高，住得放心",
            "solid_wood": "质感最好，追求品质首选",
        }
        return reason_map.get(material_key, "各方面都不错，家用合适")

    def _multi_scene_recommendation(self, text, selected_material=None, preference=None):
        """
        多场景分场景推荐主函数

        输入：
          text: 用户输入文本
          selected_material: 已选板材key（用户已经选了的话）
          preference: 偏好类型（cost_effective/eco_friendly等，没有的话用性价比）

        返回：dict 或 None
        dict结构：
        {
          "scenes": [...],  # 每个场景的推荐详情
          "has_upgrade": bool,  # 是否有升级/更换推荐
          "answer": str,  # 生成好的完整话术
          "follow_up": str,  # 跟进提问
        }
        """
        # 1. 检测所有场景（带属性）
        room_data = self._detect_room_types_attrs(text)

        # 场景数 < 2 → 返回None，走单场景逻辑
        if len(room_data) < 2:
            return None

        # 全屋场景只有1个的话，也不走多场景
        if len(room_data) == 1 and room_data[0]["scene_key"] == "whole_house":
            return None

        # 2. 默认偏好 = 性价比（如果没指定）
        if not preference:
            preference = "cost_effective"

        # 3. 逐个场景查推荐
        scenes = []
        for room in room_data:
            scene_key = room["scene_key"]
            scene_name = room["scene_name"]
            attrs = room.get("attributes", [])
            mat_key, mat_name, mat_price, reason = self._get_scene_recommendation(
                scene_key, preference, attributes=attrs
            )
            if not mat_key:
                continue

            is_same = True
            if selected_material:
                is_same = (mat_key == selected_material)

            scenes.append({
                "scene_key": scene_key,
                "scene_name": scene_name,
                "recommended_material": mat_key,
                "material_name": mat_name,
                "material_price": mat_price,
                "is_same_as_selected": is_same,
                "reason": reason,
                "attributes": attrs,  # 保留属性，供话术层使用
            })

        if len(scenes) < 2:
            return None

        # 4. 生成话术
        has_selected = selected_material is not None
        has_upgrade = any(not s["is_same_as_selected"] for s in scenes)

        # 品质偏好 → 走两档方案话术
        if preference == "quality" and self.private_rec_matrix and not has_selected:
            answer = self._build_multi_scene_quality_answer(scenes)
            # 品质偏好下，主材料取焊接大板（主力推荐档，不是升级档）
            # 找到第一个有main_push的场景，取它的main_push作为selected_material
            first_main_mat = "welded"  # 默认焊接大板
            for s in scenes:
                scene_cfg, _ = self._get_private_scene_config(s["scene_key"])
                if scene_cfg and scene_cfg.get("main_push"):
                    first_main_mat = scene_cfg["main_push"]
                    break
            main_material = first_main_mat
        else:
            answer = self._build_multi_scene_answer(scenes, has_selected)
            main_material = scenes[0]["recommended_material"] if scenes else None

        return {
            "scenes": scenes,
            "has_upgrade": has_upgrade,
            "answer": answer,
            "follow_up": "您这些柜子加起来大概多大？",
            "main_material": main_material,
        }

    # ---------- 议价状态机：嫌贵/竞品 回怼检测 ----------
    def _detect_bargain_pushback(self, text):
        """
        检测用户在议价过程中的负面反馈（嫌贵/竞品对比）
        命中后返回对应话术，状态不推进
        砍价信号（要优惠/便宜点）不在这里处理，交给各Step判断是否推进
        返回：(类型, 话术) 或 None
        优先级：竞品 > 嫌贵（竞品最具体，先处理）
        """
        # 1. 竞品对比（别家/人家/才xx钱/比你们便宜）
        competitor_keywords = [
            "别家", "人家", "我刚问的", "我问了一家", "比你们便宜",
            "别人报的", "另一家", "我朋友家", "我邻居家", "别家才",
            "别家报", "人家才", "别人才", "便宜多了", "比你家便宜",
        ]
        has_competitor = any(kw in text for kw in competitor_keywords)
        # "才xx钱" / "xx钱就行" 模式检测
        if not has_competitor:
            import re
            if re.search(r"才\s*\d+\s*块", text) or re.search(r"才\s*\d+\s*钱", text) \
               or re.search(r"\d+\s*块\s*就行", text) or re.search(r"\d+\s*钱\s*就行", text):
                has_competitor = True

        if has_competitor:
            return "competitor", self._render_bargain_template("bargain_competitor_compare")

        # 2. 嫌贵（贵/太贵/这么贵/好贵等，不含砍价动作）
        expensive_keywords = [
            "太贵", "这么贵", "好贵", "有点贵", "价格高", "价格有点高",
            "不便宜", "真贵", "也太贵", "太贵了", "贵了点", "价钱高",
            "价钱有点高", "有点小贵",
        ]
        if any(kw in text for kw in expensive_keywords):
            return "expensive", self._render_bargain_template("bargain_value_build")

        return None

    def _is_end_conversation(self, text):
        """
        检测用户不想聊了的信号（议价流程中触发引导留资）
        """
        end_keywords = [
            "我考虑下", "先看看", "再说吧", "我想想", "不急",
            "以后再说", "再考虑", "先考虑", "我再看看", "考虑一下",
            "算了", "算了吧", "不考虑了", "先不", "暂时",
            "我先了解一下", "对比一下", "再对比", "我看看再说",
            "我先想想", "我先考虑", "先想一下", "再想想",
        ]
        return any(kw in text for kw in end_keywords)

    def _is_soft_close(self, text):
        """
        观望收尾检测：用户说考虑考虑/再想想等观望类话术
        （不是真的不聊了，是暂时观望，需要软挽留）
        """
        soft_close_keywords = [
            "考虑考虑", "再想想", "我先看看", "回去想想", "再比较比较",
            "再对比一下", "先不着急", "想想再说", "回家商量", "跟家人商量",
            "再了解了解", "先了解一下", "考虑一下再说", "考虑下再说",
            "我再想想", "先考虑考虑", "考虑好了找你", "等我消息",
        ]
        return any(kw in text for kw in soft_close_keywords)

    def _detect_contact_info(self, text):
        """
        留资检测：识别用户是否留了联系方式
        返回：phone / phone_invalid / wechat / address / appointment / None
        优先级：最高，放主逻辑最前面
        """
        import re

        # 0. 先提取纯数字串（用于判断是不是疑似手机号但位数不对）
        digits_match = re.search(r'\d{7,15}', text)
        has_digits = digits_match is not None
        digits_len = len(digits_match.group(0)) if digits_match else 0

        # 1. 手机号正则（11位，1开头第二位3-9）—— 正确格式，前后不能是数字
        if re.search(r'(?<!\d)1[3-9]\d{9}(?!\d)', text):
            return "phone"

        # 1.5 疑似手机号但格式不对（7-15位数字，但不符合正确手机号格式）
        # ponytail: 上下文是留资场景时，用户发一串数字大概率是手机号，
        # 如果位数不对/格式不对，要礼貌提醒，而不是当成没听懂
        if has_digits and digits_len >= 7 and digits_len <= 15 and digits_len != 11:
            # 再确认下不是固话（固话0开头带区号的前面已经匹配过就不会走到这）
            # 同时排除价格数字（几百几千那种太短的已经被7位过滤了）
            # 如果有电话/留资相关关键词，或者上下文是留资场景，判定为错误手机号
            phone_context_keywords = [
                "电话", "手机", "联系方式", "留个", "我的号码", "我号码",
                "手机号", "微信号", "加微信", "联系我", "打给我",
            ]
            has_phone_context = any(kw in text for kw in phone_context_keywords)
            # 10位或12位的纯数字，非常像手机号（可能多/少写了一位），即使没关键词也算
            looks_like_phone = (digits_len == 10 or digits_len == 12) and not text.startswith('0')
            if has_phone_context or looks_like_phone:
                return "phone_invalid"

        # 2. 座机号（带区号的固话）
        if re.search(r'0\d{2,3}[ -]?\d{7,8}', text):
            return "phone"

        # 3. 电话/联系方式关键词
        phone_keywords = [
            "电话", "手机号", "手机", "联系方式", "联系我", "打给我",
            "我电话", "给我打", "留个电话", "留个联系方式",
        ]
        if any(kw in text for kw in phone_keywords):
            return "phone"

        # 4. 微信相关（主动留微信 / 让加微信）
        # 注意："你微信多少"这种问的不算，是正常咨询
        wechat_keywords = [
            "加我微信", "我微信", "我v信", "我vx", "加我vx", "加我v",
            "加我吧", "你加我", "我加你", "微信号是", "我微信号",
        ]
        if any(kw in text for kw in wechat_keywords):
            return "wechat"

        # 5. 确认加了/通过一下
        confirm_keywords = [
            "加了", "加你了", "通过一下", "同意一下", "发过去了", "申请了",
            "好友申请", "加你微信了",
        ]
        if any(kw in text for kw in confirm_keywords):
            return "wechat"

        # 6. 地址/约量房/上门
        # 注意：区分"主动留地址/约时间" vs "咨询量房的事"
        # 有明显疑问词且没有主动留资信号的 → 不算留资
        address_keywords = [
            "地址", "上门", "量房", "过来看看", "到店", "去你们那",
            "什么时候有空", "约一下", "约个时间", "来我家", "我在",
            "我住", "小区", "来量房", "上门量", "免费量房",
            "过去看看", "到店里", "你们店",
        ]
        if any(kw in text for kw in address_keywords):
            # 排除纯咨询句式（有疑问词且没有留资信号）
            question_words = ["吗", "怎么", "什么", "哪", "多少", "呢", "几", "要不要", "能不能", "多久", "多远", "多大"]
            has_question = any(qw in text for qw in question_words)
            # 主动留资信号：用户在提供信息，不是在问
            leave_signals = ["我在", "我住", "我家在", "地址是", "给你地址", "过来量",
                            "来量房吧", "约一下", "约个时间", "什么时候有空",
                            "发地址", "地址给你", "我那", "小区叫"]
            has_leave_signal = any(s in text for s in leave_signals)
            if has_question and not has_leave_signal:
                return None  # 是在咨询，不是留资
            return "address"

        return None

    # ---------- 信息收集与去重 ----------
    def _extract_collected_info(self, text):
        """
        从用户消息中提取可收集的信息，存入 collected_info
        策略：正则优先（手机号、面积等），关键词兜底
        原则：只增不改，置信度不够宁可不存；用户明确改口则覆盖

        Args:
            text: 用户当前输入文本
        Returns:
            dict: 本轮新提取到的字段 {field: value}，没提取到返回空dict
        """
        import re
        new_info = {}

        # 1) 手机号（最高置信度，正则精准匹配）
        phone_match = re.search(r'(?<!\d)1[3-9]\d{9}(?!\d)', text)
        if phone_match:
            new_info["phone"] = phone_match.group(0)

        # 2) 面积（带单位的数字）
        # 2.1 模糊范围：7-8平 → 取平均值
        fuzzy_area = re.search(
            r'(\d+)(?:\s+|[-~到至])(\d+)\s*(?:个)?\s*(?:平方|平米|平|㎡)', text
        )
        if fuzzy_area:
            low = float(fuzzy_area.group(1))
            high = float(fuzzy_area.group(2))
            new_info["area"] = round((low + high) / 2, 1)
        else:
            # 2.2 精确数字 + 单位
            area_match = re.search(
                r'(\d+(?:\.\d+)?)\s*(?:个)?\s*(?:平方|平米|平|㎡)', text
            )
            if area_match:
                new_info["area"] = float(area_match.group(1))

        # 3) 柜子类型（关键词匹配）
        cabinet_keywords = {
            "全屋": ["全屋定制", "整屋", "全套", "家里全部"],
            "衣柜": ["衣柜", "衣橱", "衣帽间"],
            "橱柜": ["橱柜", "厨柜", "厨房柜", "厨房柜子"],
            "鞋柜": ["鞋柜"],
            "酒柜": ["酒柜"],
            "书柜": ["书柜"],
            "电视柜": ["电视柜"],
        }
        for cab_type, keywords in cabinet_keywords.items():
            if any(kw in text for kw in keywords):
                new_info["cabinet_type"] = cab_type
                break

        # 4) 风格偏好
        style_keywords = {
            "现代简约": ["现代简约", "简约", "现代风", "极简"],
            "北欧": ["北欧", "北欧风", "ins风"],
            "新中式": ["新中式", "中式", "中式风"],
            "轻奢": ["轻奢", "奢华"],
            "美式": ["美式", "美式风格"],
            "欧式": ["欧式", "欧式风格"],
            "日式": ["日式", "原木风", "日系"],
        }
        for style, keywords in style_keywords.items():
            if any(kw in text for kw in keywords):
                new_info["style_preference"] = style
                break

        # 5) 材质偏好
        material_keywords = {
            "颗粒板": ["颗粒板", "刨花板"],
            "多层板": ["多层板", "胶合板", "多层实木板"],
            "欧松板": ["欧松板", "OSB", "osb"],
            "密度板": ["密度板", "中纤板", "MDF"],
            "实木板": ["实木板", "纯实木"],
            "生态板": ["生态板", "免漆板"],
        }
        for mat, keywords in material_keywords.items():
            if any(kw in text for kw in keywords):
                new_info["material"] = mat
                break

        # 6) 预算（带"万"/"元"单位）
        budget_match = re.search(
            r'(\d+(?:\.\d+)?)\s*万(?:元)?(?:左右|上下|以内|预算|块钱)?', text
        )
        if budget_match:
            new_info["budget"] = budget_match.group(1) + "万"
        else:
            budget_match2 = re.search(
                r'预算(?:大概|大约|是)?\s*[：:]?\s*(\d+(?:\.\d+)?)\s*万', text
            )
            if budget_match2:
                new_info["budget"] = budget_match2.group(1) + "万"

        # 7) 姓名（置信度较低，只在明确语境下提取）
        # 只匹配 "我叫XX" "我姓XX" "叫我XX" 这种明确句式
        name_patterns = [
            r'我叫([\u4e00-\u9fa5]{2,4})',
            r'我姓([\u4e00-\u9fa5]{1,2})',
            r'叫我([\u4e00-\u9fa5]{2,4})',
            r'我是([\u4e00-\u9fa5]{2,4})',
        ]
        for pat in name_patterns:
            name_match = re.search(pat, text)
            if name_match:
                new_info["name"] = name_match.group(1)
                break

        # 8) 微信（明确留微信的语境）
        wechat_patterns = [
            r'我微信[是：:]*([a-zA-Z0-9_-]{5,20})',
            r'微信号[是：:]*([a-zA-Z0-9_-]{5,20})',
            r'加我微信([a-zA-Z0-9_-]{5,20})',
        ]
        for pat in wechat_patterns:
            wx_match = re.search(pat, text)
            if wx_match:
                new_info["wechat"] = wx_match.group(1)
                break

        # 9) 小区（XX小区/XX花园/XX苑/XX府 等）
        # 置信度中等，匹配常见小区后缀
        community_suffixes = ["小区", "花园", "苑", "府", "城", "园", "里", "邨", "湾", "郡"]
        for suffix in community_suffixes:
            if suffix in text:
                # 从后缀往前找最近的2-8个汉字/字母/数字
                idx = text.index(suffix)
                # 往前截取，从第一个非汉字字母数字的地方断开
                start = idx
                while start > 0 and (text[start-1].isalnum() or '\u4e00' <= text[start-1] <= '\u9fa5'):
                    start -= 1
                comm_name = text[start:idx + len(suffix)]
                # 过滤掉明显的前缀词
                bad_prefixes = ["我家在", "你家在", "他家在", "在", "是", "有", "去", "到",
                               "我", "你", "他", "这个", "那个", "你们", "我们", "你家", "我家", "他家",
                               "叫", "叫什么", "什么", "哪个"]
                for prefix in bad_prefixes:
                    if comm_name.startswith(prefix):
                        comm_name = comm_name[len(prefix):]
                        break
                if len(comm_name) >= 3:
                    new_info["community"] = comm_name
                break

        # 10) 城市（匹配常见城市名，只匹配明确提到的）
        cities = ["北京", "上海", "广州", "深圳", "杭州", "南京", "成都", "武汉",
                  "西安", "重庆", "天津", "苏州", "长沙", "郑州", "青岛", "大连",
                  "宁波", "厦门", "福州", "济南", "合肥", "南昌", "南宁", "昆明",
                  "贵阳", "兰州", "乌鲁木齐", "哈尔滨", "沈阳", "长春", "石家庄",
                  "太原", "呼和浩特", "海口", "三亚", "无锡", "常州", "佛山",
                  "东莞", "珠海", "温州", "泉州", "烟台", "潍坊", "徐州", "南通"]
        for city in cities:
            if city in text:
                new_info["city"] = city
                break

        # —— 更新 collected_info（只增不改：有新值才存，用户改口新值覆盖旧值）——
        updated = {}
        for field, value in new_info.items():
            if field in self.collected_info:
                # 旧值为空 或 新值不同（用户改口）→ 更新
                if self.collected_info[field] != value:
                    self.collected_info[field] = value
                    updated[field] = value
        return updated

    def _has_collected(self, field):
        """检查某个字段是否已经收集到了

        Args:
            field: 字段名
        Returns:
            bool: 已收集返回True
        """
        return self.collected_info.get(field) is not None

    def _is_bargain_question(self, text):
        """判断是不是主动砍价/要优惠的问题（明确要优惠动作才算）
        注意：
        - 纯问价（多少钱、价格、报价）不算，归 _is_price_question 管
        - 嫌贵/价格异议（太贵了、有点贵）不算，归 _detect_bargain_pushback 管
        只有用户主动提出要优惠/打折/便宜点的，才算砍价，进入议价流程
        """
        bargain_keywords = [
            # 明确问优惠/折扣（按长度排序，长的优先匹配更精准）
            "能不能优惠", "能不能少", "还能少吗", "能少点吗", "能再便宜",
            "价格能便宜", "价格能少", "价格能优惠", "能优惠吗", "能优惠不",
            "可以优惠", "有什么优惠", "有啥优惠", "有没有优惠", "有优惠吗",
            "能便宜吗", "能打折吗", "能降吗", "最低多少", "最低价", "砍价",
            # 明确要求便宜/少/降
            "再便宜点", "再少点", "再降点", "再打个折", "便宜点呗",
            "不够便宜", "给点优惠", "便宜点", "少点", "优惠点",
        ]
        return any(kw in text for kw in bargain_keywords)

    def _is_bargain_related(self, text):
        """
        判断本轮输入是否跟议价话题相关
        （用于状态机入口判断：在状态中 + 输入相关 才走状态机）
        包括：问价、砍价、嫌贵、竞品对比、选材料、说面积、说偏好
        """
        if self._is_price_question(text):
            return True
        if self._is_bargain_question(text):
            return True
        # 嫌贵/竞品对比 → 也是议价相关（状态机内有专门回怼话术）
        if self._detect_bargain_pushback(text) is not None:
            return True
        if self._detect_material_choice(text):
            return True
        if self._detect_preference_type(text):
            # 注意：只有明确的偏好选择才算（"要环保的""性价比高的"）
            # 纯提问形式（"环保吗""质量怎么样"）不算，让它走知识库回答
            pref = self._detect_preference_type(text)
            # 环保偏好需要有选择意味（的/点/型），纯提问不算
            if pref == "eco_friendly":
                eco_choice_words = ["环保的", "环保点", "环保型", "要环保", "选环保", "注重环保"]
                if not any(w in text for w in eco_choice_words):
                    pass  # 纯提问，不算议价相关
                else:
                    return True
            else:
                return True
        size_val, _, _ = self._detect_order_size(text)
        if size_val:
            return True
        scene_key, _ = self._detect_room_type(text)
        if scene_key:
            return True
        if self._is_end_conversation(text):
            return True
        # 确认/认可类（议价中的"行""可以""嗯"等属于推进议价）
        confirm_keywords = ["行", "可以", "好的", "嗯", "ok", "OK", "行吧", "还行吧"]
        if any(kw == text.strip() or kw in text for kw in confirm_keywords):
            return True
        return False

    def _is_still_bargaining(self, text):
        """
        反向检测：用户还在聊价格吗？
        只有命中议价相关关键词，才算还在聊价格；没命中就是换话题了，直接退出状态机
        （比正向检测"用户在聊别的话题"更靠谱，因为价格相关词是有限的、能列全的）

        注意："可以/行/好"这类短确认词如果出现在疑问句里（"可以吗""行吗"），不算议价相关
        因为用户可能是在问别的事情行不行，不是在确认价格
        """
        # 强相关词（价格/砍价/面积/材料/竞品）— 只要命中一个就算
        strong_keywords = [
            # 价格/钱相关
            "价格", "价钱", "多少钱", "怎么卖", "怎么算", "报价", "价位", "怎么收费",
            # 砍价/嫌贵
            "便宜", "优惠", "贵", "打折", "砍价", "降价", "少点", "最低价", "最低",
            "能不能降", "太贵", "有点贵", "这么贵", "好贵", "还是贵", "贵了", "价格高",
            # 面积/数量（推进状态机）
            "平", "平方", "平米", "面积", "个平方", "几个柜子", "多少米", "多宽",
            # 材料名（推进状态机，必须是完整材料名，不能加"板"单字）
            "颗粒板", "多层板", "生态板", "欧松板", "实木板", "兔宝宝", "露水河",
            # 竞品对比（状态机内应答-价值塑造/竞品对比）
            "别家", "人家", "我问了", "比你们", "别人", "另一家", "才600", "才500",
        ]
        has_strong = any(kw in text for kw in strong_keywords)
        if has_strong:
            return True

        # 弱相关词（确认/犹豫/收尾）— 只有在不是疑问句的情况下才算
        weak_keywords = [
            # 确认/否定/犹豫
            "可以", "行", "好的", "好", "不行", "再想想", "考虑", "考虑下",
            "嗯", "哦", "行吧", "还行", "不错",
            # 收尾/留资相关
            "微信", "加个", "联系方式", "电话", "微信吧", "加微信",
            "量房", "上门", "设计师", "签单", "下单", "定", "订",
            # 结束语
            "我考虑下", "再说吧", "先看看", "以后再说", "算了",
        ]
        has_weak = any(kw in text for kw in weak_keywords)
        if not has_weak:
            return False  # 强相关没命中，弱相关也没命中 → 肯定换话题了

        # 命中了弱相关词 → 再判断是不是疑问句
        # 疑问句里的弱相关词（如"可以吗""行吗"）不算议价相关
        question_markers = ["吗", "呢", "什么", "怎么", "哪", "?", "？"]
        is_question = any(m in text for m in question_markers)
        if is_question:
            return False  # 疑问句 + 只有弱相关 → 换话题了
        return True  # 非疑问句 + 弱相关 → 还算在聊价格

    # ---------- LLM通用分类工具函数 ----------
    def _llm_classify(self, text, system_prompt, options, cache_prefix="llmcls", timeout=8):
        """
        通用LLM分类工具：给定用户输入+系统提示词+可选值列表，返回其中一个
        
        Args:
            text: 用户输入文本
            system_prompt: 系统提示词（描述分类任务和各选项含义）
            options: list of str，合法的分类值
            cache_prefix: 缓存key前缀，用于区分不同分类任务
            timeout: 超时时间（秒）
        
        Returns:
            str: options中的一个值，失败/超时返回None
        """
        import hashlib

        # 取上一轮对话做上下文
        prev_user = ""
        prev_bot = ""
        if self.history:
            last = self.history[-1]
            prev_user = last.get("user", "")
            prev_bot = last.get("bot", "")

        # 缓存key：用户输入+上下文
        cache_content = text.strip() + "|" + prev_user + "|" + prev_bot
        cache_key = f"{cache_prefix}:" + hashlib.md5(cache_content.encode("utf-8")).hexdigest()
        redis_client = self._get_redis()
        if redis_client:
            try:
                cached = redis_client.get(cache_key)
                if cached and cached in options:
                    return cached
            except Exception:
                pass  # Redis挂了也不影响主流程

        # 组装用户消息：有上下文就带上一轮
        if prev_user and prev_bot:
            user_msg = (
                f"上一轮对话：\n"
                f"用户：{prev_user}\n"
                f"客服：{prev_bot}\n\n"
                f"当前用户说：{text}\n"
                f"分类标签："
            )
        else:
            user_msg = f"用户说：{text}\n分类标签："

        try:
            resp = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    "temperature": 0,
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip().lower()

            # 在合法值中找匹配（精确匹配优先，然后包含匹配）
            result = None
            for opt in options:
                if opt == content:
                    result = opt
                    break
            if not result:
                for opt in options:
                    if opt in content:
                        result = opt
                        break

            # 写入缓存，过期7天
            if result and redis_client:
                try:
                    redis_client.setex(cache_key, 7 * 24 * 3600, result)
                except Exception:
                    pass

            return result
        except Exception as e:
            print(f"[LLM分类降级] prefix={cache_prefix}, error={e}")
            return None

    # ---------- 4) LLM 4 分类意图识别（兜底用） ----------
    def _get_redis(self):
        """获取Redis连接，单例模式，连接失败返回None（降级）"""
        if hasattr(self, '_redis_client') and self._redis_client is not None:
            return self._redis_client
        try:
            import redis as redis_lib
            from config import CACHE_REDIS_HOST, CACHE_REDIS_PORT, CACHE_REDIS_DB, CACHE_REDIS_PASSWORD, CACHE_REDIS_TIMEOUT
            self._redis_client = redis_lib.Redis(
                host=CACHE_REDIS_HOST,
                port=CACHE_REDIS_PORT,
                db=CACHE_REDIS_DB,
                password=CACHE_REDIS_PASSWORD,
                socket_timeout=CACHE_REDIS_TIMEOUT,
                socket_connect_timeout=CACHE_REDIS_TIMEOUT,
                decode_responses=True,
            )
            # 测试连接
            self._redis_client.ping()
            return self._redis_client
        except Exception as e:
            print(f"[Redis连接失败，意图分类缓存降级] {e}")
            self._redis_client = None
            return None

    def classify_intent(self, text):
        """
        LLM 10分类意图路由（只分类不生成）
        返回：1-10 的整数，失败返回 None（由调用方降级到关键词匹配）
        1=price_inquiry 2=bargain 3=material_compare 4=material_detail
        5=process_question 6=measurement_design 7=leave_contact
        8=product_type 9=chitchat 10=unclear
        缓存：Redis，key=intent:{md5(text+上下文)}，过期7天；Redis不可用时自动降级不缓存
        上下文：带上最近1轮对话，帮助LLM结合语境判断（如刚问完价格说"太贵了"=bargain）
        """
        import hashlib
        import re

        # 取上一轮对话（如果有）
        prev_user = ""
        prev_bot = ""
        if self.history:
            last = self.history[-1]
            prev_user = last.get("user", "")
            prev_bot = last.get("bot", "")

        # 缓存key：有上下文就把上下文也算进去，不同上下文相同输入可能分类不同
        cache_content = text.strip() + prev_user + prev_bot
        cache_key = "intent:" + hashlib.md5(cache_content.encode("utf-8")).hexdigest()
        redis_client = self._get_redis()
        if redis_client:
            try:
                cached = redis_client.get(cache_key)
                if cached:
                    return int(cached)
            except Exception:
                pass  # Redis挂了也不影响主流程

        sys_prompt = (
            "你是一个全屋定制客服的意图分类器。结合上下文判断用户当前问题属于哪一类，只返回数字编号：\n"
            "1. price_inquiry - 问价格、多少钱、怎么收费、价位、报价\n"
            "2. bargain - 砍价、要优惠、便宜点、打折、能不能少、太贵了\n"
            "3. material_compare - 材料对比、各有什么优缺点、哪个好、区别在哪、怎么选\n"
            "4. material_detail - 问某种材料的详细信息、环保吗、质量怎么样、什么牌子\n"
            "5. process_question - 工艺、封边、五金、安装、环保等级、施工\n"
            "6. measurement_design - 量房、设计、出图、周期、多久能做好、工期\n"
            "7. leave_contact - 留联系方式、约量房、加微信、留电话、留地址\n"
            "8. product_type - 问产品种类、能做什么、有哪些柜子、业务范围\n"
            "9. chitchat - 闲聊、打招呼、谢谢、好的、嗯、哦\n"
            "10. unclear - 听不懂、不确定\n"
            "注意：请结合上下文判断。比如用户刚问完价格说'太贵了'，就是bargain（砍价/嫌贵），不是其他类别。\n"
            "只返回一个数字（1-10），不要解释，不要其他文字。"
        )

        # 组装用户消息：有上下文就带上一轮
        if prev_user and prev_bot:
            user_msg = (
                f"上一轮对话：\n"
                f"用户：{prev_user}\n"
                f"客服：{prev_bot}\n\n"
                f"当前用户说：{text}\n"
                f"分类编号："
            )
        else:
            user_msg = f"用户说：{text}\n分类编号："

        try:
            resp = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": MODEL,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    "temperature": 0,
                },
                timeout=5,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
            m = re.search(r'10|[1-9]', content)
            if m:
                result = int(m.group())
                # 写入Redis缓存，过期7天
                if redis_client:
                    try:
                        redis_client.setex(cache_key, 7 * 24 * 3600, result)
                    except Exception:
                        pass
                return result
            return None
        except Exception as e:
            print(f"[LLM意图分类降级] {e}")
            return None

    # ========== LLM 19类细分类（新架构主力） ==========
    def classify_intent_detail(self, text, prev_user=None, prev_bot=None):
        """
        LLM 19类细分类（带上下文，新架构主入口）
        返回：分类标签字符串，失败返回 None
        
        19个分类：
        price_query     - 问价格/多少钱/报价/价位
        bargain         - 砍价/要优惠/便宜点/打折/要最低价
        complain_price  - 嫌贵/竞品对比/太贵/别家更便宜/才xx钱
        material_compare - 材料对比/哪个好/区别/怎么选
        material_detail - 板材详情/环保/品牌/质量/什么板材
        hardware_detail - 五金详情/铰链/滑轨/什么牌子五金
        process_question - 工艺/安装/封边/能不能做/玻璃门/圆弧
        pricing_method  - 计价方式/投影还是展开/怎么算/加钱项目
        after_sales     - 售后/质保/坏了/维修/保修几年
        shop_info       - 店铺地址/位置/在哪/工厂/参观
        shop_history    - 经营年限/多久了/老店/做了多少年
        measurement     - 量房/工期/设计/多久做好/上门
        product_type    - 产品种类/能做什么/有哪些柜子/榻榻米/橱柜
        lead_capture    - 用户主动留联系方式/留地址/约量房
        ask_contact     - 问联系方式/微信多少/怎么联系/电话多少
        greeting        - 打招呼/你好/在吗/有人吗
        thanks          - 谢谢/感谢/多谢
        abuse           - 辱骂/脏话/攻击性语言
        fallback        - 听不懂/不确定/其他
        """
        import hashlib

        # 缓存key：包含上下文（同样的话，上下文不同分类可能不同）
        cache_content = text.strip() + (prev_user or "") + (prev_bot or "")
        cache_key = "intent_detail:" + hashlib.md5(cache_content.encode("utf-8")).hexdigest()
        redis_client = self._get_redis()
        if redis_client:
            try:
                cached = redis_client.get(cache_key)
                if cached:
                    return cached.decode("utf-8") if isinstance(cached, bytes) else cached
            except Exception:
                pass

        # 19类分类定义（给LLM看的）
        category_defs = """
你是全屋定制客服意图分类器。结合上下文，把用户当前这句话归入以下19类之一，只输出分类标签，不要解释，不要其他文字。

分类定义：
1. price_query - 用户在问价格，如多少钱、报价、价位、怎么收费、一平方多少钱
2. bargain - 用户主动砍价要优惠，如便宜点、优惠点、打折、最低价、能不能少
3. complain_price - 用户嫌贵或拿竞品对比，如太贵、别家更便宜、人家才600、比你们便宜
4. material_compare - 用户在对比不同材料，如哪个好、有什么区别、怎么选、各有什么优缺点
5. material_detail - 用户问板材详情，如环保吗、什么板材、板材品牌、质量怎么样
6. hardware_detail - 用户问五金详情，如什么铰链、五金品牌、滑轨、标配五金有哪些
7. process_question - 用户问工艺/安装/能不能做，如能做圆弧吗、封边工艺、玻璃门、安装团队
8. pricing_method - 用户问计价方式，如按投影还是展开、怎么算、抽屉加钱吗、有没有隐形消费
9. after_sales - 用户问售后/质保，如有售后吗、坏了怎么办、质保几年、五金坏了
10. shop_info - 用户问店铺地址/位置/工厂/能不能参观，如公司在哪、工厂在哪、可以参观吗
11. shop_history - 用户问经营历史/年限，如开店多久了、做了多少年、是老品牌吗
12. measurement - 用户问量房/工期/设计，如量房免费吗、工期多久、什么时候上门、设计费
13. product_type - 用户纯问产品种类/业务范围/能做什么，如能做什么柜子、有榻榻米吗、你们都做什么
    注意：如果用户是在说"我要做橱柜+衣柜""厨房做什么板材好"这类带选材/价格意图的，归为 price_query，不是 product_type
    注意：用户在选材料阶段说场景（如"做橱柜和衣柜""档次高点的，衣柜用什么"），归为 price_query，走议价状态机，不是 product_type
14. lead_capture - 用户主动留联系方式或约上门，如我微信xxx、我家住xxx、过来量房吧
15. ask_contact - 用户问怎么联系你们，如你微信多少、留个电话、怎么联系、地址在哪（注意：纯问地址归shop_info）
16. greeting - 用户打招呼，如你好、在吗、有人吗、嗨
17. thanks - 用户表示感谢，如谢谢、感谢、多谢、辛苦
18. abuse - 用户说脏话或攻击性语言，如傻逼、滚、操、垃圾、废物
19. fallback - 以上都不是，听不懂或无关话题

注意事项：
- 结合上一轮对话判断。比如刚报完价用户说"太贵了"，就是complain_price，不是其他。
- "能便宜点吗"是bargain，"这么贵啊"是complain_price。
- 用户问"你们微信多少"是ask_contact，用户说"我微信是xxx"是lead_capture。
- "公司在哪"是shop_info，不是ask_contact。
- "开店多久了"是shop_history，"工期多久"是measurement。

只输出一个分类标签（小写英文），不要编号，不要解释。
"""

        # 组装用户消息
        if prev_user and prev_bot:
            user_msg = f"""上一轮对话：
用户：{prev_user}
客服：{prev_bot}

当前用户说：{text}

分类标签："""
        else:
            user_msg = f"""用户说：{text}

分类标签："""

        try:
            resp = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": MODEL,
                    "messages": [
                        {"role": "system", "content": category_defs},
                        {"role": "user", "content": user_msg},
                    ],
                    "temperature": 0,
                },
                timeout=10,  # 10秒超时，火山引擎第一次建连可能慢
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip().lower()
            # 提取有效分类标签（19个合法值之一）
            valid_categories = [
                "price_query", "bargain", "complain_price",
                "material_compare", "material_detail", "hardware_detail",
                "process_question", "pricing_method", "after_sales",
                "shop_info", "shop_history", "measurement", "product_type",
                "lead_capture", "ask_contact", "greeting", "thanks", "abuse", "fallback"
            ]
            # 尝试精确匹配
            for cat in valid_categories:
                if cat in content:
                    result = cat
                    # 写入缓存
                    if redis_client:
                        try:
                            redis_client.setex(cache_key, 7 * 24 * 3600, result)
                        except Exception:
                            pass
                    return result
            # 没匹配到有效分类
            return None
        except Exception as e:
            print(f"[LLM细分类降级] {e}")
            return None

    # ========== 新架构：关键词校验 & 分类映射 ==========
    # 每个分类2-3个必有关联词，沾边就算过，防止LLM完全瞎分
    CATEGORY_KEYWORDS = {
        "price_query": ["价格", "多少钱", "一平", "报价", "价位", "怎么卖", "收费"],
        "bargain": ["便宜", "优惠", "打折", "砍价", "最低", "能不能少", "降"],
        "complain_price": ["贵", "别家", "人家", "才", "比你", "太贵", "好贵", "这么贵"],
        "material_compare": ["区别", "哪个好", "怎么选", "对比", "各有什么"],
        "material_detail": ["板材", "环保", "品牌", "质量", "什么板", "兔宝宝", "甲醛"],
        "hardware_detail": ["五金", "铰链", "滑轨", "拉手", "DTC", "dtc", "什么五金"],
        "process_question": ["能做", "工艺", "封边", "安装", "玻璃门", "圆弧", "做不做"],
        "pricing_method": ["怎么算", "投影", "展开", "加钱", "计价", "包含什么", "隐形消费"],
        "after_sales": ["售后", "质保", "坏了", "保修", "维修", "出问题", "维护"],
        "shop_info": ["地址", "在哪", "店", "工厂", "门店", "参观", "位置"],
        "shop_history": ["多久", "多少年", "老店", "历史", "经营", "开了几年", "做了多久"],
        "measurement": ["量房", "工期", "设计", "上门", "多久能", "多少天", "周期"],
        "product_type": ["柜子", "做什么", "榻榻米", "橱柜", "衣柜", "能做", "有哪些"],
        "lead_capture": ["我微信", "我电话", "我家住", "我在", "留个电话", "过来量", "约一下"],
        "ask_contact": ["微信多少", "怎么联系", "联系方式", "电话多少", "你微信", "怎么找你"],
        "greeting": ["你好", "在吗", "有人", "嗨", "您好", "喂"],
        "thanks": ["谢谢", "感谢", "多谢", "辛苦", "谢了"],
        "abuse": ["傻", "滚", "操", "垃圾", "废物", "脑残", "傻逼", "艹"],
        "fallback": [],
    }

    # LLM分类 → 模板分类映射（大部分同名，少数特殊映射）
    CATEGORY_TEMPLATE_MAP = {
        "price_query": "bargain_price_range",   # 进状态机，这里只是映射参考
        "bargain": "bargain_probe",             # 进状态机
        "complain_price": "bargain_value_build", # 进状态机
        "shop_history": "shop_info",            # 店铺历史合并到shop_info
        "ask_contact": "ask_contact",
        "material_compare": "eo_vs_enf",         # 先用E0vsENF模板兜底，后续可扩展
        "measurement": "total_cycle",            # 工期模板
        "hardware_detail": "hardware_detail",
        "pricing_method": "pricing_method",
        "after_sales": "after_sales",
        "shop_info": "shop_info",
        "product_type": "material_recommend_kitchen", # 兜底，实际会动态判断
        "lead_capture": "lead_capture_success",
        "greeting": "chat_greeting",
        "thanks": "chat_thanks",
        "abuse": "chat_abuse",
        "material_detail": "material_board_brand",
        "fallback": None,
    }

    def _validate_category(self, text, category):
        """关键词校验：LLM分的类有没有沾边的关键词？有就通过，没有就降级"""
        if category == "fallback":
            return True  # fallback总是通过
        kws = self.CATEGORY_KEYWORDS.get(category, [])
        if not kws:
            return True  # 没有定义校验词就算通过
        return any(kw in text for kw in kws)

    def _render_category_template(self, category, text=None):
        """根据LLM分类标签，找到对应模板渲染回答。找不到返回None"""
        # 特殊分类的动态处理
        if category == "material_detail" and text:
            # 问环保 → eco_level模板
            eco_words = ["环保", "甲醛", "E0", "ENF", "e0", "enf", "达标"]
            if any(w in text for w in eco_words):
                for hq in self.templates.get("hot_questions", []):
                    if hq.get("category") == "eco_level":
                        from jinja2 import Template
                        tpl = random.choice(hq.get("templates", []))
                        return Template(tpl).render(**self._vars())
            # 默认 → 板材品牌
            for hq in self.templates.get("hot_questions", []):
                if hq.get("category") == "material_board_brand":
                    from jinja2 import Template
                    tpl = random.choice(hq.get("templates", []))
                    return Template(tpl).render(**self._vars())

        if category == "process_question" and text:
            # 工艺问题 → 调原有工艺检测方法
            result = self._match_process_question(text)
            if result:
                return result[1]  # 返回回答

        if category == "product_type" and text:
            # 产品类型 → 看有没有场景匹配（厨房/卧室/榻榻米等）
            room_type, _ = self._detect_room_type(text)
            if room_type:
                for hq in self.templates.get("hot_questions", []):
                    if hq.get("category") == f"material_recommend_{room_type}":
                        from jinja2 import Template
                        tpl_list = hq.get("templates", [])
                        if tpl_list:
                            return Template(random.choice(tpl_list)).render(**self._vars())

        if category == "measurement" and text:
            # 工期相关 → 检测具体类型
            rush_words = ["加急", "加快", "赶时间", "提前"]
            if any(w in text for w in rush_words):
                for hq in self.templates.get("hot_questions", []):
                    if hq.get("category") == "can_rush":
                        from jinja2 import Template
                        tpl = random.choice(hq.get("templates", []))
                        return Template(tpl).render(**self._vars())
            # 默认 → 总工期
            for hq in self.templates.get("hot_questions", []):
                if hq.get("category") == "total_cycle":
                    from jinja2 import Template
                    tpl = random.choice(hq.get("templates", []))
                    return Template(tpl).render(**self._vars())

        if category == "material_compare" and text:
            # 材料对比 → 找eo_vs_enf模板（最常见）
            for hq in self.templates.get("hot_questions", []):
                if hq.get("category") == "eo_vs_enf":
                    from jinja2 import Template
                    tpl = random.choice(hq.get("templates", []))
                    return Template(tpl).render(**self._vars())

        # 通用：直接找同名模板分类
        template_cat = self.CATEGORY_TEMPLATE_MAP.get(category, category)
        if template_cat:
            for hq in self.templates.get("hot_questions", []):
                if hq.get("category") == template_cat:
                    tpl_list = hq.get("templates", [])
                    if tpl_list:
                        from jinja2 import Template
                        return Template(random.choice(tpl_list)).render(**self._vars())

        return None

    def detect_intent(self, text):
        """调 LLM 做 4 分类意图识别，失败降级 chat"""
        sys_prompt = (
            "你是全屋定制客服意图分类器。把客户这句话归为四类之一，只输出英文标签本身：\n"
            "consult=咨询产品/工艺/参数；complain=挑刺/嫌贵/质疑；"
            "bargain=砍价/要优惠/要便宜；chat=拒绝或闲扯或打探背景或无关话题。\n"
            "只回一个单词：consult 或 complain 或 bargain 或 chat"
        )
        try:
            resp = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": MODEL,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": text},
                    ],
                    "temperature": 0,
                },
                timeout=15,
            )
            resp.raise_for_status()
            label = resp.json()["choices"][0]["message"]["content"].strip().lower()
            for tag in ("consult", "complain", "bargain", "chat"):
                if tag in label:
                    return tag
            return "chat"
        except Exception as e:
            print(f"[意图识别降级] {e}")
            return "chat"

    # ---------- 5) 闲扯子类型判断 ----------
    def _sub_chat_type(self, text):
        """reject=礼貌拒绝；background=打探背景；其他归 casual"""
        if any(w in text for w in ("考虑", "商量", "对比", "再看看", "想想")):
            return "reject"
        if any(w in text for w in ("几年", "多少年", "店在", "老板", "谁", "哪里")):
            return "background"
        return "casual"

    # ---------- 盲区检测 ----------
    def _is_obscure_question(self, text):
        """判断是不是偏门开放式问题（答不上来的那种）"""
        obscure_keywords = [
            "能不能做", "你们做不做", "可以做吗", "有没有",
            "你们有", "能做吗", "做不做", "支持吗",
            "会不会", "能做不", "有吗",
        ]
        # 精确关键词匹配
        if any(kw in text for kw in obscure_keywords):
            return True
        # 宽松句式判断：能做/可以做 + 内容 + 吗/？ （如 "能做榻榻米吗"）
        if ("能做" in text or "可以做" in text or "做不做" in text or "能不能做" in text) and \
           ("吗" in text or "？" in text or "?" in text):
            return True
        return False

    # ---------- 6) 对话结束判断（只日志 + 清上下文，红线：不发任何消息） ----------
    END_KEYWORDS = ["谢谢", "感谢", "好的", "再见", "拜拜", "不用了", "不需要", "再说吧"]

    def is_chat_ended(self, user_input):
        """满足任一规则即视为对话结束：① 命中结束关键词 ② 连续结束语≥2"""
        if any(kw in user_input for kw in self.END_KEYWORDS):
            self.end_streak += 1
        else:
            self.end_streak = 0
        if self.end_streak >= 2:
            return True
        if self.end_streak >= 1:
            # 命中 1 次结束关键词：只增加计数，不立即结束（保留软收尾机会）
            pass
        return False

    # ---------- 主入口 ----------
    def reply(self, text):
        """
        新架构主入口：LLM主力理解 + 关键词校验兜底 + 模板保证准确
        
        流程：留资检测 → LLM细分类（带上下文）→ 关键词校验 → 路由（状态机/模板）→ 存历史 → 返回
        LLM失败/校验不通过 → 自动降级到关键词匹配体系（旧架构）
        """
        # 0) 最高优先级：留资检测（保留原逻辑）
        contact_type = self._detect_contact_info(text)
        if contact_type is not None:
            self.bargain_step = 0
            self.bargain_pullback_count = 0
            import random
            from jinja2 import Template

            # 手机号格式不对 → 提醒重发
            if contact_type == "phone_invalid":
                for hot_q in self.templates.get("hot_questions", []):
                    if hot_q.get("category") == "lead_phone_invalid":
                        tpl_list = hot_q.get("templates", [])
                        if tpl_list:
                            answer = Template(random.choice(tpl_list)).render(**self._vars())
                            self.history.append({"user": text, "bot": answer})
                            self.history = self.history[-3:]
                            return "lead_phone_invalid", answer
                # 兜底
                answer = "不好意思，这个手机号好像位数不对呢，您方便再核对下发我一下吗？"
                self.history.append({"user": text, "bot": answer})
                self.history = self.history[-3:]
                return "lead_phone_invalid", answer

            # 正确手机号 → 专属感谢话术 + 引导需求
            if contact_type == "phone":
                for hot_q in self.templates.get("hot_questions", []):
                    if hot_q.get("category") == "lead_phone_success":
                        tpl_list = hot_q.get("templates", [])
                        if tpl_list:
                            answer = Template(random.choice(tpl_list)).render(**self._vars())
                            self.history.append({"user": text, "bot": answer})
                            self.history = self.history[-3:]
                            return "lead_capture/phone", answer

            # 其他留资（微信/地址等）→ 通用留资成功话术
            for hot_q in self.templates.get("hot_questions", []):
                if hot_q.get("category") == "lead_capture_success":
                    tpl_list = hot_q.get("templates", [])
                    if tpl_list:
                        answer = Template(random.choice(tpl_list)).render(**self._vars())
                        self.history.append({"user": text, "bot": answer})
                        self.history = self.history[-3:]
                        return "lead_capture", answer

        # 0.3) 信息提取：从用户消息中抽取可收集字段，存入 collected_info
        #      （在意图路由之前执行，确保后续所有询问类话术都能感知到已收集的信息）
        self._extract_collected_info(text)

        # 0.5) 全局简单问题直答层（LLM分类之前，能直接答的先答）
        # 处理：确认类追问、工艺类追问、材料细节类追问
        simple_result = self._handle_simple_question(text)
        if simple_result is not None:
            tag, answer = simple_result
            self.llm_fallback_streak = 0
            self.history.append({"user": text, "bot": answer})
            self.history = self.history[-3:]
            return tag, answer

        # 1) 取上一轮对话
        prev_user, prev_bot = None, None
        if self.history:
            last = self.history[-1]
            prev_user = last.get("user", "")
            prev_bot = last.get("bot", "")

        # 2) LLM 细分类（带1轮上下文）
        llm_category = self.classify_intent_detail(text, prev_user, prev_bot)

        # 3) 关键词匹配结果（兜底用，同时用于校验）
        hot_result = self._match_hot_question(text)
        hot_category = hot_result[0] if hot_result else None

        # 4) 关键词校验：LLM分的类沾边吗？
        final_category = llm_category
        if llm_category and not self._validate_category(text, llm_category):
            # LLM分类不靠谱，降级用关键词匹配
            final_category = hot_category

        # 5) 如果LLM完全失败，且关键词也没匹配到 → 走旧架构兜底
        #    注意：如果已经在议价中，不走旧架构，继续交给v3降级处理（原地踏步比乱跳安全）
        if final_category is None and self.bargain_step == 0:
            return self._reply_legacy(text)

        # 6) 路由：价格类 → 进议价状态机v2
        price_categories = ("price_query", "bargain", "complain_price")

        # ===== 议价状态拦截 =====
        # 判断是否进入议价状态机：
        # 1. 已经在议价中（bargain_step > 0）→ 一律进状态机，交给 v3 全权决策
        #    （v3 内部可以通过 switch_topic 动作主动退出议价）
        # 2. 不在议价中，但LLM分类是价格类 → 进入状态机
        should_enter_bargain = (self.bargain_step > 0) or (final_category in price_categories)

        if should_enter_bargain:
            # 议价状态内：完全交给 v3 决策层处理，simple_followup 不再介入
            # （v3 的 answer_detail / pullback_topic / value_build 等动作能覆盖详情问答场景）
            tag, answer = self._handle_bargain_v3(text, final_category)
            # 状态机说接不住（返回None）→ 退出去走全局分类
            if tag is None:
                # 退出了状态机，重新用关键词匹配找答案
                if hot_result:
                    tag, answer = hot_result
                else:
                    # 终极兜底
                    answer = self._render_category_template("fallback", text) or "不好意思，我没太明白您的意思~"
                    tag = "fallback"
        else:
            # 非价格类 → 找对应模板回答
            answer = self._render_category_template(final_category, text)
            if answer:
                tag = final_category
                # 非价格类问题 → 退出议价状态（如果之前在议价中）
                if final_category not in price_categories:
                    self.bargain_step = 0
                    self.bargain_pullback_count = 0
            else:
                # 找不到模板 → 用关键词兜底
                if hot_result:
                    tag, answer = hot_result
                else:
                    # 终极兜底
                    answer = self._render_fallback_guided(text)
                    tag = "fallback"

        # 7) 存历史
        self.history.append({"user": text, "bot": answer})
        self.history = self.history[-3:]

        return tag, answer

    def _render_fallback_guided(self, text):
        """引导式兜底（新架构默认兜底）"""
        fallbacks = [
            "没太明白您的意思~ 您是想了解价格、板材、还是想加个微信发点案例参考？",
            "不好意思我没get到~ 您是问价格、材料、还是想了解一下我们店？",
            "这个我得跟设计师确认下，您加我微信{{wechat_id}}，我让设计师给您详细解答。",
        ]
        from jinja2 import Template
        import random
        return Template(random.choice(fallbacks)).render(**self._vars())

    def _handle_simple_followup(self, text, llm_category=None):
        """
        简单追问直答层（插在议价状态机前面）
        核心原则：先答问题，再推进状态机
        
        处理三种简单追问：
        1. 确认类追问（"是X吗"、"你说的X吧"）→ 查上下文答是或不是
        2. 材料细节类追问（环保/封边/五金等事实问题）→ 先答再推进
        3. 其他能直接命中关键词的问题 → 先答再推进
        
        返回：(tag, answer) 或 None（表示不是简单追问，继续走状态机）
        """
        # 不在议价中，不处理
        if self.bargain_step == 0:
            return None
        # 没有上一轮对话，没法做上下文判断
        if not self.history:
            return None
        prev_bot = self.history[-1].get("bot", "")

        # ===== 类型A：确认类追问 =====
        if self._is_confirmation_question(text):
            item = self._extract_confirm_item(text)
            if item is None:
                # 提取不出确认项，可能是"对吗/是吗"这种泛确认，默认肯定
                # 只有当上一轮是问句（比如在摸底问面积），才推进状态机
                return None  # ponytail: 泛确认太复杂，先交给状态机处理
            
            # 查上一轮有没有提到过这个东西
            is_yes = self._bot_mentioned_item(prev_bot, item)
            confirm_answer = self._get_confirm_answer(item, is_yes)
            
            # 答完后要推进状态机——把问题+回答交给状态机，获取推进话术
            # 做法：先回答确认，再拼接状态机的响应
            # 但这里不能直接调状态机（会重复存history），所以只答确认+加一句推进的话
            # 推进话术：如果是材料类确认且在step1，就顺势问面积
            follow_up = self._get_bargain_follow_up()
            full_answer = confirm_answer + follow_up
            return "followup/confirm", full_answer

        # ===== 类型B/C：事实类追问（材料细节/环保/封边等）=====
        # 条件：LLM分类是详情类，或关键词能命中高频问题
        detail_categories = ("material_detail", "hardware_detail", "process_question",
                             "after_sales", "shop_info", "measurement", "product_type")
        is_detail_question = (llm_category in detail_categories) if llm_category else False
        
        # 关键词命中检测（不依赖LLM）
        hot_result = self._match_hot_question(text)
        if hot_result:
            hot_category, hot_answer = hot_result
            # 已经是答案了，再加一句推进议价的话
            follow_up = self._get_bargain_follow_up()
            full_answer = hot_answer + "\n" + follow_up
            return f"followup/{hot_category}", full_answer
        
        if is_detail_question:
            # LLM说是详情类但关键词没命中 → 尝试渲染分类模板
            detail_answer = self._render_category_template(llm_category, text)
            if detail_answer:
                follow_up = self._get_bargain_follow_up()
                full_answer = detail_answer + "\n" + follow_up
                return f"followup/{llm_category}", full_answer

        # 都不是，返回None交给状态机
        return None

    def _get_bargain_follow_up(self):
        """
        答完简单追问后，追加一句推进议价的话术
        根据当前bargain_step决定问什么
        """
        if self.bargain_step == 1:
            # 刚报完区间，一步到位问两个：偏好 + 场景
            templates = [
                "您更看重性价比还是环保呀？主要做哪些柜子？我给您好好推荐推荐。",
                "主要看您看重哪方面，还有做哪些地方的柜子。您跟我说说，我给您推荐最合适的。",
                "您家主要做哪些柜子呀？看重性价比还是环保？我给您好好算算。",
            ]
            return random.choice(templates)
        elif self.bargain_step == 2:
            # 已经报了实价，问面积推进
            templates = [
                "您大概做几个柜子？",
                "面积有多大？我给您算个总价。",
                "对了，您什么时候要啊？",
            ]
            return random.choice(templates)
        elif self.bargain_step >= 3:
            # 摸底或以后，问具体信息
            templates = [
                "您大概做多大面积呀？",
                "什么时候方便量个房？",
            ]
            return random.choice(templates)
        else:
            return ""

    def _get_lead_follow_up(self):
        """
        非议价场景下，答完简单问题后追加的引导话术（引导加微信/问需求）
        从 lead_hooks 和 concessions 里抽
        """
        lead_hooks = self.config.get("lead_hooks", [])
        if lead_hooks:
            # 注意：lead_hooks 里可能有 {{wechat_id}} 变量，需要渲染
            return self._render_lead_hook()
        # 兜底引导
        fallbacks = [
            "您家房子多大呀？我给您大概算个价？",
            "您什么时候要用呀？我给您安排个免费量房？",
            "您加我微信{{wechat_id}}，我发点案例和报价给您参考？",
        ]
        return self._render(random.choice(fallbacks))

    def _match_process_by_keywords(self, text):
        """
        从商家配置的 processes 列表里，按关键词匹配工艺类问题
        返回：(process_key, process_name, can_do, answer_template) 或 None
        匹配规则：用户输入包含工艺的任意一个关键词就算命中，第一个命中的返回
        """
        processes = self.config.get("_processes", [])
        for p in processes:
            keywords = p.get("keywords", [])
            for kw in keywords:
                if kw and kw in text:
                    return (
                        p.get("key"),
                        p.get("name", p.get("key", "")),
                        p.get("can_do", True),
                        p.get("answer_template", ""),
                    )
        return None

    def _get_process_answer(self, process_key, process_name, can_do, answer_template):
        """
        生成工艺类问题的回答
        优先用工艺配置的 answer_template，没有就用通用模板
        """
        if answer_template:
            # 商家配置了专属模板，直接渲染
            extra_vars = {
                "process_name": process_name,
            }
            return self._render(answer_template, extra_vars)
        
        # 没有配置就用通用模板（能做/不能做）
        if can_do:
            yes_tpl = (
                "没问题，{{process_name}}我们常做。"
                "您加我微信{{wechat_id}}，我给您看看款式和案例。"
            )
            return self._render(yes_tpl, {"process_name": process_name})
        else:
            no_tpl = "{{process_name}}我们做不了，不好意思哈。"
            return self._render(no_tpl, {"process_name": process_name})

    def _handle_simple_question(self, text):
        """
        全局简单问题直答层（放在reply主入口最前面，留资检测之后、LLM分类之前）
        能直接答的先答，不绕状态机、不走LLM
        
        注意：议价状态内（bargain_step > 0）直接返回None，全交给 v3 决策层处理
        
        处理顺序（优先级从高到低）：
        1. 确认类追问（"是XX吗"、"你说的XX吧"）
        2. 工艺类追问（"你们做铝框门不"、"能做洞洞板吗"）
        3. 材料细节类追问（"环保吗"、"封边怎么样"、"五金什么牌子"）
        
        返回：(tag, answer) 或 None（不是简单问题，继续走原流程）
        """
        # ponytail: 议价状态内一律交给 v3 决策层，simple_question 不介入
        if self.bargain_step > 0:
            return None

        # ===== 第0优先级：身份/名字类问题 =====
        name_keywords = ["你叫什么", "你叫啥", "你是谁", "你叫什么名字", "你叫什么名字呀", "怎么称呼你", "怎么称呼", "你贵姓"]
        if any(kw in text for kw in name_keywords):
            service_name = self._get_service_name()
            name_answers = [
                "我叫" + service_name + "，是{{shop_name}}的客服～有什么可以帮您的吗？",
                "我是" + service_name + "呀，专门负责咱们{{shop_name}}的在线咨询～您是想了解定制吗？",
                "我叫" + service_name + "，您有任何关于柜子定制的问题都可以问我哦！",
            ]
            import random
            answer = self._render(random.choice(name_answers))
            follow_up = "\n" + self._get_lead_follow_up()
            return "simple/service_name", answer + follow_up

        # ===== 第1优先级：确认类追问 =====
        if self._is_confirmation_question(text):
            item = self._extract_confirm_item(text)
            if item is None:
                return None  # 提取不出确认项，交给原流程
            
            # 判断是/否
            prev_bot = None
            if self.history:
                prev_bot = self.history[-1].get("bot", "")
            
            if prev_bot:
                # 有上下文，用上一轮bot回答判断
                is_yes = self._bot_mentioned_item(prev_bot, item)
            else:
                # 没有上下文（上来就问），检查是不是我们有的配置
                is_yes = self._item_is_available(item)
            
            confirm_answer = self._get_confirm_answer(item, is_yes)
            
            # 答完追加话术：议价中加推进，非议价加引导
            if self.bargain_step > 0:
                follow_up = self._get_bargain_follow_up()
            else:
                follow_up = "\n" + self._get_lead_follow_up()
            
            full_answer = confirm_answer + follow_up
            return "simple/confirm", full_answer

        # ===== 第2优先级：工艺类追问 =====
        process_match = self._match_process_by_keywords(text)
        if process_match:
            pkey, pname, can_do, atpl = process_match
            answer = self._get_process_answer(pkey, pname, can_do, atpl)
            
            # 答完追加话术
            if self.bargain_step > 0:
                follow_up = "\n" + self._get_bargain_follow_up()
            else:
                follow_up = "\n" + self._get_lead_follow_up()
            
            full_answer = answer + follow_up
            return f"simple/process/{pkey}", full_answer

        # ===== 第3优先级：材料细节类追问 =====
        hot_result = self._match_hot_question(text)
        if hot_result:
            hot_category, hot_answer = hot_result
            
            # 只处理详情类的，其他类型（价格、砍价等）让原流程处理
            detail_categories = (
                "material_detail", "eco_level", "edge_band", "edge_glue",
                "hardware_detail", "after_sales", "shop_info",
                "pricing_method", "product_type",
            )
            # 检查hot_category是否包含详情类特征词
            is_detail = False
            for dc in detail_categories:
                if dc in hot_category:
                    is_detail = True
                    break
            
            if is_detail:
                if self.bargain_step > 0:
                    follow_up = "\n" + self._get_bargain_follow_up()
                else:
                    follow_up = "\n" + self._get_lead_follow_up()
                full_answer = hot_answer + follow_up
                return f"simple/detail/{hot_category}", full_answer

        # 都不是，返回None交给原流程
        return None

    def _item_is_available(self, item):
        """
        没有上下文时，判断用户确认的东西是不是我们有的
        用于"上来就问是颗粒板吗"这种场景
        """
        item_type = item.get("type")
        item_key = item.get("key")
        
        if item_type == "material":
            # 在配置的板材列表里就是有的
            name_map = self.config.get("_board_name_map", {})
            return item_key in name_map
        elif item_type == "eco":
            # 环保我们默认就是有的（ENF级）
            return True
        elif item_type == "edge_band":
            # 封边默认有
            return bool(self.config.get("edge_band"))
        elif item_type == "hardware":
            # 五金默认有
            return bool(self.config.get("hardware_brand"))
        return False

    # ---------- ES知识库检索 ----------
    def _search_knowledge_base(self, text, top_k=3):
        """
        检索商家知识库（通用 + 商家私有）
        - kb_id 过滤：只搜 common 和 shop_{shop_id}
        - 商家私有知识优先级高于通用知识（检索后重排序）
        - 检索失败返回空列表，不影响主流程
        
        Args:
            text: 用户问题
            top_k: 返回条数
            
        Returns:
            list of {question, answer, kb_id, category, score}
        """
        try:
            from core.es_client import ElasticsearchClient
            es = ElasticsearchClient()
            index_name = "cs_knowledge"
            
            # 确认索引存在
            if not es.client.indices.exists(index=index_name):
                return []
            
            # 构建 kb_id 过滤列表
            kb_filter = ["common"]
            if self.shop_id:
                kb_filter.append(f"shop_{self.shop_id}")
            
            # BM25检索（question和answer都搜）
            query = {
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": text,
                                "fields": ["question^3", "answer"],
                                "type": "best_fields"
                            }
                        }
                    ],
                    "filter": [
                        {
                            "terms": {
                                "kb_id": kb_filter
                            }
                        }
                    ]
                }
            }
            
            result = es.client.search(
                index=index_name,
                body={
                    "query": query,
                    "size": top_k * 2,  # 多拉一些，后面重排序
                    "_source": ["question", "answer", "kb_id", "category"]
                }
            )
            
            hits = result.get("hits", {}).get("hits", [])
            if not hits:
                return []
            
            # 格式化结果
            results = []
            for hit in hits:
                src = hit["_source"]
                results.append({
                    "question": src.get("question", ""),
                    "answer": src.get("answer", ""),
                    "kb_id": src.get("kb_id", ""),
                    "category": src.get("category", ""),
                    "score": hit.get("_score", 0)
                })
            
            # 商家私有知识优先：相同分数下，shop_ 开头的排前面
            # 策略：商家私有结果的 score 乘以 1.2（加权）
            for r in results:
                if r["kb_id"].startswith("shop_"):
                    r["score"] = r["score"] * 1.2
            
            # 重新排序
            results.sort(key=lambda x: x["score"], reverse=True)
            
            return results[:top_k]
            
        except Exception as e:
            # 知识库检索失败不影响主流程，静默降级
            print(f"[知识库检索] 失败: {e}")
            return []

    def _kb_answer_if_confident(self, text):
        """
        知识库自信回答：如果检索结果相关性足够高，直接用知识库答案
        - 只有最高分超过阈值才认为可信
        - 返回 (tag, answer) 或 None
        """
        results = self._search_knowledge_base(text, top_k=1)
        if not results:
            return None
        
        top = results[0]
        # 简单阈值：score > 2.0 认为足够相关（ES BM25分数，经验值）
        if top["score"] < 2.0:
            return None
        
        answer = top["answer"]
        # 知识库answer里可能有 {{wechat_id}}/{{shop_location}} 等配置变量，需要渲染
        answer = self._render(answer)
        # 加引导留资
        if self.bargain_step == 0:
            answer += "\n" + self._get_lead_follow_up()
        
        return f"kb/{top['category']}", answer


    def _render(self, template_str, extra_vars=None):
        """
        渲染单个模板字符串，注入配置变量
        统一入口，避免到处写 from jinja2 import Template
        """
        from jinja2 import Template
        if extra_vars:
            vars_dict = {**self._vars(), **extra_vars}
        else:
            vars_dict = self._vars()
        return Template(template_str).render(**vars_dict)

    # ========== 议价状态机 v3：LLM决策层 + 动作执行层 ==========

    # 议价动作库定义（给LLM看的说明）
    BARGAIN_ACTIONS_DESC = """
可用动作列表（只能选一个）：
1. restate_price_range - 换个说法重述价格区间。适用：Step1时用户重复问价、没听懂价格
2. recommend_material - 推荐材料并报实价。适用：用户说"你推荐""都行""不知道选什么"，或用户提到了具体场景/偏好需要推荐；detail_param传JSON字符串：{"material":"推荐的材料key", "reason":"推荐理由（一句话，结合场景+偏好，口语化，只说主推材料的好处，不要提其他材料的名字和价格）"}
   重要：一次只推一个主推材料，不要在理由里对比或提到其他材料
3. quote_material_price - 报指定材料的实价。适用：用户明确说"颗粒板多少钱""多层板呢"，detail_param传材料key
4. probe_area_demand - 摸底询问面积/需求。适用：用户确认了材料/价格，进入摸底阶段
5. answer_detail - 回答材料/工艺/五金等详情问题。适用：用户问"什么封边""五金什么牌子""环保吗"
6. value_build - 价值塑造（解释为什么这个价）。适用：用户嫌贵、质疑价格、拿竞品对比
7. give_discount - 给出优惠报价。适用：用户明确议价、且已摸底（Step3+）
8. pullback_topic - 拉回议价主题。适用：用户扯远了（问工期、问安装、问店铺等无关问题）
9. switch_topic - 切换出议价话题。适用：用户明确不想聊价格了，问其他重大话题
10. lead_wechat - 引导加微信。适用：用户要走、犹豫不定、聊很久没进展
11. advance_from_step2 - 从Step2推进到Step3（摸底）。适用：Step2时用户对价格/材料表示认可
12. confirm_intent - 反问确认用户意图。适用：用户输入太模糊，不知道想干什么
13. compare_materials - 回答材料对比/质疑类问题。适用：用户问"XX板不好吗""XX和XX哪个好""为什么不推荐XX""XX板怎么样"；detail_param传JSON字符串，包含{affirmed_material:"被肯定的材料key", affirm_reason:"为什么说它也不错", recommend_reason:"为什么推荐当前材料（结合场景）", choice_message:"选择权还给用户的话"}
14. supplement_scene - 补充场景推荐。适用：Step2时用户补充新场景/新房间（如"还有卧室呢""厨房也做""衣柜也要""客厅用什么合适""橱柜用什么板""榻榻米呢"），表示在问新场景用什么材料/多少钱；用户问的是新空间/新房间/新柜子的选材或价格，而不是在换材料；detail_param传JSON字符串：{"scene":"场景名（如客厅/厨房/衣柜）", "reason":"推荐理由（一句话，说明为什么当前已选材料也适合这个新场景，口语化）"}
"""

    def _llm_bargain_decision(self, text):
        """
        LLM 议价决策层：根据当前状态 + 对话历史 + 用户输入，从动作库选一个动作
        返回：{"action": str, "reason": str, "detail_param": str}，失败返回 None
        
        设计原则：
        - LLM 只做决策（选动作），不生成话术
        - temperature=0，确保决策稳定
        - 严格 JSON 格式输出
        """
        import json

        # 组装对话历史（最近3轮）
        history_text = ""
        if self.history:
            for i, h in enumerate(self.history[-3:]):
                history_text += f"第{i+1}轮 - 用户：{h['user']}\n客服：{h['bot']}\n"
        else:
            history_text = "（无历史对话）"

        # 当前选中材料说明
        if self.selected_material:
            material_info = f"已选材料：{get_material_name(self.selected_material, self.config)}（{self.selected_material}）"
        else:
            material_info = "未选材料"

        # 主推材料
        main_mat = self.config.get("main_material", "multi_layer_board")
        main_mat_name = get_material_name(main_mat, self.config)

        # 系统提示词
        # 组装最近用户输入历史（只取用户说的话，最近2轮，帮助LLM理解上下文）
        user_history_text = ""
        if self.history:
            user_msgs = [h["user"] for h in self.history[-2:]]
            for i, msg in enumerate(user_msgs):
                user_history_text += f"{i+1}. {msg}\n"
        if not user_history_text:
            user_history_text = "（无）"

        system_prompt = f"""
你是全屋定制客服的议价决策引擎。根据当前议价阶段、对话历史、用户输入，从预定义动作库中选择最合适的一个动作。

【当前状态】
- 议价阶段（bargain_step）：{self.bargain_step}（0=未开始，1=已报价格区间，2=已报材料实价，3=摸底问面积，4=已报优惠价）
- {material_info}
- 商家主推材料：{main_mat_name}（{main_mat}）
- 拉回正题累计次数：{self.bargain_pullback_count}

【最近用户输入历史（最近2轮，辅助理解上下文）】
{user_history_text}
【最近对话历史】
{history_text}
{self.BARGAIN_ACTIONS_DESC}
【输出要求】
- 严格输出 JSON 格式，不要其他文字，不要 markdown 代码块
- JSON 结构：{{"action": "动作名称", "reason": "一句话说明理由", "detail_param": "可选参数"}}
- detail_param 说明：
  * quote_material_price → 传材料key（particle_board/multi_layer_board/eco_board/solid_wood）
  * recommend_material → 传JSON字符串：{{"material":"材料key", "reason":"推荐理由（一句话，只说主推材料的好处，别提其他材料价格）"}}
  * compare_materials → 传JSON字符串：{{"affirmed_material":"材料key", "affirm_reason":"...", "recommend_reason":"...", "choice_message":"..."}}
  * supplement_scene → 传JSON字符串：{{"scene":"场景名", "reason":"推荐理由"}}
  * answer_detail → 传问题类型关键词（如process_question/material_detail/hardware_detail等）
  * 其他动作 → 传空字符串
- 只能从上面14个动作中选一个，不能自创动作
- 选择动作时优先考虑：不要乱推进状态，用户没明确表示推进就停留在当前阶段
- Step1（已报价格区间）时，优先级：
  1) 用户说了场景+偏好 → recommend_material（推荐材料并报实价）
  2) 用户明确说某种材料 → quote_material_price（报指定材料价格）
  3) 用户重复问价格/没听懂 → restate_price_range（重述价格区间）
  不要把"用户说做哪些柜子/选什么档次"当成无关问题，这是在选材料的信号
"""

        user_msg = f"用户当前输入：{text}\n\n请选择动作并输出JSON："

        try:
            resp = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    "temperature": 0,
                },
                timeout=10,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()

            # 清理可能的 markdown 代码块标记
            content = content.replace("```json", "").replace("```", "").strip()

            # 解析 JSON
            result = json.loads(content)

            # 校验必填字段
            if "action" not in result:
                print(f"[LLM议价决策] 返回缺少action字段: {content}")
                return None

            # 校验动作合法性（从动作handler字典的key里取，避免重复维护）
            valid_actions = list(self.BARGAIN_ACTION_HANDLERS.keys())
            if result["action"] not in valid_actions:
                print(f"[LLM议价决策] 返回未知动作: {result['action']}")
                return None

            # 确保有 reason 和 detail_param 字段
            result.setdefault("reason", "")
            result.setdefault("detail_param", "")

            print(f"[LLM议价决策] action={result['action']}, reason={result['reason']}, param={result.get('detail_param', '')}")
            return result

        except Exception as e:
            print(f"[LLM议价决策降级] 调用失败: {e}")
            return None

    def _action_restate_price_range(self, detail_param):
        """
        动作：重述价格区间（Step1用户重复问价时）
        状态更新：bargain_step 不变
        """
        reply = self._render_bargain_template("bargain_price_range")
        return "bargain/restate_price_range", reply, {}

    def _action_recommend_material(self, detail_param):
        """
        动作：推荐材料并报实价
        detail_param：LLM返回的信息（可能包含scene/preference等）
        核心原则：材料和推荐理由必须从商家的推荐矩阵里取，不能用LLM自己编的
                 （LLM不知道商家卖什么材料，会乱推荐木质板）
        状态更新：bargain_step=2, selected_material=推荐材料
        """
        # 从推荐矩阵里查，优先用当前用户输入做场景+偏好检测
        text = getattr(self, "_current_bargain_text", "")

        # ===== 多场景推荐（优先级最高） =====
        # 检测到 >=2 个场景 → 走多场景分场景推荐逻辑
        if text:
            room_types = self._detect_room_types(text)
            if len(room_types) >= 2:
                pref_type = self._detect_preference_type(text)
                multi_rec = self._multi_scene_recommendation(text, preference=pref_type)
                if multi_rec and multi_rec.get("answer"):
                    # 多场景推荐成功 → 直接用返回的话术
                    # selected_material 取主材料（品质偏好下是主力档焊接大板，不是升级档）
                    scenes = multi_rec.get("scenes", [])
                    first_mat = multi_rec.get("main_material") or (scenes[0]["recommended_material"] if scenes else None)
                    reply = multi_rec["answer"]
                    # 加一句过渡开头，让话术更自然
                    reply = "根据您的情况，我给您分别说一下：\n" + reply
                    state_updates = {
                        "bargain_step": 2,
                        "selected_material": first_mat,
                        "bargain_pullback_count": 0,
                    }
                    print(f"  [多场景推荐] 命中{len(scenes)}个场景，主材料={first_mat}")
                    return "bargain/recommend_material", reply, state_updates

        rec_result = None
        if text:
            rec_result = self._get_recommendation(text)
        
        if rec_result:
            mat_key = rec_result["material"]
            recommend_reason = rec_result["reason"]
            # 私有矩阵 → 直接组装推荐话术
            if rec_result.get("is_private"):
                mat_name = rec_result["material_name"]
                mat_price = rec_result["material_price"]
                mix_match = rec_result.get("mix_match", "")
                parts = [
                    f"根据您的情况，我推荐用【{mat_name}】，{mat_price}一平。",
                    recommend_reason,
                ]
                if mix_match:
                    parts.append(mix_match)
                reply = "\n".join(parts)
                self.selected_material = mat_key
                return "bargain/recommend_material", reply, {
                    "bargain_step": 2,
                    "selected_material": mat_key,
                }
        else:
            # 检测不出来 → 用主推材料 + 默认推荐理由
            mat_key = self.config.get("main_material")
            # 从矩阵的default场景里找recommend_me的理由
            # 私有矩阵有就用私有矩阵的全屋主推，否则用公用矩阵
            if self.private_rec_matrix:
                scenes = self.private_rec_matrix.get("scenes", {})
                whole_house_cfg = None
                for name, cfg in scenes.items():
                    if cfg.get("scene_key") == "whole_house":
                        whole_house_cfg = cfg
                        break
                if whole_house_cfg:
                    main_push = whole_house_cfg.get("main_push")
                    if main_push:
                        mat_key = main_push
                        recommend_reason = whole_house_cfg.get("reason", "性价比高，家用合适")
                else:
                    recommend_reason = "性价比高，家用合适"
            else:
                matrix = self.rec_matrix.get("matrix", {})
                default_rec = matrix.get("default", {}).get("recommend_me", {})
                if isinstance(default_rec, dict):
                    recommend_reason = default_rec.get("reason", "性价比高，家用合适")
                else:
                    recommend_reason = "性价比高，家用合适"
        
        extra_vars = {"recommend_reason": recommend_reason}
        reply = self._render_bargain_template(
            "bargain_recommend_material",
            material=mat_key,
            extra_vars=extra_vars
        )
        state_updates = {"bargain_step": 2, "selected_material": mat_key, "bargain_pullback_count": 0}
        return "bargain/recommend_material", reply, state_updates

    def _action_quote_material_price(self, detail_param):
        """
        动作：报指定材料的实价
        detail_param：材料key（如 particle_board）
        状态更新：bargain_step=2, selected_material=指定材料
        """
        material = detail_param or self.config.get("main_material", "multi_layer_board")
        valid_materials = list(self.config.get("_board_keywords_map", {}).keys())
        
        # 校验材料合法性，不合法就用关键词匹配从用户输入里检测
        if material not in valid_materials and valid_materials:
            # LLM返回的key不对，用关键词匹配来检测
            text = getattr(self, "_current_bargain_text", "")
            if text:
                detected = self._detect_material_choice(text)
                if detected and detected in valid_materials:
                    material = detected
                else:
                    # 关键词也没检测到，降级到主推
                    material = self.config.get("main_material", valid_materials[0])
            else:
                material = self.config.get("main_material", valid_materials[0] if valid_materials else "multi_layer_board")
        
        reply = self._render_bargain_template("bargain_material_price", material=material)
        state_updates = {"bargain_step": 2, "selected_material": material, "bargain_pullback_count": 0}
        return "bargain/quote_material_price", reply, state_updates

    def _action_probe_area_demand(self, detail_param):
        """
        动作：摸底询问面积/需求
        状态更新：bargain_step=3
        """
        reply = self._render_bargain_template("bargain_probe")
        state_updates = {"bargain_step": 3, "bargain_pullback_count": 0}
        return "bargain/probe_area_demand", reply, state_updates

    def _action_answer_detail(self, detail_param):
        """
        动作：回答材料/工艺/五金等详情问题，回答后追加拉回议价的话术
        状态更新：bargain_step 不变
        """
        # 策略：先关键词匹配 → 再工艺匹配 → 再知识库检索 → 最后分类模板兜底
        detail_answer = None
        
        # 从v3保存的当前输入取用户原文
        text = getattr(self, "_current_bargain_text", "")
        
        if text:
            # 1) 关键词匹配hot_questions
            hot_result = self._match_hot_question(text)
            if hot_result:
                _, detail_answer = hot_result
            
            # 2) 工艺匹配
            if not detail_answer:
                process_match = self._match_process_by_keywords(text)
                if process_match:
                    pkey, pname, can_do, atpl = process_match
                    detail_answer = self._get_process_answer(pkey, pname, can_do, atpl)
            
            # 3) 知识库检索（高分才用）
            if not detail_answer:
                kb_results = self._search_knowledge_base(text, top_k=1)
                if kb_results and kb_results[0]["score"] > 3.0:
                    detail_answer = self._render(kb_results[0]["answer"])
        
        # 4) 分类模板渲染兜底
        if not detail_answer and detail_param:
            detail_answer = self._render_category_template(detail_param, text)
        if not detail_answer:
            # 兜底：用材料详情模板
            detail_answer = self._render_category_template("material_detail", text)
        if not detail_answer:
            detail_answer = "这个您放心，我们家用的都是达标材料，质量有保障。"
        
        # 答完后追加拉回议价的话术（根据当前step）
        follow_up = self._get_bargain_follow_up()
        full_answer = detail_answer + "\n" + follow_up
        
        return "bargain/answer_detail", full_answer, {"bargain_pullback_count": 0}

    def _action_value_build(self, detail_param):
        """
        动作：价值塑造（解释为什么这个价）
        状态更新：bargain_step 不变
        """
        # 用当前选中的材料来做价值塑造，不能用默认材料
        material = self.selected_material or self.config.get("main_material", "multi_layer_board")
        reply = self._render_bargain_template("bargain_value_build", material=material)
        return "bargain/value_build", reply, {"bargain_pullback_count": 0}

    def _action_give_discount(self, detail_param):
        """
        动作：给出优惠报价
        状态更新：bargain_step=4
        """
        # 用中单优惠模板兜底（实际订单大小由摸底结果决定，这里先给中单）
        material = self.selected_material or self.config.get("default_material", "particle_board")
        reply = self._render_bargain_template(
            "bargain_medium",
            material=material,
            order_desc="您这单"
        )
        reply = self._append_lead_hook(reply)
        state_updates = {"bargain_step": 4, "bargain_pullback_count": 0}
        return "bargain/give_discount", reply, state_updates

    def _action_pullback_topic(self, detail_param):
        """
        动作：拉回议价主题
        策略：先试试能不能回答事实类问题（店铺信息、售后、工期等），能答就先答再拉回
        状态更新：bargain_pullback_count + 1，达到阈值就引导加微信
        """
        text = getattr(self, "_current_bargain_text", "")
        
        # 先试试关键词匹配事实类问题，能答就先答再拉回
        detail_answer = None
        if text:
            # 关键词匹配
            hot_result = self._match_hot_question(text)
            if hot_result:
                _, detail_answer = hot_result
            # 工艺匹配
            if not detail_answer:
                process_match = self._match_process_by_keywords(text)
                if process_match:
                    pkey, pname, can_do, atpl = process_match
                    detail_answer = self._get_process_answer(pkey, pname, can_do, atpl)
            # 知识库检索（高分才用）
            if not detail_answer:
                kb_results = self._search_knowledge_base(text, top_k=1)
                if kb_results and kb_results[0]["score"] > 4.0:
                    detail_answer = self._render(kb_results[0]["answer"])
        
        new_count = self.bargain_pullback_count + 1
        
        # 有事实答案 → 先答再拉回（不增加pullback计数，因为回答了问题）
        if detail_answer:
            pullback = self._render_pullback_template(step=max(1, min(self.bargain_step, 4)))
            full_answer = detail_answer + "\n" + pullback
            # ponytail: 回答了事实问题就不算扯远，不增加计数，避免太早引导加微信
            return "bargain/pullback_topic", full_answer, {"bargain_pullback_count": self.bargain_pullback_count}
        
        # 答不上来 → 正常拉回正题，计数+1
        if new_count >= 2:
            # 连续2轮扯远 → 引导加微信
            reply = self._render_bargain_template("bargain_lead_wechat")
            return "bargain/lead_wechat", reply, {"bargain_pullback_count": new_count}
        # 否则拉回正题
        step = max(1, min(self.bargain_step, 4))
        reply = self._render_pullback_template(step=step)
        return "bargain/pullback_topic", reply, {"bargain_pullback_count": new_count}

    def _action_switch_topic(self, detail_param):
        """
        动作：切换出议价话题（用户明确不想聊价格了）
        状态更新：bargain_step=0，返回 (None, None) 让外层路由处理
        """
        return None, None, {"bargain_step": 0, "bargain_pullback_count": 0}

    def _action_lead_wechat(self, detail_param):
        """
        动作：引导加微信
        去重逻辑：已收集到微信/电话 → 换确认/邀约话术，不重复索要
        状态更新：bargain_step 不变
        """
        # 已有微信 → 不重复要，换邀约话术
        if self._has_collected("wechat"):
            reply = (
                "好的，我记下您的微信了~ 稍后我加您，详细报价单和案例我发您微信上，"
                "您有什么问题随时问我哈。"
            )
            return "bargain/lead_wechat_skip", reply, {}
        # 已有电话 → 不重复要，换邀约话术
        if self._has_collected("phone"):
            reply = (
                "好的，您的电话我记下了~ 稍后我让设计师跟您联系，"
                "免费给您出个方案和报价，您看方便什么时候安排量房？"
            )
            return "bargain/lead_wechat_skip", reply, {}
        reply = self._render_bargain_template("bargain_lead_wechat")
        return "bargain/lead_wechat", reply, {}

    def _action_advance_from_step2(self, detail_param):
        """
        动作：从Step2推进到Step3（摸底）
        去重逻辑：已收集到面积 → 跳过摸底，直接给优惠报价（Step4）
        状态更新：bargain_step=3 或 4
        """
        # 已有面积 → 跳过摸底，直接给优惠（跳到step4）
        if self._has_collected("area"):
            size_val = "medium"
            area = self.collected_info["area"]
            if area >= 30:
                size_val = "large"
            elif area < 6:
                size_val = "small"
            material = self.selected_material or ""
            order_desc = f"您家大约{area}平"
            reply = self._render_bargain_template(
                f"bargain_{size_val}", material=material, order_desc=order_desc
            )
            state_updates = {"bargain_step": 4, "bargain_pullback_count": 0}
            return "bargain/skip_probe", reply, state_updates
        reply = self._render_bargain_template("bargain_probe")
        state_updates = {"bargain_step": 3, "bargain_pullback_count": 0}
        return "bargain/advance_from_step2", reply, state_updates

    def _action_compare_materials(self, detail_param):
        """
        动作：回答材料对比/质疑类问题
        铁律：绝对不踩其他材料，先肯定对方，再解释推荐理由，把选择权还给用户
        detail_param：JSON字符串，包含 {affirmed_material, affirm_reason, recommend_reason, choice_message}
                    如果不是JSON或字段不全，用默认话术兜底
        状态更新：bargain_step 不变
        """
        import json

        # 默认值（detail_param 解析失败时用）
        affirmed_mat_key = ""
        affirm_reason = "也挺好的，性价比很高"
        recommend_reason = "主要看您的需求和预算"
        choice_message = "您要是预算有限选那款也完全够用"

        # 尝试解析 detail_param（LLM 应该返回 JSON）
        if detail_param:
            try:
                data = json.loads(detail_param)
                affirmed_mat_key = data.get("affirmed_material", "")
                if data.get("affirm_reason"):
                    affirm_reason = data["affirm_reason"]
                if data.get("recommend_reason"):
                    recommend_reason = data["recommend_reason"]
                if data.get("choice_message"):
                    choice_message = data["choice_message"]
            except Exception:
                # 解析失败就用默认值，不报错
                pass

        # 被肯定材料的中文名
        affirmed_mat_name = ""
        if affirmed_mat_key:
            affirmed_mat_name = get_material_name(affirmed_mat_key, self.config)

        # 当前推荐材料的中文名
        rec_mat_key = self.selected_material or self.config.get("main_material", "multi_layer_board")
        rec_mat_name = get_material_name(rec_mat_key, self.config)

        # 组装话术（结构：肯定对方 → 解释推荐理由 → 选择权还给用户）
        if affirmed_mat_name:
            # ponytail: LLM返回的affirm_reason可能已经包含了材料名，避免重复拼接
            if affirmed_mat_name in affirm_reason:
                part1 = f"{affirm_reason}，"
            else:
                part1 = f"{affirmed_mat_name}{affirm_reason}，"
        else:
            part1 = "这两种材料都挺好的，各有各的优势，"

        # 推荐理由也做同样的去重处理
        if rec_mat_name in recommend_reason:
            part2 = f"{recommend_reason}，所以我更推荐{rec_mat_name}。"
        else:
            part2 = f"主要是{recommend_reason}，所以我更推荐{rec_mat_name}。"

        part3 = choice_message

        full_answer = part1 + part2 + part3

        return "bargain/compare_materials", full_answer, {"bargain_pullback_count": 0}

    def _action_supplement_scene(self, detail_param):
        """
        动作：补充场景推荐（用户在Step2补充新房间/新场景）
        处理逻辑：
        1. 新场景也推荐同一种材料（保持推荐一致性）
        2. 说明为什么这种材料也适合新场景（从推荐矩阵/材料卖点里取，不用LLM生成，避免乱推荐）
        3. 重申价格+配置
        4. 继续引导摸底面积
        detail_param：JSON字符串，包含 {scene:"场景名", ...}
        状态更新：bargain_step 不变（仍为2），bargain_pullback_count 重置
        """
        import json

        if not self.selected_material:
            # 异常兜底：还没选材料就进了这个动作，走推荐流程
            return self._action_recommend_material(detail_param)

        # 默认值
        scene_name = "这个空间"
        
        # 尝试解析detail_param里的场景名
        if detail_param:
            try:
                data = json.loads(detail_param)
                if data.get("scene"):
                    scene_name = data["scene"]
            except Exception:
                pass
        
        # 如果LLM给的场景名太模糊，从用户输入里重新检测
        text = getattr(self, "_current_bargain_text", "")
        if text and scene_name in ["这个空间", "", "厨房"]:
            detected_key, detected_name = self._detect_room_type(text)
            if detected_name:
                scene_name = detected_name
        
        # 推荐理由：从材料卖点里拼，安全可控，不会出现LLM乱编的木质词汇
        # 用材料的环保/卖点作为核心理由
        material = self.selected_material
        reason = self._get_material_selling_point(material, scene_name)
        
        extra_vars = {
            "recommend_reason": reason,
            "scene_name": scene_name,
        }
        reply = self._render_bargain_template(
            "bargain_supplement_scene",
            material=material,
            extra_vars=extra_vars
        )

        # 状态不变，仍在Step2
        return "bargain/supplement_scene", reply, {"bargain_pullback_count": 0}

    def _action_confirm_intent(self, detail_param):
        """
        动作：反问确认用户意图
        状态更新：bargain_step 不变
        """
        confirm_templates = [
            "您具体是想了解哪方面呢？价格、材料还是工艺？",
            "不好意思没太明白，您是想问价格吗？还是想看材料？",
            "您可以说具体点，我好给您准确推荐。",
        ]
        reply = random.choice(confirm_templates)
        return "bargain/confirm_intent", reply, {}

    # 动作 → handler 映射表
    BARGAIN_ACTION_HANDLERS = {
        "restate_price_range": _action_restate_price_range,
        "recommend_material": _action_recommend_material,
        "quote_material_price": _action_quote_material_price,
        "probe_area_demand": _action_probe_area_demand,
        "answer_detail": _action_answer_detail,
        "value_build": _action_value_build,
        "give_discount": _action_give_discount,
        "pullback_topic": _action_pullback_topic,
        "switch_topic": _action_switch_topic,
        "lead_wechat": _action_lead_wechat,
        "advance_from_step2": _action_advance_from_step2,
        "confirm_intent": _action_confirm_intent,
        "compare_materials": _action_compare_materials,
        "supplement_scene": _action_supplement_scene,
    }

    def _bargain_fallback_current_step(self):
        """
        降级策略：LLM 决策失败时，返回当前阶段的默认话术，不推进状态
        兜底原则：宁可原地踏步重复话术，也不能乱推进状态
        返回：(tag, reply_text, state_updates)
        """
        step = self.bargain_step
        if step == 0:
            # 未开始 → 报价格区间，进Step1
            reply = self._render_bargain_template("bargain_price_range")
            return "bargain/fallback_price_range", reply, {"bargain_step": 1, "bargain_pullback_count": 0}
        elif step == 1:
            # Step1 → 重述价格区间，不推进
            reply = self._render_bargain_template("bargain_price_range")
            return "bargain/fallback_step1", reply, {}
        elif step == 2:
            # Step2 → 重述当前选中材料的价格，不推进
            material = self.selected_material or self.config.get("main_material", "multi_layer_board")
            reply = self._render_bargain_template("bargain_material_price", material=material)
            return "bargain/fallback_step2", reply, {}
        elif step == 3:
            # Step3 → 重述摸底话术，不推进
            reply = self._render_bargain_template("bargain_probe")
            return "bargain/fallback_step3", reply, {}
        else:
            # Step4+ → 重述优惠价，不推进
            material = self.selected_material or self.config.get("default_material", "particle_board")
            reply = self._render_bargain_template(
                "bargain_medium", material=material, order_desc="您这单"
            )
            reply = self._append_lead_hook(reply)
            return "bargain/fallback_step4", reply, {}

    def _handle_bargain_v3(self, text, llm_category):
        """
        议价状态机 v3（LLM决策层 + 动作执行层）
        核心流程：LLM决策 → 动作分发 → 更新状态 → 返回
        LLM 失败时降级到当前阶段默认应答（不推进状态）
        
        Args:
            text: 用户输入
            llm_category: LLM细分类结果（兼容旧接口，v3主要用自己的决策）
        
        Returns:
            (tag, answer) 或 (None, None) 表示退出状态机
        """
        # 保存当前用户输入，供动作处理器使用（比如answer_detail需要原文做关键词匹配）
        self._current_bargain_text = text
        import datetime
        print(f"[Bargain V3 Debug] time={datetime.datetime.now().strftime('%H:%M:%S')}")
        print(f"  用户输入: {text}")
        print(f"  LLM意图分类: {llm_category}")
        print(f"  当前bargain_step: {self.bargain_step}")
        print(f"  selected_material: {self.selected_material}")

        # Step 0 初次进入：直接报价格区间（不需要LLM决策，确定性最高）
        if self.bargain_step == 0:
            if llm_category in ("bargain", "complain_price"):
                # 砍价/嫌贵 → 先摸底（但如果已经知道面积，就直接报价不摸底）
                if self._has_collected("area"):
                    # 已有面积 → 跳过摸底，直接报区间+提面积
                    self.bargain_step = 1
                    self.bargain_pullback_count = 0
                    reply = self._render_bargain_template("bargain_price_range")
                    return "bargain/price_range", reply
                self.bargain_step = 3
                self.bargain_pullback_count = 0
                reply = self._render_bargain_template("bargain_probe")
                return "bargain/probe", reply
            # 问价格 → 报区间
            self.bargain_step = 1
            self.bargain_pullback_count = 0
            reply = self._render_bargain_template("bargain_price_range")
            return "bargain/price_range", reply

        # Step 1+：先检查是否是选材料信号，命中就直接推荐（优先级最高）
        # 原因：LLM意图分类有时会把"做橱柜和衣柜"归为product_type或事实类，导致走不到推荐
        # 只在 bargain_step == 1 时生效
        # 误判防护：纯疑问句（有吗/呢/怎么/怎样/如何/行不行/好不好/怎么样）且无选择信号的，跳过
        if self.bargain_step == 1:
            pref_type = self._detect_preference_type(text)
            room_types = self._detect_room_types(text)
            if pref_type or room_types:
                # 误判防护：纯疑问句且无选择信号 → 不触发推荐（如"环保吗""品质怎么样""有什么区别"）
                # ponytail: 疑问词不仅是吗/呢/怎么，还包括板材对比类（区别/哪个好/对比），
                # 否则"这两种板有什么区别"会被当成偏好信号触发推荐，完全跑偏
                pure_question_keywords = [
                    "吗", "呢", "怎么", "怎样", "如何", "行不行", "好不好", "怎么样",
                    "区别", "哪个好", "对比", "差别", "不一样", "优缺点", "有啥区别",
                    "什么区别", "哪种好", "选哪种", "有什么不同", "有啥不同",
                ]
                choice_signal_keywords = ["要", "选", "用", "想要", "给我来", "来个", "就要", "就用", "就选", "做", "你推荐"]
                is_pure_question = any(kw in text for kw in pure_question_keywords)
                has_choice_signal = any(kw in text for kw in choice_signal_keywords)
                if not (is_pure_question and not has_choice_signal):
                    # 命中 → 直接走推荐动作，不调LLM
                    print(f"  [Step1兜底推荐] 命中偏好={pref_type}, 场景数={len(room_types)}，直接走recommend_material")
                    tag, reply, state_updates = self._action_recommend_material("")
                    for key, value in state_updates.items():
                        setattr(self, key, value)
                    return tag, reply

        # ===== 议价状态下确定性信号优先检测（在事实问题检查之前）=====
        # 原因：事实问题预检查（hot_questions/工艺/知识库）有时会误匹配，
        # 把用户报面积/选材料/砍价等明确信号给截胡了，导致议价流程跑偏。
        # 原则：议价状态下，明确的流程推进信号 > 事实问题回答

        # 上一轮待确认面积 + 本轮回答了投影/展开 → 确认面积类型并报优惠
        # ponytail: 用户说"20平"→反问投影还是展开→用户说"投影的"，要能关联起来
        projection_answer_words = ["投影", "投影面积", "按投影", "投影算"]
        expanded_answer_words = ["展开", "展开面积", "按展开", "展开算", "实际面积", "按实际"]
        has_proj_answer = any(kw in text for kw in projection_answer_words)
        has_exp_answer = any(kw in text for kw in expanded_answer_words)

        if hasattr(self, '_pending_area') and self._pending_area and (has_proj_answer or has_exp_answer):
            # 有待确认的面积 + 用户回答了类型 → 重新计算并推进
            pending_val = self._pending_area['value']
            pending_raw = self._pending_area['raw']
            if has_proj_answer:
                # 投影面积 → ×2.5转展开
                real_area = pending_val * 2.5
                raw_display = f"投影{pending_raw}平(展开约{real_area:.0f}平)"
            else:
                real_area = pending_val
                raw_display = str(pending_raw)
            # 重新分档
            if real_area >= 50:
                size_val = "xlarge"
            elif real_area >= 30:
                size_val = "large"
            elif real_area >= 15:
                size_val = "medium"
            else:
                size_val = "small"
            order_desc = raw_display
            print(f"  [确定性信号] 用户确认面积类型={has_proj_answer and '投影' or '展开'}, 重新分档={size_val}，推进到Step4")
            if not self.selected_material:
                self.selected_material = self.config.get("default_material", "particle_board")
            self.bargain_step = 4
            self.bargain_pullback_count = 0
            self._pending_area = None  # 清掉待确认
            reply = self._render_bargain_template(
                f"bargain_{size_val}",
                material=self.selected_material,
                order_desc=order_desc
            )
            reply = self._append_lead_hook(reply)
            return f"bargain/{size_val}", reply

        size_val, input_type, raw_qty = self._detect_order_size(text)
        material = self._detect_material_choice(text)
        is_bargain = self._is_bargain_question(text)

        # Step2或Step3（已报价/摸底阶段）+ 检测到面积/数量 → 推进到step4报优惠价
        # 优先级最高，绝对不能被事实问题匹配截胡
        if self.bargain_step in (2, 3) and size_val:
            # ponytail: 没明确说投影还是展开的，先反问确认，避免算错面积
            projection_words = ["投影", "投影面积", "按投影算", "投影算"]
            expanded_words = ["展开", "展开面积", "按展开算", "实际面积", "按实际算"]
            has_projection = any(kw in text for kw in projection_words)
            has_expanded = any(kw in text for kw in expanded_words)

            if input_type == "area" and not has_projection and not has_expanded:
                # 只说了面积数字，没说是投影还是展开 → 先问清楚
                # 同时存下面积，等用户回答了类型后直接用
                try:
                    area_val = float(raw_qty) if raw_qty and isinstance(raw_qty, str) and raw_qty.replace('.', '').isdigit() else None
                except (ValueError, TypeError):
                    area_val = None
                if area_val:
                    self._pending_area = {'value': area_val, 'raw': raw_qty}
                else:
                    self._pending_area = None
                reply = "您说的是投影面积还是展开面积呀？展开大概是投影的2.5倍左右，这点我先跟您说清楚，免得后面报价有出入。"
                return "bargain/ask_area_type", reply

            # 明确说了投影/展开 → 推进到Step4报优惠
            order_desc = self._gen_order_desc(size_val, input_type, raw_qty)
            print(f"  [确定性信号] Step{self.bargain_step}检测到面积={size_val} ({order_desc})，直接推进到Step4")
            # 如果还没选材料，用默认材料
            if not self.selected_material:
                self.selected_material = self.config.get("default_material", "particle_board")
            self.bargain_step = 4
            self.bargain_pullback_count = 0
            reply = self._render_bargain_template(
                f"bargain_{size_val}",
                material=self.selected_material,
                order_desc=order_desc
            )
            reply = self._append_lead_hook(reply)
            return f"bargain/{size_val}", reply

        # 砍价信号（任何阶段）→ 跳过事实问题检查，直接交给LLM决策层
        # 原因：知识库经常把'能便宜吗'误匹配成'厨房能用吗'这类问题，导致砍价跑偏
        if is_bargain and self.bargain_step >= 1:
            print(f"  [确定性信号] 检测到砍价意图，跳过事实问题检查")
            fact_answer = None  # 确保不命中事实问题
        else:
            # 没有砍价信号 → 先检查pushback（嫌贵/竞品），命中直接返回
            # ponytail: pushback优先级高于事实问题，避免'太贵了'被hot_questions
            # 的'为什么别家便宜'误匹配截胡
            pushback_result = self._detect_bargain_pushback(text)
            if pushback_result:
                pushback_type, pushback_ans = pushback_result
                print(f"  [确定性信号] 命中pushback({pushback_type})，跳过事实问题检查")
                return f"bargain/pushback/{pushback_type}", pushback_ans

            # 正常做事实问题预检查
            fact_answer = None
            fact_category = None  # 记录事实问题分类，用于判断是否需要加拉回尾巴
            # 1) 关键词匹配hot_questions（已经自动跳过bargain_only和工艺类）
            hot_result = self._match_hot_question(text)
            if hot_result:
                hot_tag, hot_answer = hot_result
                # 排除材料推荐类问题（material_recommend_xxx），这些应该走议价推荐矩阵
                if "material_recommend" not in hot_tag:
                    fact_answer = hot_answer
                    fact_category = hot_tag
            # 2) 工艺匹配
            if not fact_answer:
                process_match = self._match_process_by_keywords(text)
                if process_match:
                    pkey, pname, can_do, atpl = process_match
                    fact_answer = self._get_process_answer(pkey, pname, can_do, atpl)
            # 3) 知识库检索（高分才用，阈值设高一点避免误伤议价流程）
            # ponytail: 额外限制——只有看起来是在提问的句子才走知识库检索
            # 陈述句（如'大概吧''20个平'）不检索，避免误匹配跑偏
            question_markers = ["?", "？", "吗", "呢", "怎么", "什么", "为什么", "怎样", "如何", "多少", "区别", "对比", "哪个好", "好不好", "行不行"]
            looks_like_question = any(m in text for m in question_markers)
            if not fact_answer and looks_like_question:
                kb_results = self._search_knowledge_base(text, top_k=1)
                if kb_results and kb_results[0]["score"] > 10.0:
                    fact_answer = self._render(kb_results[0]["answer"])
        
        # 命中事实问题 → 先回答，再追加议价拉回话术，状态不推进
        if fact_answer:
            follow_up = self._get_bargain_follow_up()
            # ponytail: 指代词反问类回答不需要加拉回尾巴
            # 比如"您说的是哪两种板材呀？"后面再加"什么时候量房"很奇怪
            if fact_category and "pronoun_clarify" in fact_category:
                full_answer = fact_answer
            else:
                full_answer = fact_answer + "\n" + follow_up
            return "bargain/answer_detail", full_answer

        # 然后才调用 LLM 决策
        decision = self._llm_bargain_decision(text)

        # Step 2 规则后置校验：补充场景推荐
        # ponytail: LLM有时会把"客厅用什么合适"这类新场景问题选错动作，
        # 规则校验：Step2 + 已选材料 + 检测到新场景 + 没说材料/确认/砍价 → 强制走 supplement_scene
        if self.bargain_step == 2 and self.selected_material and decision:
            scene_key, scene_name = self._detect_room_type(text)
            material = self._detect_material_choice(text)
            is_bargain = self._is_bargain_question(text)
            confirm_keywords = ["行", "可以", "还行", "好的", "没问题", "嗯", "ok", "OK"]
            is_confirm = any(kw in text for kw in confirm_keywords)
            if scene_key and not material and not is_bargain and not is_confirm:
                # LLM选的不是supplement_scene → 强制纠正
                if decision.get("action") != "supplement_scene":
                    # 用推荐矩阵的reason（去掉前缀）
                    rec_reason = "整体风格统一，施工也方便"
                    matrix = self.rec_matrix.get("matrix", {})
                    scene_config = matrix.get(scene_key, {})
                    if scene_config:
                        for pref in ["recommend_me", "balanced", "eco_friendly", "cost_effective"]:
                            if pref in scene_config and scene_config[pref].get("material") == self.selected_material:
                                raw_reason = scene_config[pref].get("reason", "")
                                # ponytail: reason里可能带"XX我推荐YY板"前缀，跟补充场景模板的"XX也推荐YY"重复了
                                # 把前缀去掉，只留核心理由（去掉开头到第一个逗号的内容，如果包含"推荐"字样）
                                clean_reason = raw_reason
                                if "推荐" in raw_reason and "，" in raw_reason:
                                    parts = raw_reason.split("，", 1)
                                    if len(parts) == 2 and ("推荐" in parts[0] or "首选" in parts[0]):
                                        clean_reason = parts[1].strip()
                                rec_reason = clean_reason
                                break
                    import json
                    decision["action"] = "supplement_scene"
                    decision["detail_param"] = json.dumps({"scene": scene_name, "reason": rec_reason}, ensure_ascii=False)

        if decision and decision.get("action") in self.BARGAIN_ACTION_HANDLERS:
            # LLM 决策成功 → 执行对应动作
            handler = self.BARGAIN_ACTION_HANDLERS[decision["action"]]
            tag, reply, state_updates = handler(self, decision.get("detail_param", ""))
            # 更新状态变量
            for key, value in state_updates.items():
                setattr(self, key, value)
            print(f"  决策结果: action={decision['action']}, reason={decision.get('reason', '')}")
            print(f"  状态更新: {state_updates}")
            return tag, reply
        else:
            # 降级：返回当前阶段默认话术，不推进状态
            print("  LLM决策失败，降级到当前阶段默认应答")
            tag, reply, state_updates = self._bargain_fallback_current_step()
            for key, value in state_updates.items():
                setattr(self, key, value)
            return tag, reply

    def _handle_bargain_v2(self, text, llm_category):
        """
        议价状态机 v2（旧版硬编码实现，已被v3替代，保留作为降级备选）
        状态机不再自己判断用户在说啥，直接用LLM分好的类来应答
        返回：(tag, answer) 或 (None, None)表示接不住，要退出状态机
        """
        # ===== Step 1 入口日志 =====
        # 打印关键信息，方便排查推荐逻辑不生效的问题
        import datetime
        scene_key, scene_name = self._detect_room_type(text)
        preference = self._detect_preference_type(text)
        print(f"[Bargain Step1 Debug] time={datetime.datetime.now().strftime('%H:%M:%S')}")
        print(f"  用户输入: {text}")
        print(f"  LLM意图分类: {llm_category}")
        print(f"  检测场景: scene_key={scene_key}, scene_name={scene_name}")
        print(f"  检测偏好: {preference}")
        print(f"  当前bargain_step: {self.bargain_step}")

        # LLM分类不是价格类 → 视情况决定是否退出状态机
        price_categories = ("price_query", "bargain", "complain_price")
        if llm_category not in price_categories:
            # Step 1 且有推荐结果 → 继续往下走推荐分支
            if self.bargain_step == 1:
                rec_result = self._get_recommendation(text)
                if rec_result:
                    print("  分支走向: Step1推荐逻辑（非价格类但命中推荐）")
                else:
                    print("  分支走向: 退出状态机（非价格类且无推荐）")
                    self.bargain_step = 0
                    self.bargain_pullback_count = 0
                    return None, None
            # Step >= 2 → 继续往下，尝试在当前Step内处理追问（不重置状态）
            elif self.bargain_step >= 2:
                print(f"  分支走向: Step{self.bargain_step}追问处理（非价格类，保持状态）")
            else:
                print("  分支走向: 退出状态机（非价格类且不在议价中）")
                self.bargain_step = 0
                self.bargain_pullback_count = 0
                return None, None

        # 检测推进信号
        material = self._detect_material_choice(text)
        size_val, input_type, raw_qty = self._detect_order_size(text)

        # ========== Step 0：初次进入 ==========
        if self.bargain_step == 0:
            if llm_category in ("bargain", "complain_price"):
                # 砍价/嫌贵 → 先摸底
                self.bargain_step = 3
                self.bargain_pullback_count = 0
                return "bargain/probe", self._render_bargain_template("bargain_probe")
            # 问价格 → 报区间
            self.bargain_step = 1
            self.bargain_pullback_count = 0
            return "bargain/price_range", self._render_bargain_template("bargain_price_range")

        # ========== Step 1：已报价格区间 ==========
        if self.bargain_step == 1:
            # 用户选了材料 → 报实价
            if material:
                self.bargain_step = 2
                self.selected_material = material
                self.bargain_pullback_count = 0
                return "bargain/material_price", self._render_bargain_template(
                    "bargain_material_price", material=material
                )

            # ===== 多场景推荐检测（优先级最高，在单场景推荐之前）=====
            room_types = self._detect_room_types(text)
            pref_type = self._detect_preference_type(text)

            # 有 >=2 个场景 + 有偏好 → 直接走多场景推荐
            if len(room_types) >= 2 and pref_type:
                multi_rec = self._multi_scene_recommendation(
                    text, selected_material=None, preference=pref_type
                )
                if multi_rec:
                    self.bargain_step = 2
                    self.selected_material = multi_rec["scenes"][0]["recommended_material"]
                    self.bargain_pullback_count = 0
                    return "bargain/multi_scene_recommend", multi_rec["answer"]

            # ===== 只有场景，没有偏好 → 追问偏好 =====
            if len(room_types) >= 1 and not pref_type and not material:
                ask_pref_templates = [
                    "好的，那您更看重性价比还是环保呀？我给您挑最合适的。",
                    "了解，您主要看重哪方面？性价比还是环保？",
                    "好的，您对板材有什么要求吗？看重环保还是性价比？",
                ]
                return "bargain/ask_preference", random.choice(ask_pref_templates)

            # ===== 只有偏好，没有场景 → 追问场景 =====
            if pref_type and len(room_types) == 0 and not material:
                ask_scene_templates = [
                    "好的，那您主要做哪些柜子呀？不同地方用的材料不一样。",
                    "了解，您家做哪些地方的柜子？我给您推荐最合适的。",
                    "好的，您主要用在哪些房间？我给您针对性推荐。",
                ]
                return "bargain/ask_scene", random.choice(ask_scene_templates)

            # ===== 二维推荐矩阵（场景 × 偏好）=====
            rec_result = self._get_recommendation(text)
            if rec_result:
                print(f"  _get_recommendation 返回: {rec_result}")
                mat_key = rec_result["material"]
                reason = rec_result["reason"]
                follow_up = rec_result["follow_up"]
                scene_key = rec_result["scene_key"]
                self.selected_material = mat_key
                self.bargain_step = 2
                self.bargain_pullback_count = 0
                print(f"  状态推进: bargain_step → {self.bargain_step}")

                # 私有推荐矩阵 → 用私有格式组装话术
                if rec_result.get("is_private"):
                    mat_name = rec_result["material_name"]
                    mat_price = rec_result["material_price"]
                    mix_match = rec_result.get("mix_match", "")
                    recommend_parts = [
                        f"根据您的情况，我推荐用【{mat_name}】，{mat_price}一平。",
                        reason,
                    ]
                    if mix_match:
                        recommend_parts.append(mix_match)
                    recommend_text = "\n".join(recommend_parts)
                    print("  私有矩阵推荐话术组装成功")
                else:
                    # 公用矩阵 → 走原有模板渲染
                    try:
                        recommend_text = self._render_bargain_template(
                            "bargain_recommendation_v2",
                            material=mat_key,
                            extra_vars={
                                "recommend_reason": reason,
                                "recommended_material_name": rec_result["material_name"],
                                "recommended_material_price": str(rec_result["material_price"]),
                                "board_brand": self.config.get("board_brand", ""),
                                "eco_level": self.config.get("eco_level", ""),
                                "hardware_brand": self.config.get("hardware_brand", ""),
                                "edge_banding": self.config.get("edge_band", ""),
                            }
                        )
                        print("  模板渲染: bargain_recommendation_v2 成功")
                    except Exception as e:
                        print(f"  模板渲染: bargain_recommendation_v2 失败: {e}")
                        # 渲染失败 → fallback到老模板
                        recommend_text = self._render_bargain_template(
                            "bargain_material_price", material=mat_key
                        )
                        print("  fallback到老模板: bargain_material_price")

                full_answer = recommend_text + "\n" + follow_up
                print(f"  最终回答: {full_answer[:80]}...")
                return f"bargain/recommend/{scene_key}", full_answer
            else:
                print("  _get_recommendation 返回: None（无推荐结果）")

            # 用户嫌贵/拿竞品比 → 价值塑造，不推进
            if llm_category == "complain_price":
                return "bargain/pushback/expensive", self._render_bargain_template("bargain_value_build")
            # 用户砍价 → 摸底问面积
            if llm_category == "bargain":
                self.bargain_step = 3
                self.bargain_pullback_count = 0
                return "bargain/probe", self._render_bargain_template("bargain_probe")

            # 用户说了面积 → 按默认材料报价，进step2
            if size_val:
                self.bargain_step = 2
                default_mat = self.config.get("default_material", "particle_board")
                self.selected_material = default_mat
                return "bargain/material_price", self._render_bargain_template(
                    "bargain_material_price", material=default_mat
                )
            # 其他 → 推荐主推款，推进到step2
            self.bargain_pullback_count = 0
            main_mat = self.config.get("main_material", "multi_layer_board")
            self.selected_material = main_mat
            self.bargain_step = 2
            return "bargain/material_price", self._render_bargain_template(
                "bargain_material_price", material=main_mat
            )

        # ========== Step 2：已报材料实价 ==========
        if self.bargain_step == 2:
            # 用户嫌贵/竞品 → 价值塑造
            if llm_category == "complain_price":
                return "bargain/pushback/expensive", self._render_bargain_template("bargain_value_build")
            # 用户砍价 → 摸底，进step3
            if llm_category == "bargain":
                self.bargain_step = 3
                self.bargain_pullback_count = 0
                return "bargain/probe", self._render_bargain_template("bargain_probe")
            # 用户又问价格 → 结合上下文，报当前选中材料的实价
            if llm_category == "price_query":
                mat = material or self.selected_material or self.config.get("main_material", "multi_layer_board")
                if material:
                    self.selected_material = material  # 用户换了材料，更新选中
                self.bargain_pullback_count = 0
                return "bargain/material_price", self._render_bargain_template(
                    "bargain_material_price", material=mat
                )
            # 用户换了材料 → 重报价，更新选中材料
            if material:
                self.selected_material = material
                return "bargain/material_price", self._render_bargain_template(
                    "bargain_material_price", material=material
                )

            # ===== Step2 多场景推荐：用户说了多个场景，逐个判断是否适合已选板材 =====
            room_types = self._detect_room_types(text)
            if len(room_types) >= 2 and self.selected_material:
                multi_rec = self._multi_scene_recommendation(
                    text, selected_material=self.selected_material
                )
                if multi_rec:
                    self.bargain_pullback_count = 0
                    # 如果有升级推荐，更新 selected_material 为第一个推荐的（主推荐）
                    if multi_rec["has_upgrade"]:
                        pass  # 先不改，保持用户选的，只做推荐建议
                    return "bargain/multi_scene_step2", multi_rec["answer"]

            # 用户说了面积 → 直接给优惠价，跳step4
            if size_val:
                self.bargain_step = 4
                self.bargain_pullback_count = 0
                order_desc = self._gen_order_desc(size_val, input_type, raw_qty)
                ans = self._render_bargain_template(
                    f"bargain_{size_val}",
                    material=material or self.config.get("default_material", "particle_board"),
                    order_desc=order_desc
                )
                ans = self._append_lead_hook(ans)
                return f"bargain/{size_val}", ans
            # ===== 议价中追问：问环保/材料/五金/工艺等 =====
            # 不推进状态，不计数pullback，正常回答后保持在Step 2
            detail_categories = ("material_detail", "hardware_detail", "process_question",
                                 "pricing_method", "after_sales", "shop_info",
                                 "shop_history", "measurement", "product_type")
            if llm_category in detail_categories:
                detail_answer = self._render_category_template(llm_category, text)
                if detail_answer:
                    self.bargain_pullback_count = 0
                    return f"bargain/detail/{llm_category}", detail_answer
            # 其他 → 拉回正题
            self.bargain_pullback_count += 1
            if self.bargain_pullback_count >= 2:
                return "bargain/lead_wechat", self._render_bargain_template("bargain_lead_wechat")
            return "bargain/pullback", self._render_pullback_template(step=2)

        # ========== Step 3：摸底问面积 ==========
        if self.bargain_step == 3:
            # 用户报了面积 → 给优惠价，进step4
            if size_val:
                self.bargain_step = 4
                self.bargain_pullback_count = 0
                order_desc = self._gen_order_desc(size_val, input_type, raw_qty)
                ans = self._render_bargain_template(
                    f"bargain_{size_val}",
                    material=material or self.config.get("default_material", "particle_board"),
                    order_desc=order_desc
                )
                ans = self._append_lead_hook(ans)
                return f"bargain/{size_val}", ans
            # 用户嫌贵/竞品 → 价值塑造
            if llm_category == "complain_price":
                return "bargain/pushback/expensive", self._render_bargain_template("bargain_value_build")
            # 用户还在砍价但不说面积 → 拉回
            if llm_category == "bargain":
                self.bargain_pullback_count += 1
                if self.bargain_pullback_count >= 2:
                    return "bargain/lead_wechat", self._render_bargain_template("bargain_lead_wechat")
                return "bargain/pullback", self._render_pullback_template(step=3)
            # 不知道/没量过 → 默认中单
            unknown_keywords = ["不知道", "没量", "没算过", "不清楚", "大概吧", "还没", "不确定"]
            if any(kw in text for kw in unknown_keywords):
                self.bargain_step = 4
                self.bargain_pullback_count = 0
                ans = self._render_bargain_template(
                    "bargain_medium",
                    material=material or self.config.get("default_material", "particle_board"),
                    order_desc="您这单"
                )
                ans = self._append_lead_hook(ans)
                return "bargain/medium", ans
            # 其他 → 默认中单推进（不跑丢）
            self.bargain_step = 4
            self.bargain_pullback_count = 0
            ans = self._render_bargain_template(
                "bargain_medium",
                material=material or self.config.get("default_material", "particle_board"),
                order_desc="您这单"
            )
            ans = self._append_lead_hook(ans)
            return "bargain/medium", ans

        # ========== Step 4+：已报优惠价 ==========
        if llm_category in ("bargain", "complain_price"):
            # 还在砍/嫌贵 → 升级话术
            self.bargain_step += 1
            self.bargain_pullback_count = 0
            return "bargain/upgrade", self._render_bargain_template("bargain_upgrade")
        # 其他 → 拉回或引导留资
        self.bargain_pullback_count += 1
        if self.bargain_pullback_count >= 2:
            return "bargain/lead_wechat", self._render_bargain_template("bargain_lead_wechat")
        return "bargain/pullback", self._render_pullback_template(step=4)

    def _reply_legacy(self, text):
        """
        主入口：
        高频关键词命中 → 工艺问题判断 → 议价状态机 → LLM意图识别 → 模板渲染 → 返回
        """
        tag = ""
        answer = ""

        # 0) 最高优先级：留资检测（客户留了联系方式，比啥都重要）
        contact_type = self._detect_contact_info(text)
        if contact_type is not None:
            self.llm_fallback_streak = 0
            self.bargain_step = 0
            self.bargain_pullback_count = 0

            # 手机号格式不对 → 提醒重发
            if contact_type == "phone_invalid":
                for hot_q in self.templates.get("hot_questions", []):
                    if hot_q.get("category") == "lead_phone_invalid":
                        templates_list = hot_q.get("templates", [])
                        if templates_list:
                            answer = self._render(random.choice(templates_list))
                            return "lead_phone_invalid", answer
                # 兜底
                return "lead_phone_invalid", "不好意思，这个手机号好像位数不对呢，您方便再核对下发我一下吗？"

            # 正确手机号 → 专属感谢话术 + 引导需求
            if contact_type == "phone":
                for hot_q in self.templates.get("hot_questions", []):
                    if hot_q.get("category") == "lead_phone_success":
                        templates_list = hot_q.get("templates", [])
                        if templates_list:
                            answer = self._render(random.choice(templates_list))
                            return "lead_capture/phone", answer

            # 其他留资（微信/地址等）→ 通用留资成功话术
            for hot_q in self.templates.get("hot_questions", []):
                if hot_q.get("category") == "lead_capture_success":
                    templates_list = hot_q.get("templates", [])
                    if templates_list:
                        answer = self._render(random.choice(templates_list))
                        return "lead_capture", answer
            # 兜底话术
            return "lead_capture", "好的收到，我记下了，一会就联系您。"

        # 0.5) 观望收尾检测（优先级比状态机高，软挽留+加微信）
        if self._is_soft_close(text):
            self.llm_fallback_streak = 0
            # 不重置状态机变量，用户回头还能接着聊
            for hot_q in self.templates.get("hot_questions", []):
                if hot_q.get("category") == "soft_close":
                    tpl_list = hot_q.get("templates", [])
                    if tpl_list:
                        answer = self._render(random.choice(tpl_list))
                        self.history.append({"user": text, "bot": answer})
                        self.history = self.history[-3:]
                        return "soft_close", answer
            # 兜底
            answer = f"行，您慢慢考虑。加我微信{self.config.get('wechat_id', '')}，有啥问题随时问我。"
            self.history.append({"user": text, "bot": answer})
            self.history = self.history[-3:]
            return "soft_close", answer

        # 1) LLM 意图路由（10分类）—— 判断用户问的是哪一类，决定走哪条路
        # 已在议价中 → 直接交给议价状态机，不再重复分类（状态机内部自己处理）
        # LLM失败 → 降级走关键词匹配（原来的逻辑）

        intent_num = None
        if self.bargain_step == 0:
            # 只有不在议价中时才用LLM做入口分类
            intent_num = self.classify_intent(text)

        # 1.5 关键词检测（LLM降级备用 + 议价状态机内部判断使用）
        hot_result = self._match_hot_question(text)
        is_bargain = self._is_bargain_question(text)
        is_price_question = self._is_price_question(text)

        # ========== 分支0.5：议价中反向检测——用户还在聊价格吗？不在就退出 ==========
        # （反向检测比正向检测靠谱：价格相关词是有限的，没命中就是换话题了）
        if self.bargain_step > 0 and not self._is_still_bargaining(text):
            # 用户换话题了，退出议价状态机
            self.bargain_step = 0
            self.bargain_pullback_count = 0
            # 继续往下走正常分类流程（不return）

        # ========== 分支1：已在议价中 且 本轮输入相关 → 走议价状态机 ==========
        is_bargain_rel = self._is_bargain_related(text)
        if self.bargain_step > 0 and is_bargain_rel:
            stage, answer = self._handle_bargain(text)
            self.llm_fallback_streak = 0
            tag = f"bargain/{stage}"
            # 保存上下文后返回
            self.history.append({"user": text, "bot": answer})
            self.history = self.history[-3:]
            return tag, answer

        # ========== 分支2：不在议价中 → 按LLM分类路由 ==========
        # LLM分类成功（1-10）
        if intent_num is not None:
            # 7=留资（理论上前面的关键词检测已经处理了，这里做双保险）
            if intent_num == 7:
                self.llm_fallback_streak = 0
                self.bargain_step = 0
                self.bargain_pullback_count = 0
                for hot_q in self.templates.get("hot_questions", []):
                    if hot_q.get("category") == "lead_capture_success":
                        tpl_list = hot_q.get("templates", [])
                        if tpl_list:
                            answer = self._render(random.choice(tpl_list))
                            self.history.append({"user": text, "bot": answer})
                            self.history = self.history[-3:]
                            return "lead_capture", answer

            # 1=问价格 / 2=砍价 → 进议价状态机
            if intent_num in (1, 2):
                stage, answer = self._handle_bargain(text)
                self.llm_fallback_streak = 0
                tag = f"bargain/{stage}"
                self.history.append({"user": text, "bot": answer})
                self.history = self.history[-3:]
                return tag, answer

            # 3=材料对比 → 走材料对比模板
            if intent_num == 3:
                self.llm_fallback_streak = 0
                for hot_q in self.templates.get("hot_questions", []):
                    if hot_q.get("category") == "material_compare":
                        tpl_list = hot_q.get("templates", [])
                        if tpl_list:
                            answer = self._render(random.choice(tpl_list))
                            tag = "material/compare"
                            self.history.append({"user": text, "bot": answer})
                            self.history = self.history[-3:]
                            return tag, answer

            # 4=材料细节 / 5=工艺问题 → 先关键词命中，答不上走知识库
            if intent_num in (4, 5):
                self.llm_fallback_streak = 0
                # 先尝试关键词模板
                if hot_result is not None:
                    tag, answer = hot_result
                    self.history.append({"user": text, "bot": answer})
                    self.history = self.history[-3:]
                    return tag, answer
                # 关键词没命中 → 降级走原LLM四分类兜底（会走知识库检索）
                # 交给下面的else分支

            # 6=量房/设计 → 量房模板
            if intent_num == 6:
                self.llm_fallback_streak = 0
                for hot_q in self.templates.get("hot_questions", []):
                    if hot_q.get("category") == "measurement_design":
                        tpl_list = hot_q.get("templates", [])
                        if tpl_list:
                            days = self._calc_delivery_days()
                            tpl = random.choice(tpl_list)
                            tpl = tpl.replace("{{total_days}}", str(days["total"]))
                            tpl = tpl.replace("{{design_days}}", str(days["design"]))
                            tpl = tpl.replace("{{production_days}}", str(days["production"]))
                            tpl = tpl.replace("{{install_days}}", str(days["install"]))
                            answer = self._render(tpl)
                            tag = "measurement/design"
                            self.history.append({"user": text, "bot": answer})
                            self.history = self.history[-3:]
                            return tag, answer

            # 8=产品类型 → 产品范围模板
            if intent_num == 8:
                self.llm_fallback_streak = 0
                for hot_q in self.templates.get("hot_questions", []):
                    if hot_q.get("category") == "product_type":
                        tpl_list = hot_q.get("templates", [])
                        if tpl_list:
                            answer = self._render(random.choice(tpl_list))
                            tag = "product/type"
                            self.history.append({"user": text, "bot": answer})
                            self.history = self.history[-3:]
                            return tag, answer

            # 9=闲聊 → 先匹配细分闲聊关键词，再走通用模板
            if intent_num == 9:
                self.llm_fallback_streak = 0
                # 先尝试匹配细分闲聊（打招呼/致谢/告别）
                chat_result = self._match_hot_question(text)
                if chat_result is not None:
                    cat, ans = chat_result
                    # 只处理闲聊类的细分（chat_前缀的category）
                    if cat.startswith("chat_"):
                        self.history.append({"user": text, "bot": ans})
                        self.history = self.history[-3:]
                        return cat, ans
                # 没匹配到细分 → 走通用chitchat模板
                for hot_q in self.templates.get("hot_questions", []):
                    if hot_q.get("category") == "chitchat":
                        tpl_list = hot_q.get("templates", [])
                        if tpl_list:
                            answer = self._render(random.choice(tpl_list))
                            tag = "chat/chitchat"
                            self.history.append({"user": text, "bot": answer})
                            self.history = self.history[-3:]
                            return tag, answer

            # 10=unclear / 其他 → 走原兜底逻辑（关键词→LLM四分类→盲区）

        # ========== 分支3：LLM分类失败 或 unclear → 降级走原关键词+LLM兜底逻辑 ==========
        # （保留原有的完整兜底链路，确保不丢客户）

        # 关键词前置命中（但议价和工艺类问题优先级更高，避免被截胡）
        if hot_result is not None:
            hot_tag, hot_ans = hot_result
            # 明显的价格/议价问题 → 优先进议价状态机（别被材料推荐等分类截胡）
            if is_bargain or is_price_question:
                # 只有当匹配到的不是议价类/计价类时才让位
                if not hot_tag.startswith(("bargain", "pricing", "projection", "drawer", "hidden_cost", "free_services", "same_board")):
                    stage, answer = self._handle_bargain(text)
                    self.llm_fallback_streak = 0
                    tag = f"bargain/{stage}"
                else:
                    self.llm_fallback_streak = 0
                    tag, answer = hot_result
            # 明显的"能不能做"类问题 → 先过工艺判断（别被材料推荐截胡）
            elif self._is_obscure_question(text) and not hot_tag.startswith("process/"):
                process_result = self._match_process_question(text)
                if process_result is not None:
                    self.llm_fallback_streak = 0
                    tag, answer = process_result
                else:
                    self.llm_fallback_streak = 0
                    tag, answer = hot_result
            else:
                self.llm_fallback_streak = 0
                tag, answer = hot_result
        else:
            # 工艺问题判断
            process_result = self._match_process_question(text)
            if process_result is not None:
                self.llm_fallback_streak = 0
                tag, answer = process_result
            elif is_bargain or is_price_question:
                # 议价状态机入口（关键词降级路径）
                stage, answer = self._handle_bargain(text)
                self.llm_fallback_streak = 0
                tag = f"bargain/{stage}"
            else:
                # LLM 四分类兜底（原来的逻辑）
                intent = self.detect_intent(text)
                self.llm_fallback_streak += 1

                # 盲区兜底检测
                if self.llm_fallback_streak >= 2 or self._is_obscure_question(text):
                    # 先试试知识库检索能不能答
                    kb_result = self._kb_answer_if_confident(text)
                    if kb_result:
                        tag, answer = kb_result
                        self.llm_fallback_streak = 0
                    else:
                        unknown_pool = self.templates.get("unknown_question", [])
                        if unknown_pool:
                            answer = self._render(random.choice(unknown_pool))
                            tag = "fallback/unknown"

                # 正常分类逻辑
                if not answer:
                    if intent == "chat":
                        self.chat_streak += 1
                        sub = "soft_end" if self.chat_streak >= 2 else self._sub_chat_type(text)
                        pool = self.templates["chat"][sub]
                        tag = f"chat/{sub}"
                    elif intent == "bargain":
                        stage, answer = self._handle_bargain(text)
                        self.llm_fallback_streak = 0
                        tag = f"bargain/{stage}_llm"
                    else:
                        self.chat_streak = 0
                        pool = self.templates[intent]
                        tag = intent

                    if not answer:
                        answer = self._render(random.choice(pool))

            # 5) 上下文记忆（最多 3 轮）
        self.history.append({"user": text, "bot": answer})
        self.history = self.history[-3:]

        # 6) 对话结束判断（仅日志 + 清上下文，红线：不发任何消息）
        if self.is_chat_ended(text):
            self.end_streak = 0
            self.history = []
            self.chat_streak = 0
            self.bargain_step = 0            # 对话结束，议价状态重置
            self.bargain_pullback_count = 0   # 对话结束，拉回计数重置
            self.llm_fallback_streak = 0     # 对话结束，盲区计数重置
            print("[日志] 对话已结束，上下文已清空")

        return tag, answer

    # ---------- 交互命令支持（status / clear） ----------
    def status(self):
        """返回当前对话状态字符串，供 test_cli.py 打印"""
        return (
            f"[status] 历史 {len(self.history)} 轮 | "
            f"连续闲扯 {self.chat_streak} | "
            f"连续结束语 {self.end_streak} | "
            f"议价阶段 {self.bargain_step} | "
            f"拉回计数 {self.bargain_pullback_count}"
        )

    def clear(self):
        """手动清空上下文与计数器"""
        self.history = []
        self.chat_streak = 0
        self.end_streak = 0
        self.bargain_step = 0
        self.bargain_pullback_count = 0
        self.llm_fallback_streak = 0
        return "[clear] 上下文已清空"
