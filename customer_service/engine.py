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
from customer_service.shop_config_loader import load_shop_config


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
        self.bargain_step = 0            # 议价状态机：0=未开始 1=已摸底 2=已分档 3=已升级
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
        返回：'large'=大单(全屋) 'medium'=中单(3-5个) 'small'=小单(1-2个) None=判断不出
        """
        # 大单关键词
        large_keywords = [
            "全屋", "整体", "全部", "整套", "所有房间", "全屋定制",
            "整套房子", "全房", "所有柜子", "全部做", "全套"
        ]
        # 小单关键词
        small_keywords = [
            "一个柜子", "两个柜子", "就一个", "就做一个", "就衣柜",
            "就鞋柜", "一二个", "一两个", "只做一个", "1个", "2个",
            "就一个柜子", "单个", "一个房间"
        ]
        # 中单关键词
        medium_keywords = [
            "三个柜子", "四个柜子", "五个柜子", "三四个", "四五个",
            "几个柜子", "两三", "三四", "四五", "3个", "4个", "5个",
            "几个", "两三个", "三四个柜子"
        ]

        if any(kw in text for kw in large_keywords):
            return "large"
        if any(kw in text for kw in small_keywords):
            return "small"
        if any(kw in text for kw in medium_keywords):
            return "medium"
        return None

    def _handle_bargain(self, text):
        """
        议价状态机处理
        返回：(阶段名, 话术内容)
        状态流转：0(未开始) → 1(已摸底) → 2(已分档) → 3(已升级)
        同一客户连续问价格相关问题，逐步深入，不重复
        """
        # 从历史里找单值线索（如果本轮判断不出，看历史有没有）
        order_size = self._detect_order_size(text)
        if order_size is None:
            for h in self.history:
                size = self._detect_order_size(h["user"])
                if size:
                    order_size = size
                    break

        if self.bargain_step == 0:
            # 第一步：如果能直接判断单值大小，直接跳分档；否则摸底
            if order_size == "large":
                self.bargain_step = 2
                return "large", self._render_bargain_template("bargain_large")
            elif order_size == "small":
                self.bargain_step = 2
                return "small", self._render_bargain_template("bargain_small")
            elif order_size == "medium":
                self.bargain_step = 2
                return "medium", self._render_bargain_template("bargain_medium")
            else:
                self.bargain_step = 1
                return "probe", self._render_bargain_template("bargain_probe")

        elif self.bargain_step == 1:
            # 第二步：根据单值分档
            self.bargain_step = 2
            if order_size == "large":
                return "large", self._render_bargain_template("bargain_large")
            elif order_size == "small":
                return "small", self._render_bargain_template("bargain_small")
            else:
                # 判断不出默认走中单（保守策略）
                return "medium", self._render_bargain_template("bargain_medium")

        else:  # bargain_step >= 2
            # 第三步及以后：升级话术（搬出老板）
            return "upgrade", self._render_bargain_template("bargain_upgrade")

    def _render_bargain_template(self, category_name):
        """从 hot_questions 中找到指定分类的模板，随机选一个渲染"""
        for hot_q in self.templates.get("hot_questions", []):
            if hot_q.get("category") == category_name:
                templates_list = hot_q.get("templates", [])
                if templates_list:
                    return self._render(random.choice(templates_list))
        # 找不到兜底
        return "价格好商量，您具体有什么需求？"

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

        # 1) 高频关键词前置命中（最高优先级，跳过工艺类和议价专属）
        # 只要匹配到就返回，不管是不是议价相关（具体场景比通用议价更精准）
        hot_result = self._match_hot_question(text)
        is_bargain = self._is_bargain_question(text)
        has_size_clue = self._detect_order_size(text) is not None

        if hot_result is not None:
            self.llm_fallback_streak = 0
            tag, answer = hot_result
        else:
            # 2) 工艺问题判断
            process_result = self._match_process_question(text)
            if process_result is not None:
                self.llm_fallback_streak = 0
                tag, answer = process_result
            elif is_bargain or (self.bargain_step > 0 and has_size_clue):
                # 3) 议价多轮状态机
                # 触发条件：① 有议价关键词 ② 或 已在议价中且有数量线索
                stage, answer = self._handle_bargain(text)
                self.llm_fallback_streak = 0
                tag = f"bargain/{stage}"
            else:
                # 4) LLM 意图分类（兜底）
                intent = self.detect_intent(text)
                self.llm_fallback_streak += 1

        # 盲区兜底检测：连续2轮LLM兜底 或 偏门开放式问题
        if self.llm_fallback_streak >= 2 or self._is_obscure_question(text):
            unknown_pool = self.templates.get("unknown_question", [])
            if unknown_pool:
                answer = self._render(random.choice(unknown_pool))
                tag = "fallback/unknown"

        # 正常分类逻辑（盲区兜底没命中才走）
        if not answer:
            if intent == "chat":
                self.chat_streak += 1
                sub = "soft_end" if self.chat_streak >= 2 else self._sub_chat_type(text)
                pool = self.templates["chat"][sub]
                tag = f"chat/{sub}"
            elif intent == "bargain":
                # LLM 识别为议价但关键词没命中，走状态机
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
            f"议价阶段 {self.bargain_step}"
        )

    def clear(self):
        """手动清空上下文与计数器"""
        self.history = []
        self.chat_streak = 0
        self.end_streak = 0
        self.bargain_step = 0
        self.llm_fallback_streak = 0
        return "[clear] 上下文已清空"
