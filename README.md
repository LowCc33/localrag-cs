# LocalRAG-CS 智能客服知识库问答系统

基于本地大模型的私有部署智能客服系统，支持 **BM25检索 + 向量检索 + RRF融合 + Reranker重排 + LLM生成答案** 完整RAG链路。

---

## ✨ 核心特性

| 特性 | 说明 |
|------|------|
| 🔀 **混合检索** | BM25全文检索 + 向量语义检索 + RRF融合排序 |
| 📊 **智能重排** | Qwen3-Reranker 模型精排，提升相关性 |
| 🤖 **本地大模型** | Qwen2.5-7B 纯CPU/GPU推理，无数据外泄 |
| 🛡️ **优雅降级** | 向量/Reranker服务不可用时自动降级到纯BM25 |
| 📏 **长度稳定** | 所有分支统一返回前3条文档，LLM输入长度可控 |
| ✅ **排序正确** | 修复降级分支排序bug，ES原生分数与显示顺序严格一致 |

---

## 🚀 3步启动

```bash
# 1. 进入项目目录
cd localrag-cs

# 2. 一键启动所有服务
bash scripts/start_all.sh

# 3. 打开浏览器访问
http://localhost:8000
```

---

## 📋 端口说明

| 服务 | 端口 | 模型 | 说明 |
|------|------|------|------|
| 🌐 **API 服务** | 8000 | - | 问答接口、管理后台、健康检查 |
| 🤖 **LLM 生成** | 8080 | Qwen2.5-7B-Instruct | 大模型推理服务 |
| 🔢 **Embedding** | 8081 | Qwen3-Embedding-0.6B | 向量编码服务 |
| 📊 **Reranker** | 8082 | Qwen3-Reranker-0.6B | 重排排序服务 |
| 💾 **Elasticsearch** | 9200 | - | 知识库向量数据库 |

---

## 🏗️ 系统架构

```
用户提问
   ↓
┌───────────────────────────────────────────────────────────┐
│                     RAG 完整链路                            │
├───────────────────────────────────────────────────────────┤
│  1. 查询重写  →  同义词扩展 + 意图识别                     │
│  2. 混合检索  →  BM25全文检索 + 向量检索 + RRF融合        │
│  3. 智能重排  →  Reranker 模型精排 Top 3                   │
│  4. 答案生成  →  LLM 基于检索结果生成回答                  │
└───────────────────────────────────────────────────────────┘
   ↓
最终答案 + 引用来源
```

### 🛡️ 降级机制

```
正常路径: 查询重写 → 向量编码 → 混合检索(RRF) → Reranker重排 → LLM生成
            ↓ 向量编码失败
降级路径: 查询重写 → 纯BM25检索 (ES原生排序) → LLM生成
            ↓ Reranker不可用
降级路径: 查询重写 → 混合检索 → 按检索分排序取Top3 → LLM生成
```

**所有分支最终统一返回前3条文档，确保LLM输入长度稳定可控。**

---

## 📁 目录结构

```
localrag-cs/
├── api/
│   └── app.py             # ✅ FastAPI 主入口
├── config.py              # ✅ 统一配置文件（所有参数都在这里）
├── dependencies.py        # 依赖注入管理
├── schemas.py             # API数据模型
│
├── routes/                # API路由
│   ├── ask.py             # 问答接口 (POST /api/ask)
│   └── health.py          # 健康检查接口
│
├── core/                  # ✅ 核心业务模块
│   ├── es_client.py       # ES客户端
│   ├── es_hybrid.py       # BM25检索 + RRF融合排序
│   ├── es_vector.py       # 向量检索
│   ├── embedding.py       # 向量编码客户端
│   ├── reranker.py        # 重排客户端
│   ├── retriever.py       # 检索器（查询重写 + 混合检索 + 降级处理）
│   └── llm_client.py      # LLM生成客户端
│
├── scripts/               # ✅ 运维脚本（推荐使用）
│   ├── start_all.sh       # 🚀 一键启动所有服务
│   ├── stop_all.sh        # ⏹️ 一键停止所有服务
│   ├── stop_llm.sh        # 单独停止LLM服务
│   ├── stop_embedding.sh  # 单独停止Embedding服务
│   ├── stop_reranker.sh   # 单独停止Reranker服务
│   └── test_services.sh   # 🔍 一键检查所有服务状态
│
├── logs/                  # 日志目录
│
├── docs/                  # 文档目录
│   ├── 部署指南.md
│   ├── 常见问题排查.md
│   └── 参数优化指南.md
│
├── templates/             # 前端模板
│   └── index.html         # 管理后台页面
│
├── requirements.txt       # Python依赖
└── .gitignore             # Git忽略配置
```

---

## 🔧 配置说明

所有配置统一在 `config.py` 中修改：

