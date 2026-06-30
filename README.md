<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-blue" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-0.115+-green" alt="FastAPI">
  <img src="https://img.shields.io/badge/Elasticsearch-8.x-orange" alt="Elasticsearch">
  <img src="https://img.shields.io/badge/llama.cpp-latest-purple" alt="llama.cpp">
  <img src="https://img.shields.io/badge/license-MIT-yellow" alt="License">
</p>

# LocalRAG-CS · 企业级本地RAG智能客服

> **纯本地部署 · 零API依赖 · GTX1070 8G可运行**

LocalRAG-CS 是一个面向企业客服场景的纯本地智能问答系统。包含**自研Agent规划层**（DeepSeek驱动）、**七级熔断降级**、**三级混合检索**、**Redis缓存加速**、**SSE流式输出**等完整功能。后端+前端约3000行代码，**零重型框架依赖**（无LangChain/LlamaIndex）。

---

## 📸 界面预览

| 问答界面 | Agent模式 | Chunk管理 |
|---------|----------|----------|
| DeepSeek风格深色对话界面，支持会话历史侧边栏 | Agent思考过程可视化，分步展示检索→生成链路 | 知识库Chunk搜索、编辑、删除管理 |
| *(截图待补充)* | *(截图待补充)* | *(截图待补充)* |

---

## ✨ 功能特性

### 🤖 Agent智能规划（核心亮点）
- **自研Agent层**：基于DeepSeek-V4-Flash实现意图识别 + retrieve/generate双工具编排
- **3轮防死循环**：达到轮数上限自动兜底RAG，API故障自动降级到本地检索
- **可视化思考过程**：前端分步展示Agent的思考链路（蓝色🔍检索、紫色🤖生成、黄色⚠️降级）
- **降级透明**：Agent不可用时用户无感知切换到标准RAG流程

### 🔍 三级混合检索
- **BM25关键词召回** → **向量语义召回** → **RRF融合排序** → **规则重排** → **Reranker精排**
- **Elasticsearch 8.x**：全文检索 + 向量检索双引擎
- **多模型协作**：Qwen2.5-7B-Q3_K_M（本地生成）+ Qwen3-Embedding（向量）+ Qwen3-Reranker（精排）+ DeepSeek-V4-Flash（Agent规划推理）

### 🛡️ 七级熔断降级
- **Agent故障** → **Reranker故障** → **向量故障** → **Embedding故障** → **生成模型故障** → **纯BM25** → **兜底回答**
- 每一级降级对用户完全透明，仅返回时附带降级标记
- 修复了降级分支乱序、BM25分数与排序不一致等核心bug

### ⚡ 交互体验
- **SSE流式输出**：`/api/ask/stream` 接口，按Token实时推送，前端逐字显示
- **Redis热点缓存**：高频问答缓存，命中率30%+，命中响应1ms，Redis异常自动静默降级
- **DeepSeek深色风格UI**：专业工作台视觉，全屏对话
- **会话历史管理**：左侧侧边栏，自动创建/切换/刷新
- **Chunk管理页面**：知识库Chunk搜索、编辑、删除
- **问答报告导出**：PDF/Word格式，一键导出对话记录
- **知识库导入管理**：多格式文件导入，导入队列可视化

### 🔧 工程化
- **FastAPI分层架构**：依赖注入管理全局客户端，便于调试与扩展
- **WSL2 CUDA优化**：解决显存碎片化问题
- **一键启停脚本**：`start_all.sh` / `stop_all.sh` / `test_services.sh`

---

## 🏗️ 系统架构

```
┌──────────────────────────────────────────────────────────┐
│                    前端层 (HTML/CSS/JS)                    │
│  问答页面(index.html)  │  Agent模式(agent.html)           │
│  Chunk管理(chunks.html)  │  导入管理(ingest.html)         │
└──────────────────────┬───────────────────────────────────┘
                       │ HTTP/SSE
┌──────────────────────▼───────────────────────────────────┐
│                  网关层 (FastAPI)                          │
│  /api/ask  │  /api/ask/stream  │  /api/agent/ask          │
│  /api/chunks  │  /api/session  │  /api/export             │
└──────┬──────────────┬──────────────┬─────────────────────┘
       │              │              │
┌──────▼──────┐ ┌─────▼──────┐ ┌───▼───────────────┐
│  检索层      │ │  Agent层   │ │  缓存层            │
│  BM25+向量   │ │  意图识别   │ │  Redis热点缓存     │
│  RRF+Reranker│ │  工具编排   │ │  TTL 24h + LRU    │
└──────┬───────┘ └─────┬──────┘ └───────────────────┘
       │               │
┌──────▼───────────────▼───────────────────────────────┐
│                  推理层 (llama.cpp)                    │
│  Qwen2.5-7B-Q3_K_M (生成)  │  Qwen3-Embedding (向量) │
│  Qwen3-Reranker (精排)                                │
└──────────────────────┬───────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────┐
│                  降级边界                              │
│  七级熔断降级：Agent→Reranker→向量→Embedding→生成→BM25→兜底│
└──────────────────────────────────────────────────────┘
```

