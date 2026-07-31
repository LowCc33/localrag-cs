# -*- coding: utf-8 -*-
"""
全屋定制客服引擎 v1.1
职责：高频问题前置命中 → 4 分类意图识别（调火山引擎 DeepSeek）→ 模板随机选取
      → 变量填充（卖点/钩子/让利/紧迫感随机抽取）→ 上下文记忆（最多 3 轮）
      → 软收尾触发 + 对话结束判断（仅日志 + 清上下文，不发任何消息）
架构位置：独立 MVP，不依赖 LocalRAG-CS，纯命令行调用
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


class CustomerServiceEngine:
    """客服引擎 v1.1"""

    def __init__(self):
        """初始化：加载模板库与商家配置，准备上下文与计数器（两个独立）"""
        with open(os.path.join(BASE_DIR, "templates.yaml"), encoding="utf-8") as f:
            self.templates = yaml.safe_load(f)
        with open(os.path.join(BASE_DIR, "shop_config.yaml"), encoding="utf-8") as f:
            self.config = yaml.safe_load(f)
        self.history = []                # 上下文记忆，最多留 3 轮
        self.chat_streak = 0             # 连续闲扯计数器（软收尾用，v1 沿用）
        self.end_streak = 0              # 连续结束语计数器（对话结束判断用，v1.1 新增，独立）

    # ---------- 通用辅助：抽取变量 + 渲染模板 ----------
    def _vars(self):
        """每次渲染前组装变量：硬参数 + 4 个随机抽取池"""
        return {
            "board_brand": self.config["board_brand"],
            "edge_band": self.config["edge_band"],
            "hardware_brand": self.config["hardware_brand"],
            "eco_level": self.config["eco_level"],
            "years_in_business": self.config["years_in_business"],
            "shop_name": self.config["shop_name"],
            "city": self.config["city"],
            "shop_location": self.config["shop_location"],
            "boss_name": self.config["boss_name"],
            "warranty": self.config["warranty"],
            "concessions": random.choice(self.config["concessions"]),
            "urgency": random.choice(self.config["urgency_factors"]),
            "hook": random.choice(self.config["lead_hooks"]),
            "selling_point": random.choice(self.config["selling_points"]),
        }

    def _render(self, raw):
        """用 jinja2 渲染单条模板字符串"""
        return Template(raw).render(**self._vars())

    # ---------- 1) 高频问题前置命中（最高优先级，不走 LLM） ----------
    def _match_hot_question(self, text):
        """关键词命中就返回渲染好的话术，否则 None"""
        for hot_q in self.templates.get("hot_questions", []):
            if any(kw in text for kw in hot_q["keywords"]):
                return self._render(random.choice(hot_q["templates"]))
        return None

    # ---------- 2) 4 分类意图识别（调 LLM） ----------
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

    # ---------- 3) 闲扯子类型判断 ----------
    def _sub_chat_type(self, text):
        """reject=礼貌拒绝；background=打探背景；其他归 casual"""
        if any(w in text for w in ("考虑", "商量", "对比", "再看看", "想想")):
            return "reject"
        if any(w in text for w in ("几年", "多少年", "店在", "老板", "谁", "哪里")):
            return "background"
        return "casual"

    # ---------- 4) 对话结束判断（v1.1 红线：只日志 + 清上下文） ----------
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
        """主入口：hot 命中 → 意图分类 → 模板选 + 渲染 → 上下文 + 计数器更新"""
        # 1) 高频问题前置（最高优先级）
        hot_answer = self._match_hot_question(text)
        if hot_answer is not None:
            tag = "hot_question"
            answer = hot_answer
        else:
            # 2) LLM 意图分类
            intent = self.detect_intent(text)

            if intent == "chat":
                self.chat_streak += 1
                sub = "soft_end" if self.chat_streak >= 2 else self._sub_chat_type(text)
                pool = self.templates["chat"][sub]
                tag = f"chat/{sub}"
            else:
                self.chat_streak = 0
                pool = self.templates[intent]
                tag = intent

            answer = self._render(random.choice(pool))

        # 3) 上下文记忆（最多 3 轮）
        self.history.append({"user": text, "bot": answer})
        self.history = self.history[-3:]

        # 4) 对话结束判断（仅日志 + 清上下文，红线：不发任何消息）
        if self.is_chat_ended(text):
            self.end_streak = 0
            self.history = []
            self.chat_streak = 0
            print("[日志] 对话已结束，上下文已清空")

        return tag, answer

    # ---------- 交互命令支持（status / clear） ----------
    def status(self):
        """返回当前对话状态字符串，供 test_cli.py 打印"""
        return (
            f"[status] 历史 {len(self.history)} 轮 | "
            f"连续闲扯 {self.chat_streak} | "
            f"连续结束语 {self.end_streak}"
        )

    def clear(self):
        """手动清空上下文与计数器"""
        self.history = []
        self.chat_streak = 0
        self.end_streak = 0
        return "[clear] 上下文已清空"