```python
# ========== 服务地址配置 ==========
ES_HOST = "http://localhost:9200"
ES_USER = "elastic"
ES_PASSWORD = "your-password"

# 模型服务地址
LLM_API_URL = "http://localhost:8080"
EMBEDDING_API_URL = "http://localhost:8081"
RERANKER_API_URL = "http://localhost:8082"

# ========== 检索参数配置 ==========
RETRIEVE_TOP_K = 10        # 混合检索召回数量
RERANKER_TOP_K = 3         # 重排后返回给LLM的数量（固定为3）
RRF_K = 60                  # RRF融合常数

# ========== LLM 配置 ==========
LLM_TEMPERATURE = 0.1      # 越低越严谨
LLM_MAX_TOKENS = 1024      # 最大生成长度
```

### llama.cpp 启动最优参数（GTX 1070 验证）

```bash
./llama-server -m qwen2.5-7b-instruct-q3_k_m.gguf \
  -c 4096 \
  -b 256 \                # 最优batch size，超过反而变慢
  --cublas \              # CUDA加速，提升约10%速度
  --fit off               # ✅ 核心修复：解决WSL2 CUDA显存碎片化OOM
```

---

## ❓ 常见问题速查

### Q1: 服务启动卡/推理速度慢怎么办？

**原因：** CPU跑满/显存不足

**解决：**
```bash
# 1. 先停所有服务
bash scripts/stop_all.sh

# 2. 检查显存占用
nvidia-smi

# 3. 如果GPU显存不够，用纯CPU模式运行
# 修改 start_all.sh 中 --n-gpu-layers 为 0
```

---

### Q2: OOM爆显存怎么办？

**原因：** 7B模型需要 ~8GB 显存，三个模型加起来 ~12GB

**解决：**
1. 只启动必要的模型（可以单独停掉不需要的服务）
2. 用更小的模型（Qwen-1.8B 只需要 2GB）
3. 增加 swap 分区作为应急

---

### Q3: 接口返回503怎么办？

**原因：** 某个依赖服务没启动成功

**解决：**
```bash
# 1. 先检查所有服务状态
bash scripts/test_services.sh

# 2. 看哪个服务挂了，检查对应日志
# API日志:    tail -f logs/api.log
# 单个服务日志查看参考 start_all.sh 中的配置
```

---

### Q4: 回答不准怎么办？

**排查顺序：**
1. ✅ 知识库有没有相关内容？先搜索确认
2. ✅ 检索到的文档和问题相关吗？看接口返回的 sources
3. ✅ 重排后的 Top3 是不是最相关的？
4. ✅ LLM 有没有正确引用文档内容？

**优化方法：**
- 增加知识库覆盖范围
- 把长文档拆分成更小的问答对（Q&A格式）
- 调整 `RETRIEVE_TOP_K` 参数（默认10，不建议太大）

---

### Q5: WSL2 下第三个模型必OOM怎么办？

**已解决 ✅**

**问题本质：** WSL2 的CUDA驱动会把显存分割成很多小块，即使总容量够，没有连续大块也无法加载模型

**终极解决：** 启动时加 `--fit off` 参数

```bash
# 在 start_all.sh 中所有 llama-server 启动命令都加上 --fit off
./llama-server -m 模型文件 --port 8080 --fit off ...
```

**效果：** 三个7B级别模型可以同时在 GTX 1070 8GB 上稳定运行，总显存占用≈5GB

---

## 📞 常用命令

```bash
# ========== 一键操作（推荐） ==========
# 启动所有服务
bash scripts/start_all.sh

# 停止所有服务
bash scripts/stop_all.sh

# 检查所有服务状态
bash scripts/test_services.sh

# ========== 单独控制 ==========
# 单独停止LLM服务
bash scripts/stop_llm.sh

# 单独停止Embedding服务
bash scripts/stop_embedding.sh

# 单独停止Reranker服务
bash scripts/stop_reranker.sh

# ========== 日志查看 ==========
# 查看API日志
tail -f logs/api.log

# 查看 llama.cpp 服务日志
lsof -i :8080  # 找到进程后看日志位置
```

---

## 🧪 接口测试

启动服务后，可以用 `curl` 直接测试：

```bash
# 测试健康检查
curl http://localhost:8000/api/health

# 测试问答接口
curl -X POST http://localhost:8000/api/ask \
  -H "Content-Type: application/json" \
  -d '{
    "question": "雇主责任险和工伤保险有什么区别？",
    "top_k": 10,
    "rerank_top_k": 3
  }'
```

---

## 💡 踩坑总结

### 1. WSL2 CUDA 显存碎片化问题

**现象：** 明明总显存还剩3-4GB，但启动第三个模型就OOM

**根本原因：** WSL2的CUDA驱动会把显存分割成很多小块，即使总容量够，没有连续大块也无法加载模型

**解决方案：** 启动时加 `--fit off` 参数，让 llama.cpp 不做连续内存检查，三个模型总显存可以控制在≈5GB

---

### 2. BM25 分数与排序不一致问题

