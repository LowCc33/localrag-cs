"""
Agent 模块
提供基于 DeepSeek-V4-Flash API 的智能代理能力：
- 理解用户意图
- 自主决策调用哪个工具
- 工具调用循环，最多3轮
- DeepSeek API 不可用时自动降级到原有 RAG 流程
"""