---

## 📊 性能指标

| 指标 | 数据 |
|------|------|
| **单轮问答耗时** | 4-5秒（端到端），缓存命中1ms |
| **总显存占用** | ≈5GB（GTX1070 8G） |
| **模型配置** | Qwen2.5-7B-Instruct-Q3_K_M（生成，~4GB） |
| | Qwen3-Embedding-0.6B-Q8_0（向量，~0.7GB） |
| | Qwen3-Reranker-0.6B-Q8_0（精排，~0.6GB） |
| **代码规模** | 后端≈2000行 + 前端≈1000行 |

---

## 🚀 快速开始

### 环境要求

- **硬件**：GTX 1060 6G 及以上显卡（推荐8G）
- **系统**：Linux / WSL2
- **依赖**：Python 3.10+、Elasticsearch 8.x、Redis（可选）

### 1. 克隆项目

```bash
git clone https://github.com/LowCc33/localrag-cs.git
cd localrag-cs
```

### 2. 安装依赖

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. 下载模型

```bash
# 生成模型
wget -P ~/models/Qwen2.5-7B-Instruct-GGUF/ https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/Qwen2.5-7B-Instruct-Q3_K_M.gguf

# Embedding模型
wget -P ~/models/Qwen3-Embedding-0.6B-GGUF/ https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/main/Qwen3-Embedding-0.6B-Q8_0.gguf

# Reranker模型
wget -P ~/models/Qwen3-Reranker-0.6B-GGUF/ https://huggingface.co/Qwen/Qwen3-Reranker-0.6B-GGUF/resolve/main/Qwen3-Reranker-0.6B-q8_0.gguf
```

### 4. 配置Elasticsearch

修改 `config.py` 中的 `ES_HOST` 为你的ES地址。

### 5. 启动服务

```bash
# 一键启动所有服务（模型 + API）
bash scripts/start_all.sh

# 检查服务状态
bash scripts/test_services.sh

# 访问
# http://localhost:8000  — 问答页面
# http://localhost:8000/docs  — API文档
```

### 6. 停止服务

```bash
bash scripts/stop_all.sh
```

---

## 📁 项目结构

```
localrag-cs/
├── api/                    # API路由层
│   ├── app.py              # FastAPI应用入口
│   ├── agent_routes.py     # Agent模式路由
│   └── ingest_router.py    # 导入路由
├── agent/                  # Agent规划层（核心亮点）
│   ├── agent.py            # Agent主逻辑：工具调用循环
│   ├── llm_client.py       # DeepSeek-V4-Flash客户端
│   └── tools.py            # retrieve/generate工具定义
├── core/                   # 核心业务层
│   ├── cache.py            # Redis缓存模块
│   ├── embedding.py        # 向量化
│   ├── es_client.py        # ES客户端
│   ├── es_hybrid.py        # 混合检索（BM25+向量+RRF）
│   ├── es_vector.py        # 向量检索
│   ├── llm_client.py       # LLM客户端（含流式生成）
│   ├── reranker.py         # 重排
│   └── retriever.py        # 检索器
├── ingestion/              # 数据导入
│   ├── pipeline.py         # 导入管道
│   ├── chunker.py          # 文本分块
│   ├── parsers.py          # 文件解析
│   └── task_manager.py     # 任务管理
├── routes/                 # 功能路由
│   ├── ask.py              # 问答接口（含SSE流式）
│   ├── cache.py            # 缓存统计
│   ├── chunks.py           # Chunk管理
│   ├── export.py           # 导出
│   ├── health.py           # 健康检查
│   ├── public.py           # 公网暴露
│   └── session.py          # 会话管理
├── templates/              # 前端页面
│   ├── index.html          # 问答页面（SSE流式）
│   ├── agent.html          # Agent模式页面（思考过程可视化）
│   ├── ingest.html         # 导入页面
│   └── chunks.html         # Chunk管理页面
├── scripts/                # 运维脚本
│   ├── start_all.sh        # 一键启动
│   ├── stop_all.sh         # 一键停止
│   └── test_services.sh    # 服务检查
├── config.py               # 全局配置
├── dependencies.py         # 依赖注入
└── schemas.py              # 数据模型
```

