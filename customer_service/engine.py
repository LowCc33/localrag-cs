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
    get_material_price, get_material_name
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
        # 加载二维推荐矩阵（场景 × 偏好 → 材料 + 理由）
        with open(os.path.join(BASE_DIR, "recommendation_matrix.yaml"), encoding="utf-8") as f:
            self.rec_matrix = yaml.safe_load(f)
        # 根据 shop_id 加载对应商家配置（None 则用默认配置）
        self.config = load_shop_config(shop_id)
        self.history = []                # 上下文记忆，最多留 3 轮
        self.chat_streak = 0             # 连续闲扯计数器（软收尾用）
        self.end_streak = 0              # 连续结束语计数器（对话结束判断用）
        self.bargain_step = 0            # 议价状态机：0=未开始 1=报区间 2=报实价 3=摸底 4=已优惠 5+=升级
        self.bargain_pullback_count = 0  # 拉回正题计数器，连续2轮不正面回答就引导加微信
        self.selected_material = None    # 当前选中/推荐的材料key（Step2及以后有值）
        self.llm_fallback_streak = 0     # LLM兜底连续次数（盲区检测用）

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
            # 砍价意图优先 → 直接摸底（不管有没有说材料/面积，先问清楚需求再报价）
            if is_bargain:
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
        follow_up = "有什么想了解的随时说哈，板材、工艺、安装啥的都行。"
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
        room_patterns = [
            ("kids_room", [
                "儿童房", "小孩房", "宝宝房", "孩子房间", "儿童衣柜", "孩子用",
                "儿童", "小孩", "宝宝", "儿子房", "女儿房",
            ]),
            ("tatami", [
                "榻榻米", "地台", "踏踏米", "和室", "书房",
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
            ("shoe_cabinet", [
                "鞋柜", "玄关", "入户", "门厅", "玄关柜", "酒柜", "门厅柜", "入户柜",
            ]),
            ("living_room", [
                "客厅", "电视柜", "餐边柜", "背景墙",
            ]),
            ("whole_house", [
                "全屋", "整套", "整体", "全套", "家里", "整屋",
            ]),
            ("bedroom_wardrobe", [
                "卧室", "主卧", "次卧", "衣柜", "大衣柜", "衣帽间",
                "大衣橱", "衣橱",
            ]),
        ]

        for room_key, keywords in room_patterns:
            if any(kw in text for kw in keywords):
                scene_name = self.rec_matrix.get("scene_names", {}).get(room_key, room_key)
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
        room_patterns = [
            ("kids_room", [
                "儿童房", "小孩房", "宝宝房", "孩子房间", "儿童衣柜", "孩子用",
                "儿童", "小孩", "宝宝", "儿子房", "女儿房",
            ]),
            ("tatami", [
                "榻榻米", "地台", "踏踏米", "和室", "书房",
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
            ("shoe_cabinet", [
                "鞋柜", "玄关", "入户", "门厅", "玄关柜", "酒柜", "门厅柜", "入户柜",
            ]),
            ("living_room", [
                "客厅", "电视柜", "餐边柜", "背景墙",
            ]),
            ("whole_house", [
                "全屋", "整套", "整体", "全套", "家里", "整屋",
            ]),
            ("bedroom_wardrobe", [
                "卧室", "主卧", "次卧", "衣柜", "大衣柜", "衣帽间",
                "大衣橱", "衣橱",
            ]),
        ]

        results = []
        seen_keys = set()

        for room_key, keywords in room_patterns:
            if any(kw in text for kw in keywords):
                # 全屋优先，命中了就直接返回单个全屋
                if room_key == "whole_house":
                    scene_name = self.rec_matrix.get("scene_names", {}).get(room_key, room_key)
                    return [(room_key, scene_name)]
                if room_key not in seen_keys:
                    scene_name = self.rec_matrix.get("scene_names", {}).get(room_key, room_key)
                    results.append((room_key, scene_name))
                    seen_keys.add(room_key)
                    if len(results) >= 4:
                        break

        return results

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
        scene_key, scene_name = self._detect_room_type(text)
        preference = self._detect_preference_type(text)

        # 都没有 → 走原有逻辑
        if not scene_key and not preference:
            return None

        # 默认场景：default（通用推荐，不脑补具体房间）
        if not scene_key:
            scene_key = "default"
            scene_name = self.rec_matrix.get("scene_names", {}).get(scene_key, "通用")

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

    def _get_scene_recommendation(self, scene_key, preference="cost_effective"):
        """
        查单个场景的推荐板材
        返回：(material_key, material_name, material_price, reason) 或 (None, None, None, None)
        """
        from customer_service.shop_config_loader import get_material_name, get_material_price

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

        return answer + follow_up

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
        # 1. 检测所有场景
        room_types = self._detect_room_types(text)

        # 场景数 < 2 → 返回None，走单场景逻辑
        if len(room_types) < 2:
            return None

        # 全屋场景只有1个的话，也不走多场景
        if len(room_types) == 1 and room_types[0][0] == "whole_house":
            return None

        # 2. 默认偏好 = 性价比（如果没指定）
        if not preference:
            preference = "cost_effective"

        # 3. 逐个场景查推荐
        scenes = []
        for scene_key, scene_name in room_types:
            mat_key, mat_name, mat_price, reason = self._get_scene_recommendation(
                scene_key, preference
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
            })

        if len(scenes) < 2:
            return None

        # 4. 生成话术
        has_selected = selected_material is not None
        has_upgrade = any(not s["is_same_as_selected"] for s in scenes)
        answer = self._build_multi_scene_answer(scenes, has_selected)

        return {
            "scenes": scenes,
            "has_upgrade": has_upgrade,
            "answer": answer,
            "follow_up": "您这些柜子加起来大概多大？",
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
            "太贵", "这么贵", "好贵", "有点贵", "价格高", "不便宜",
            "真贵", "也太贵", "太贵了", "贵了点", "价钱高",
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

    def _is_bargain_question(self, text):
        """判断是不是砍价/要优惠的问题（只有明确砍价信号才算）
        注意：纯问价（多少钱、价格、报价）不算，归 _is_price_question 管
        """
        bargain_keywords = [
            "便宜", "优惠", "打折", "最低价", "能不能少", "砍价",
            "再便宜点", "太贵了", "价格高", "有没有优惠", "能便宜吗",
            "便宜点", "少点", "能不能优惠", "还能少吗", "再降点",
            "再打个折", "再少点", "不够便宜", "还是贵", "还是高",
            "贵不贵", "贵了", "太贵", "最低多少", "能降吗",
            "能再便宜", "能少点吗", "便宜点呗",
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
        import re

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
13. product_type - 用户问产品种类/业务范围，如能做什么柜子、有榻榻米吗、做橱柜吗
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
            for hot_q in self.templates.get("hot_questions", []):
                if hot_q.get("category") == "lead_capture_success":
                    tpl_list = hot_q.get("templates", [])
                    if tpl_list:
                        import random
                        from jinja2 import Template
                        answer = Template(random.choice(tpl_list)).render(**self._vars())
                        self.history.append({"user": text, "bot": answer})
                        self.history = self.history[-3:]
                        return "lead_capture", answer

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
                "您房子在哪？我看看有没有附近的案例。",
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
            return random.choice(lead_hooks)
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
14. supplement_scene - 补充场景推荐。适用：Step2时用户补充新场景/新房间（如"还有卧室呢""厨房也做""衣柜也要"），表示在问新场景用什么材料多少钱；detail_param传推荐理由（一句话，说明为什么当前材料也适合这个新场景，口语化）
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
        detail_param：JSON字符串 {"material": "材料key", "reason": "推荐理由"}
                    或纯字符串（当理由用，材料取主推）
        设计：LLM根据用户偏好选材料（便宜→颗粒板，环保→多层板等），不再写死main_material
        状态更新：bargain_step=2, selected_material=推荐材料
        """
        import json

        # 默认值：主推材料 + 默认理由
        mat_key = self.config.get("main_material", "multi_layer_board")
        recommend_reason = "性价比很高，家用完全够"

        # 解析 detail_param
        if detail_param:
            try:
                data = json.loads(detail_param)
                # LLM指定了材料 → 用LLM选的（必须在配置的材料列表里）
                if data.get("material"):
                    suggested = data["material"]
                    # 校验材料是否在配置中（_boards 是 list，每个元素有key字段）
                    boards = self.config.get("_boards", [])
                    valid_keys = [b["key"] for b in boards if isinstance(b, dict) and "key" in b]
                    if suggested in valid_keys:
                        mat_key = suggested
                # LLM写了理由
                if data.get("reason"):
                    recommend_reason = data["reason"]
            except Exception:
                # 不是JSON，整个当理由用
                recommend_reason = detail_param.strip() or recommend_reason

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
        # 校验材料合法性，不合法就用主推
        valid_materials = list(self.config.get("_board_keywords_map", {}).keys())
        if material not in valid_materials and valid_materials:
            material = self.config.get("main_material", "multi_layer_board")
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
        # 复用现有的分类模板渲染逻辑
        detail_answer = None
        if detail_param:
            detail_answer = self._render_category_template(detail_param)
        if not detail_answer:
            # 兜底：用材料详情模板
            detail_answer = self._render_category_template("material_detail")
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
        reply = self._render_bargain_template("bargain_value_build")
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
        状态更新：bargain_pullback_count + 1，达到阈值就引导加微信
        """
        new_count = self.bargain_pullback_count + 1
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
        状态更新：bargain_step 不变
        """
        reply = self._render_bargain_template("bargain_lead_wechat")
        return "bargain/lead_wechat", reply, {}

    def _action_advance_from_step2(self, detail_param):
        """
        动作：从Step2推进到Step3（摸底）
        状态更新：bargain_step=3
        """
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
        2. 说明为什么这种材料也适合新场景（LLM动态生成理由）
        3. 重申价格+配置
        4. 继续引导摸底面积
        detail_param：JSON字符串，包含 {scene:"场景名（如卧室/厨房/衣柜）", reason:"推荐理由"}
                    或直接传理由字符串（兜底）
        状态更新：bargain_step 不变（仍为2），bargain_pullback_count 重置
        """
        import json

        if not self.selected_material:
            # 异常兜底：还没选材料就进了这个动作，走推荐流程
            return self._action_recommend_material(detail_param)

        # 默认值
        scene_name = "这个空间"
        recommend_reason = "用一样的材料整体风格统一，也方便施工"

        # 尝试解析 detail_param（优先解析为JSON）
        if detail_param:
            try:
                data = json.loads(detail_param)
                if data.get("scene"):
                    scene_name = data["scene"]
                if data.get("reason"):
                    recommend_reason = data["reason"]
            except Exception:
                # 不是JSON，把整个detail_param当理由用
                recommend_reason = detail_param.strip() or recommend_reason

        extra_vars = {
            "recommend_reason": recommend_reason,
            "scene_name": scene_name,
        }
        reply = self._render_bargain_template(
            "bargain_supplement_scene",
            material=self.selected_material,
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
        import datetime
        print(f"[Bargain V3 Debug] time={datetime.datetime.now().strftime('%H:%M:%S')}")
        print(f"  用户输入: {text}")
        print(f"  LLM意图分类: {llm_category}")
        print(f"  当前bargain_step: {self.bargain_step}")
        print(f"  selected_material: {self.selected_material}")

        # Step 0 初次进入：直接报价格区间（不需要LLM决策，确定性最高）
        if self.bargain_step == 0:
            if llm_category in ("bargain", "complain_price"):
                # 砍价/嫌贵 → 先摸底
                self.bargain_step = 3
                self.bargain_pullback_count = 0
                reply = self._render_bargain_template("bargain_probe")
                return "bargain/probe", reply
            # 问价格 → 报区间
            self.bargain_step = 1
            self.bargain_pullback_count = 0
            reply = self._render_bargain_template("bargain_price_range")
            return "bargain/price_range", reply

        # Step 1+：调用 LLM 决策
        decision = self._llm_bargain_decision(text)

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
                    print(f"  分支走向: Step1推荐逻辑（非价格类但命中推荐）")
                else:
                    print(f"  分支走向: 退出状态机（非价格类且无推荐）")
                    self.bargain_step = 0
                    self.bargain_pullback_count = 0
                    return None, None
            # Step >= 2 → 继续往下，尝试在当前Step内处理追问（不重置状态）
            elif self.bargain_step >= 2:
                print(f"  分支走向: Step{self.bargain_step}追问处理（非价格类，保持状态）")
            else:
                print(f"  分支走向: 退出状态机（非价格类且不在议价中）")
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
                    print(f"  模板渲染: bargain_recommendation_v2 成功")
                except Exception as e:
                    print(f"  模板渲染: bargain_recommendation_v2 失败: {e}")
                    # 渲染失败 → fallback到老模板
                    recommend_text = self._render_bargain_template(
                        "bargain_material_price", material=mat_key
                    )
                    print(f"  fallback到老模板: bargain_material_price")
                full_answer = recommend_text + "\n" + follow_up
                print(f"  最终回答: {full_answer[:80]}...")
                return f"bargain/recommend/{scene_key}", full_answer
            else:
                print(f"  _get_recommendation 返回: None（无推荐结果）")

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
            # 从模板组找留资成功话术
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