**现象：** 前端显示的BM25分数从上到下不是降序排列，看起来排序混乱

**根本原因：** 之前有个画蛇添足的"规则重排"逻辑，对ES原生BM25分进行了二次加权，但显示时用的还是原始分

**现在的处理：** 完全删除了规则重排，排序和显示都使用 **ES原生`_score`**，顺序严格一致

---

## 📝 修改记录

### v1.3 (2026-05-20)
- ✅ 修复目录结构，app.py 移到 api/ 目录，符合标准Python包结构
- ✅ 新增 .gitignore，过滤所有不该提交的文件

### v1.2 (2026-05-20)
- ✅ 修复降级分支排序bug，纯BM25分支按ES原生`_score`降序排列
- ✅ 删除画蛇添足的"规则重排"逻辑，保持检索质量纯净
- ✅ 所有分支统一返回前3条文档，LLM输入长度稳定可控
- ✅ 增加单独停止各服务的脚本，方便调试和降级测试
- ✅ 完善降级机制，单个服务故障不影响整体可用性
- ✅ 解决WSL2 CUDA显存碎片化OOM问题（--fit off）

### v1.1 (2026-05-16)
- ✅ 修复 RRF 融合分数保留问题
- ✅ 增加 has_vector 状态标记
- ✅ 前端分数显示优化

### v1.0 (2026-05-15)
- 🎉 标准目录结构整理，支持一键部署
- ✅ 完整的RAG链路实现
- ✅ 支持多级优雅降级

---

## ⚡ Redis 热点查询缓存（新增）

### 功能简介
对高频查询做完整答案缓存，命中时**毫秒级返回**（实测 < 1ms），跳过检索 + 重排 + LLM 三阶段。
Redis 不可用时自动降级到无缓存模式，**不影响主链路**。

### 用户态部署（不依赖系统服务，无需 sudo）
Redis 编译安装在 `~/redis/` 下，由项目内脚本启停。

```bash
# 启动 Redis（幂等，已启动会跳过）
bash scripts/start_redis.sh

# 验证（应返回 PONG）
~/redis/bin/redis-cli ping

# 优雅停止
bash scripts/stop_redis.sh
```

> 首次部署需要手动编译一次：
> ```bash
> mkdir -p ~/build && cd ~/build
> wget https://download.redis.io/releases/redis-7.2.4.tar.gz
> tar xzf redis-7.2.4.tar.gz && cd redis-7.2.4 && make -j$(nproc) MALLOC=libc
> mkdir -p ~/redis/bin && cp src/redis-server src/redis-cli ~/redis/bin/
> # 配置 ~/redis/etc/redis.conf 参考仓库内 redis.conf 示例
> ```

### 安装 Python 依赖

```bash
cd ~/localrag-cs1 && source venv/bin/activate
pip install redis
```

### 关键设计
| 维度 | 设计 |
|------|------|
| 缓存粒度 | 完整答案（query → answer + sources） |
| Key 归一化 | 去中英文标点 + 去所有空白 + 转小写 + md5 |
| TTL | 24 小时（`config.CACHE_TTL_SECONDS`） |
| 内存上限 | 256MB + `allkeys-lru` 淘汰（redis.conf） |
| 降级保护 | Redis 异常静默降级，返回 None/False，不阻塞业务 |
| 命名空间 | 所有 key 走 `localrag:cache:` 前缀，flush 不会误伤其他业务 |

### 接口

| Method | Path | 说明 |
|--------|------|------|
| GET | `/api/cache/stats` | 返回 hit/miss/命中率/已缓存条数等运行时统计 |
| POST | `/api/cache/flush` | 清空本项目命名空间缓存 + 重置 HIT/MISS 计数 |

`/api/ask` 与 `/api/ask/stream` 响应里新增字段：
- `cache_status`：`HIT` / `MISS`
- `response_time_ms`：端到端响应耗时
- `cached_at`：缓存写入时间（仅 HIT 时返回）

### 演示界面
- 答案卡右上角：**HIT 绿色徽章** / **MISS 灰色徽章**，带响应时间
- 页面顶部：实时统计条（总查询 / HIT / MISS / 命中率 / HIT 平均耗时 / MISS 平均耗时）
- 每 15 秒自动刷新；带"清空缓存"按钮便于演示从零开始

### 配置项（`config.py`）
| 常量 | 默认 | 说明 |
|------|------|------|
| `CACHE_ENABLED` | `True` | 缓存总开关（环境变量同名可覆盖） |
| `CACHE_REDIS_HOST` | `127.0.0.1` | Redis 地址 |
| `CACHE_REDIS_PORT` | `6379` | Redis 端口 |
| `CACHE_REDIS_TIMEOUT` | `0.1`(s) | 操作超时，超时立即降级 |
| `CACHE_TTL_SECONDS` | `86400` | 缓存 TTL，24 小时 |
| `CACHE_KEY_PREFIX` | `localrag:cache:` | Key 命名空间前缀 |