---

## 🔌 API接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/ask` | 非流式问答 |
| POST | `/api/ask/stream` | SSE流式问答（逐Token推送） |
| POST | `/api/agent/ask` | Agent模式问答（DeepSeek规划+工具调用） |
| GET | `/api/health` | 服务健康检查 |
| GET | `/api/cache/stats` | Redis缓存统计 |
| GET/PUT/DELETE | `/api/chunks` | Chunk管理CRUD |
| GET | `/api/session` | 会话管理 |
| POST | `/api/export/pdf` | 导出PDF报告 |
| POST | `/api/export/docx` | 导出Word报告 |
| POST | `/api/ingest` | 导入文档 |

---

## 🧪 七级熔断降级机制

```
正常链路(Agent模式):
  Agent规划 → retrieve工具 → generate工具 → 回答
     ↓ Agent API超时/工具调用失败（自动降级，用户无感）
降级链路1(标准RAG):
  BM25 → 向量 → RRF → 重排 → Embedding → 生成 → 回答
     ↓ Reranker故障
降级链路2:
  BM25 → 向量 → RRF → 重排 → Embedding → 生成 → 回答（跳过Reranker）
     ↓ 向量/Embedding故障
降级链路3:
  BM25 → 生成 → 回答（纯关键词检索）
     ↓ 生成模型故障
降级链路4:
  纯BM25检索 → 返回原文片段
     ↓ ES故障
降级链路5:
  兜底回答（"抱歉，系统暂时无法回答"）
```

**降级触发条件**：
- **Agent降级**：DeepSeek API超时（30s）/ 报错 / 工具调用失败
- **Reranker降级**：reranker服务不可用，自动跳过精排
- **向量降级**：ES向量检索异常，回退纯BM25
- **生成降级**：LLM服务不可用，返回原文片段

每一级降级对用户完全透明，仅返回时附带降级标记。

---

## ❓ 常见问题

### Q1: 显存不够怎么办？
8G显卡运行三个模型（生成~4GB + Embedding~0.7GB + Reranker~0.6GB）理论够用，但WSL2存在**显存碎片化bug**——总显存够但没有连续块就会OOM。解决方案：

1. **启动模型加 `--fit off` 参数**（必加，绕过碎片化分配策略）
2. **把Reranker或Embedding模型放CPU**：减少约600M显存压力
3. **扩大WSL2内存**：在 `%UserProfile%\.wslconfig` 中设置 `memory=10GB`

### Q2: Elasticsearch连不上？
检查 `config.py` 中的 `ES_HOST` 配置，确保ES服务已启动。ES 8.x默认开启安全认证，需配置用户名密码或关闭安全认证。

### Q3: 导入文档后检索不到？
检查Chunk管理页面确认文档已成功分块和向量化。如果向量化失败，检索会回退到纯BM25模式。

### Q4: 如何切换模型？
修改 `config.py` 中的模型路径和端口配置，重启服务即可。支持任意GGUF格式模型。

---

## 📹 演示视频

[![B站演示视频](https://img.shields.io/badge/B站-演示视频-red)](https://www.bilibili.com/video/BV1fCLm6GEZz)

*(视频待更新，展示Agent模式 + 流式输出 + Chunk管理等新功能)*

---

## 🧑‍💻 关于作者

**周博生** · 转行AI应用开发工程师

从跨境电商到全屋定制，再到AI开发，跨界自学成才。坚持全职工作下每天3-4小时学习，完成2个完整AI项目（LocalRAG-CS企业级本地RAG系统 + 铁三角多Agent协同开发框架）。

- GitHub: [LowCc33](https://github.com/LowCc33)
- 项目演示: [B站](https://www.bilibili.com/video/BV1fCLm6GEZz)

---

## 📄 License

MIT
