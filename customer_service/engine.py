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
    get_material_price, get_material_name, MATERIAL_PRICE_KEY_MAP
)


class CustomerServiceEngine:
    """客服引擎 v2.0"""

    def __init__(self, shop_id: str = None):
        """初始化：加载模板库与商家配置，准备上下文与计数器

        Args:
            shop_id: 商家ID，不传则使用默认配置（向后兼容）
        """
        self.shop_id = shop_id
        with open(os.path.join(BASE_DIR, "templates.yaml"), encoding="utf-8") as f:
            self.templates = yaml.safe_load(f)
        # 根据 shop_id 加载对应商家配置（None 则用默认配置）
        self.config = load_shop_config(shop_id)
        self.history = []                # 上下文记忆，最多留 3 轮
        self.chat_streak = 0             # 连续闲扯计数器（软收尾用）
        self.end_streak = 0              # 连续结束语计数器（对话结束判断用）
        self.bargain_step = 0            # 议价状态机：0=未开始 1=报区间 2=报实价 3=摸底 4=已优惠 5+=升级
        self.bargain_pullback_count = 0  # 拉回正题计数器，连续2轮不正面回答就引导加微信
        self.llm_fallback_streak = 0     # LLM兜底连续次数（盲区检测用）
        self.llm_fallback_streak = 0     # LLM兜底连续次数（盲区检测用）

    # ---------- 通用辅助：抽取变量 + 渲染模板 ----------
    def _vars(self):
        """
        每次渲染前组装变量：硬参数 + 嵌套配置对象 + 4个随机抽取池
        嵌套对象直接传入，模板里用点号访问（如 {{payment_terms.deposit}}）
        """
        return {
            # —— 店铺基础信息 ——
            "shop_name": self.config["shop_name"],
            "boss_name": self.config["boss_name"],
            "years_in_business": self.config["years_in_business"],
            "city": self.config["city"],
            "shop_location": self.config["shop_location"],
            "wechat_id": self.config["wechat_id"],
            # —— 产品硬参数 ——
            "board_brand": self.config["board_brand"],
            "edge_band": self.config["edge_band"],
            "hardware_brand": self.config["hardware_brand"],
            "eco_level": self.config["eco_level"],
            # —— 嵌套配置对象（模板里用点号访问） ——
            "process_capability": self.config.get("process_capability", {}),
            "payment_terms": self.config.get("payment_terms", {}),
            "production_cycle": self.config.get("production_cycle", {}),
            "trust_points": self.config.get("trust_points", {}),
            # —— 随机抽取池（每次渲染抽一个） ——
            "concessions": random.choice(self.config.get("concessions", [""])),
            "urgency": random.choice(self.config.get("urgency_factors", [""])),
            "selling_point": random.choice(self.config.get("selling_points", [""])),
            # —— 留资钩子（先渲染钩子模板里的 {{wechat_id}}，再传入外层模板） ——
            "hook": self._render_lead_hook(),
            # —— 兼容旧模板的 warranty 字段 ——
            "warranty": self.config.get("warranty", ""),
        }

    def _render_lead_hook(self):
        """渲染单个留资钩子（钩子模板里也有 {{wechat_id}} 需要先渲染）"""
        hook_template = random.choice(self.config.get("lead_hooks", [""]))
        # 钩子模板里只有 wechat_id 变量，直接替换
        return hook_template.replace("{{wechat_id}}", self.config.get("wechat_id", ""))

    def _render(self, raw):
        """用 jinja2 渲染单条模板字符串"""
        return Template(raw).render(**self._vars())

    # ---------- 1) 高频问题前置命中（最高优先级，不走 LLM） ----------
    def _match_hot_question(self, text):
        """
        关键词命中就返回（分类标签, 渲染好的话术），否则返回 None
        跳过工艺类问题（有 process_key 的）和议价专属模板（bargain_only）
        匹配度最高优先（匹配关键词数多的优先，相同则关键词总长度长的优先）
        """
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
                answer = self._render(random.choice(templates_list))
                return best_match.get("category", "hot_question"), answer
        return None

    # ---------- 2) 工艺能力判断 ----------
    def _match_process_question(self, text):
        """
        匹配工艺关键词，识别客户问的是哪个工艺
        匹配度最高优先（匹配关键词数多的优先）
        查 shop_config 的 process_capability
        能做 → 渲染 yes_templates
        不能做 → 渲染 no_templates（替代方案话术）
        匹配不上 → 返回 None，走正常流程
        """
        best_match = None
        best_count = 0
        best_len = 0

        for hot_q in self.templates.get("hot_questions", []):
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
        面积分档：≤5平=小单，6-29平=中单，≥30平=大单
        """
        import re

        # 1. 全屋类（大单）
        large_keywords = [
            "全屋", "整体", "全部", "整套", "所有房间", "全屋定制",
            "整套房子", "全房", "所有柜子", "全部做", "全套"
        ]
        if any(kw in text for kw in large_keywords):
            return ("large", "whole_house", None)

        # 2. 面积检测（正则：数字 + 个(可选) + 平方/平米/平/㎡）
        area_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:个)?\s*(?:平方|平米|平|㎡)', text)
        if area_match:
            raw = area_match.group(1)
            area = float(raw)
            if area >= 30:
                return ("large", "area", raw)
            elif area >= 6:
                return ("medium", "area", raw)
            else:
                return ("small", "area", raw)

        # 3. 柜子数量检测
        # 小单关键词（1-2个）
        small_keywords = [
            "一个柜子", "两个柜子", "就一个", "就做一个", "就衣柜",
            "就鞋柜", "一二个", "一两个", "只做一个", "1个", "2个",
            "就一个柜子", "单个", "一个房间"
        ]
        # 中单关键词（3-5个）
        medium_keywords = [
            "三个柜子", "四个柜子", "五个柜子", "三四个", "四五个",
            "几个柜子", "两三", "三四", "四五", "3个", "4个", "5个",
            "几个", "两三个", "三四个柜子"
        ]
        # 尝试提取具体数字
        count_match = re.search(r'(\d+)\s*(?:个|套)', text)
        count_num = None
        if count_match:
            count_num = int(count_match.group(1))
            if count_num >= 6:
                return ("large", "count", count_match.group(1))
            elif count_num >= 3:
                return ("medium", "count", count_match.group(1))
            else:
                return ("small", "count", count_match.group(1))

        # 没有数字但有关键词
        if any(kw in text for kw in small_keywords):
            # 找个大概数量描述
            if "一个" in text or "1个" in text or "就一个" in text:
                return ("small", "count", "1")
            elif "两个" in text or "2个" in text:
                return ("small", "count", "2")
            return ("small", "count", "一两")
        if any(kw in text for kw in medium_keywords):
            if "三四个" in text or "三四" in text:
                return ("medium", "count", "三四")
            elif "四五个" in text or "四五" in text:
                return ("medium", "count", "四五")
            elif "3个" in text:
                return ("medium", "count", "3")
            elif "4个" in text:
                return ("medium", "count", "4")
            elif "5个" in text:
                return ("medium", "count", "5")
            return ("medium", "count", "三四")

        return (None, None, None)
        return (None, None, None)

    def _gen_order_desc(self, size_val, input_type, raw_quantity):
        """
        根据订单信息生成描述字符串，用于模板动态插入
        - 面积输入 → "10个平方"
        - 柜子数量输入 → "3个柜子"
        - 全屋类输入 → "全屋"
        - 判断不出 → "您这单"
        """
        if input_type == "area" and raw_quantity:
            return f"{raw_quantity}个平方"
        elif input_type == "count" and raw_quantity:
            return f"{raw_quantity}个柜子"
        elif input_type == "whole_house":
            return "全屋"
        else:
            return "您这单"

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
        is_price = self._is_price_question(text)
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

        # ========== Step 0：初次进入：先报价格区间 ==========
        if self.bargain_step == 0:
            self.bargain_step = 1
            self.bargain_pullback_count = 0
            return "price_range", self._render_bargain_template("bargain_price_range")

        # ========== Step 1：等用户选材料 ==========
        if self.bargain_step == 1:
            # 明确选了材料 → 报实价，进step2
            if material:
                self.bargain_step = 2
                self.bargain_pullback_count = 0
                return "material_price", self._render_bargain_template(
                    "bargain_material_price", material=material
                )

            # 说了某个房间/场景 → 场景化推荐 + 进step2（相当于帮用户选了推荐材料）
            room_type = self._detect_room_type(text)
            if room_type:
                # 场景推荐对应的默认材料
                room_default_materials = {
                    "kids_room": "ecological_board",    # 儿童房→生态板
                    "kitchen": "multi_layer_board",    # 厨房→多层板
                    "bathroom": "multi_layer_board",   # 卫生间/阳台→多层板
                    "bedroom": "particle_board",        # 卧室→颗粒板（默认）
                    "shoe_cabinet": "particle_board",   # 鞋柜等→颗粒板
                    "tatami": "ecological_board",       # 榻榻米→生态板
                }
                default_mat = room_default_materials.get(
                    room_type,
                    self.config.get("default_material", "particle_board")
                )
                # 先渲染场景推荐模板
                for hot_q in self.templates.get("hot_questions", []):
                    if hot_q.get("category") == f"material_recommend_{room_type}":
                        tpl_list = hot_q.get("templates", [])
                        if tpl_list:
                            recommend_text = self._render(tpl_list[0])
                            self.bargain_step = 2
                            self.bargain_pullback_count = 0
                            return f"recommend/{room_type}", recommend_text
                # 模板没找到就降级用材料报价
                self.bargain_step = 2
                self.bargain_pullback_count = 0
                return "material_price", self._render_bargain_template(
                    "bargain_material_price", material=default_mat
                )

            # 说了性价比偏好 → 推荐颗粒板
            if preference == "cost_effective":
                self.bargain_step = 2
                self.bargain_pullback_count = 0
                return "material_price", self._render_bargain_template(
                    "bargain_material_price", material="particle_board"
                )

            # 说了环保偏好 → 推荐生态板
            if preference == "eco_friendly":
                self.bargain_step = 2
                self.bargain_pullback_count = 0
                return "material_price", self._render_bargain_template(
                    "bargain_material_price", material="ecological_board"
                )

            # 平衡型/让推荐 → 推荐主推款（main_material）
            if preference == "balanced":
                main_mat = self.config.get("main_material", "multi_layer_board")
                self.bargain_step = 2
                self.bargain_pullback_count = 0
                return "material_price", self._render_bargain_template(
                    "bargain_material_price", material=main_mat
                )

            # 只说了面积/数量没说材料 → 按默认材料报价，进step2
            if size_val:
                default_mat = self.config.get("default_material", "particle_board")
                self.bargain_step = 2
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

            # 以上都没命中 → 推荐主推款（比追问更能推进转化）
            self.bargain_pullback_count += 1
            if self.bargain_pullback_count >= 2:
                return "lead_wechat", self._render_bargain_template("bargain_lead_wechat")
            main_mat = self.config.get("main_material", "multi_layer_board")
            self.bargain_step = 2
            return "material_price", self._render_bargain_template(
                "bargain_material_price", material=main_mat
            )

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

            # 说不知道/没量过 → 默认走中单（不跑丢），进step4
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
                return "medium", ans

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
        钩子随机选一条，引导话题固定
        """
        lead_hooks = [
            "对了，您加我微信{{wechat_id}}吧，我给您发份详细报价单，您回去也好对比对比。",
            "方便加个微信不{{wechat_id}}？后面有什么变动我直接跟您说。",
            "您加微信{{wechat_id}}我把材料图和实景案例给您发过去，您先看看效果。",
        ]
        hook = self._render(random.choice(lead_hooks))
        follow_up = "对了，还有什么想了解的不？板材、工艺、安装啥的都行。"
        return answer + "\n\n" + hook + "\n" + follow_up

    def _render_pullback_template(self, step):
        """
        渲染拉回正题话术（根据当前step选对应话术）
        bargain_pullback 模板组有4条，分别对应 step 1-4
        """
        for hot_q in self.templates.get("hot_questions", []):
            if hot_q.get("category") == "bargain_pullback":
                templates_list = hot_q.get("templates", [])
                if templates_list:
                    # step 1 对应索引0，step 2 对应索引1...
                    idx = max(0, min(step - 1, len(templates_list) - 1))
                    return self._render(templates_list[idx])
        # 兜底
        return "咱先把这事定下来？"

    def _render_bargain_template(self, category_name, material=None, order_desc=None):
        """
        从 hot_questions 中找到指定分类的模板，随机选一个渲染
        material 参数：选中的材料key，用于计算 material_name/material_price 等变量
        order_desc 参数：订单描述字符串，用于 {{order_desc}} 占位符
        """
        for hot_q in self.templates.get("hot_questions", []):
            if hot_q.get("category") == category_name:
                templates_list = hot_q.get("templates", [])
                if templates_list:
                    template_str = random.choice(templates_list)
                    # 如果有材料信息，组装额外变量
                    extra_vars = {}
                    if order_desc:
                        extra_vars["order_desc"] = order_desc
                    if material:
                        extra_vars["material_name"] = get_material_name(material)
                        extra_vars["material_price"] = get_material_price(self.config, material)
                        extra_vars["price_method"] = self.config.get("_base_price_method", "投影面积")
                        extra_vars["hardware"] = self.config.get("hardware_brand", "")
                        extra_vars["edge_band"] = self.config.get("edge_band", "")
                        extra_vars["price_range_low"], extra_vars["price_range_high"] = get_price_range(self.config)
                        extra_vars["materials_list"] = get_materials_list_text(self.config)
                    # 价格区间模板也需要这些变量
                    if category_name == "bargain_price_range":
                        extra_vars["price_range_low"], extra_vars["price_range_high"] = get_price_range(self.config)
                        extra_vars["materials_list"] = get_materials_list_text(self.config)
                    return self._render_with_extra(template_str, extra_vars)
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
        返回材料key：particle_board/multi_layer_board/ecological_board/solid_wood/osb_board
        识别不到返回 None
        """
        # 材料关键词映射（材料key → 关键词列表）
        material_keywords = {
            "particle_board": ["颗粒板", "刨花板", "799", "899"],
            "multi_layer_board": ["多层板", "胶合板", "1099", "1299"],
            "ecological_board": ["生态板", "免漆板", "1199", "1399"],
            "solid_wood": ["实木", "原木板", "1699", "1899"],
            "osb_board": ["OSB板", "欧松板", "osb", "OSB", "999", "1199"],
        }

        best_material = None
        best_count = 0
        for mat_key, keywords in material_keywords.items():
            count = sum(1 for kw in keywords if kw in text)
            if count > best_count:
                best_count = count
                best_material = mat_key

        return best_material if best_count > 0 else None

    def _detect_room_type(self, text):
        """
        检测用户说的是哪个房间/使用场景（场景化材料推荐用）
        返回：kids_room / kitchen / bathroom / bedroom / shoe_cabinet / tatami / None
        优先级高的在前（比如"儿童衣柜"应该命中儿童房而不是卧室）
        """
        # 按优先级排序（更具体的场景放前面）
        room_patterns = [
            ("kids_room", [
                "儿童房", "小孩房", "宝宝房", "孩子房间", "儿童衣柜", "孩子用",
                "儿童", "小孩", "宝宝",
            ]),
            ("tatami", [
                "榻榻米", "地台", "踏踏米", "和室",
            ]),
            ("kitchen", [
                "厨房", "橱柜", "厨柜", "厨房柜子", "灶台", "吊柜",
            ]),
            ("bathroom", [
                "卫生间", "洗手间", "卫浴柜", "浴室柜", "洗手台",
                "阳台", "阳台柜", "洗衣柜", "洗衣机柜",
            ]),
            ("shoe_cabinet", [
                "鞋柜", "餐边柜", "玄关柜", "酒柜", "门厅柜", "入户柜",
            ]),
            ("bedroom", [
                "卧室", "主卧", "次卧", "衣柜", "大衣柜", "衣帽间",
                "大衣橱", "衣橱",
            ]),
        ]

        for room_key, keywords in room_patterns:
            if any(kw in text for kw in keywords):
                return room_key
        return None

    def _detect_preference_type(self, text):
        """
        检测用户在选材料阶段的偏好类型
        返回：cost_effective（性价比）/ eco_friendly（环保）/ balanced（平衡/让推荐）/ None
        """
        # 性价比关键词
        cost_keywords = [
            "便宜", "性价比", "预算", "实惠", "省钱", "划算",
            "便宜点", "便宜的", "经济", "实惠点", "预算有限",
        ]
        # 环保关键词
        eco_keywords = [
            "环保", "孩子", "小孩", "孕妇", "无甲醛", "健康",
            "环保的", "环保点", "甲醛", "宝宝", "怀孕",
        ]
        # 平衡/让推荐关键词
        balanced_keywords = [
            "都行", "你推荐", "你看着办", "均衡", "都可以",
            "卖得最好", "最火的", "热门", "推荐一下", "你觉得",
            "差不多", "随便", "都要",
        ]

        if any(kw in text for kw in eco_keywords):
            return "eco_friendly"
        if any(kw in text for kw in cost_keywords):
            return "cost_effective"
        if any(kw in text for kw in balanced_keywords):
            return "balanced"
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

    def _detect_contact_info(self, text):
        """
        留资检测：识别用户是否留了联系方式
        返回：phone / wechat / address / appointment / None
        优先级：最高，放主逻辑最前面
        """
        import re

        # 1. 手机号正则（11位，1开头第二位3-9）
        if re.search(r'1[3-9]\d{9}', text):
            return "phone"

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
            question_words = ["吗", "怎么", "什么", "哪", "多少", "呢", "几", "要不要", "能不能"]
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

    def _is_bargain_question(self, text):
        """判断是不是议价相关问题（命中 bargain 类关键词）"""
        bargain_keywords = [
            "便宜", "优惠", "打折", "最低价", "能不能少", "砍价",
            "再便宜点", "太贵了", "价格高", "有没有优惠", "能便宜吗",
            "便宜点", "少点", "能不能优惠", "还能少吗", "再降点",
            "再打个折", "再少点", "不够便宜", "还是贵", "还是高",
            "多少钱", "怎么卖", "咋卖", "报价", "价格", "贵不贵",
        ]
        return any(kw in text for kw in bargain_keywords)

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
        缓存：Redis，key=intent:{md5(text)}，过期7天；Redis不可用时自动降级不缓存
        """
        import hashlib
        import re

        # Redis 缓存
        cache_key = "intent:" + hashlib.md5(text.strip().encode("utf-8")).hexdigest()
        redis_client = self._get_redis()
        if redis_client:
            try:
                cached = redis_client.get(cache_key)
                if cached:
                    return int(cached)
            except Exception:
                pass  # Redis挂了也不影响主流程

        sys_prompt = (
            "你是一个全屋定制客服的意图分类器。根据用户的问题，判断属于以下哪一类，只返回数字编号：\n"
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
            "只返回一个数字（1-10），不要解释，不要其他文字。"
        )
        try:
            resp = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": MODEL,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": f"用户说：{text}\n分类编号："},
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
        return any(kw in text for kw in obscure_keywords)

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
            # 从模板组找留资成功话术
            for hot_q in self.templates.get("hot_questions", []):
                if hot_q.get("category") == "lead_capture_success":
                    templates_list = hot_q.get("templates", [])
                    if templates_list:
                        answer = self._render(random.choice(templates_list))
                        return "lead_capture", answer
            # 兜底话术
            return "lead_capture", "好的收到，我记下了，一会就联系您。"

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

        # ========== 分支1：已在议价中 → 直接走议价状态机 ==========
        if self.bargain_step > 0:
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
                            answer = self._render(random.choice(tpl_list))
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

            # 9=闲聊 → 闲聊模板
            if intent_num == 9:
                self.llm_fallback_streak = 0
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

        # 关键词前置命中
        if hot_result is not None:
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
