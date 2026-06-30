"""
LocalRAG-CS 配置模块
所有硬编码参数抽离到这里
支持开发/生产环境切换
"""
import os
import warnings
import urllib3

# ========== 警告抑制（开发环境） ==========
# 本地开发用自签名证书时，跳过证书验证产生的警告太多刷屏，禁用掉
# 生产环境如果用了正式证书，可以删除这里的代码
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", category=Warning, module="elasticsearch")
warnings.filterwarnings("ignore", message=".*verify_certs=False.*")

# ========== 环境配置 ==========
ENV = os.getenv('RAG_ENV', 'development')  # development / production
DEBUG = ENV == 'development'

# ========== ES连接配置 ==========
# -----------------------------------------------------------------------------
# WSL2 访问 Windows 本机 Elasticsearch 的说明：
#
# 1. WSL2 的 localhost 与 Windows 的 localhost 是隔离的，不能直接使用
# 2. 需要使用 Windows 在 WSL2 网络中的网关 IP，查看命令: ip route | grep default
# 3. 示例：如果网关是 172.28.208.1，则 ES_HOST = https://172.28.208.1:9200
# 4. 需要确保 Windows 防火墙允许 WSL2 子网访问 9200 端口
# 5. 备选：使用 Windows 的局域网 IP (如 192.168.x.x)
#
# 查看当前 WSL2 网关: ip route | grep default
# -----------------------------------------------------------------------------
ES_HOST = os.getenv('ES_HOST', 'https://192.168.1.3:9200')
ES_USER = os.getenv('ES_USER', 'elastic')
ES_PASSWORD = os.getenv('ES_PASSWORD', 'Xw5sMLBqQuJfowJe8T*q')  # 请修改为实际密码
ES_TIMEOUT = int(os.getenv('ES_TIMEOUT', 30))
ES_VERIFY_CERTS = False  # ES8默认开启SSL，本地开发关闭

# ========== 索引配置 ==========
ES_INDEX_NAME = os.getenv('ES_INDEX_NAME', 'cs_knowledge_base')
ES_BATCH_SIZE = 100

# ========== 向量模型配置 ==========
# -----------------------------------------------------------------------------
# llama.cpp + Qwen3-Embedding 配置
# 通过 HTTP API 调用本地 llama.cpp 服务进行编码
# 模型: Qwen3-Embedding-0.6B-GGUF (llama.cpp 服务端加载)
# 维度: 1024
# 服务启动命令:
#   /home/zbs/llama.cpp/build/bin/llama-server \
#       -m /home/zbs/models/Qwen3-Embedding-0.6B-GGUF/Qwen3-Embedding-0.6B-Q8_0.gguf \
#       --port 8081 \
#       --embeddings
# -----------------------------------------------------------------------------
EMBEDDING_API_URL = os.getenv('EMBEDDING_API_URL', 'http://localhost:8081/embeddings')
# 向量维度统一管理（避免硬编码，所有模块都引用 EMBEDDING_DIM）
EMBEDDING_DIM = int(os.getenv('EMBEDDING_DIM', 1024))  # Qwen3-Embedding-0.6B 实际输出维度
# 兼容别名（保留向后兼容，新代码请使用 EMBEDDING_DIM）
EMBEDDING_DIMENSION = EMBEDDING_DIM
VECTOR_SIMILARITY = 'cosine'  # 相似度计算方式

# 当前使用的向量维度（统一入口）
CURRENT_VECTOR_DIM = EMBEDDING_DIM

# ========== 检索参数配置 ==========
RETRIEVE_TOP_K = 50  # RRF融合前取的候选数量
FINAL_TOP_K = 10   # 最终返回结果数量
RRF_K = 60           # RRF融合常数k

# ========== 默认索引Mapping ==========
# 向量索引Mapping（同时支持BM25和向量检索）
VECTOR_INDEX_MAPPING = {
    "mappings": {
        "properties": {
            "doc_id": {"type": "keyword"},
            "question": {"type": "text", "analyzer": "standard"},
            "answer": {"type": "text", "analyzer": "standard"},
            "embedding": {
                "type": "dense_vector",
                "dims": EMBEDDING_DIM,
                "index": True,
                "similarity": VECTOR_SIMILARITY
            },
            "category": {"type": "keyword"},
            "create_time": {"type": "date"}
        }
    }
}

# 兼容旧版（阶段1使用的mapping）
DEFAULT_MAPPING = VECTOR_INDEX_MAPPING
HYBRID_INDEX_MAPPING = VECTOR_INDEX_MAPPING  # 别名，兼容es_vector.py引用

# ========== 同义词词典 ==========
DEFAULT_SYNONYMS = {
    '医保': '医疗保险',
    '社保': '社会保险',
    '报销': '理赔',
    '看病': '就医',
    '大病': '重大疾病',
    '住院': '入院',
    '门诊': '门急诊',
    '缴费': '交费',
    '钱': '费用',
    '怎么': '如何'
}

# ========== 类目权重配置 ==========
# 检索时对特定类目进行加权
DEFAULT_CATEGORY_BOOST = {
    '保险': 1.5,
    '医疗': 1.3,
    '政策': 1.2
}

# ========== Reranker 配置 ==========
# Qwen3-Reranker-0.6B 服务配置
# llama.cpp 服务启动命令:
#   /home/zbs/llama.cpp/build/bin/llama-server \
#       -m /home/zbs/models/Qwen3-Reranker-0.6B-GGUF/model.gguf \
#       --port 8082 \
#       --reranking
# -----------------------------------------------------------------------------
RERANKER_API_URL = os.getenv('RERANKER_API_URL', 'http://localhost:8082/v1/rerank')
RERANKER_TOP_K = 3  # 重排后返回的最终文档数

# ========== LLM 配置 ==========
# Qwen2.5-7B 生成模型配置
# llama.cpp 服务启动命令:
#   /home/zbs/llama.cpp/build/bin/llama-server \
#       -m /home/zbs/models/Qwen2.5-7B-Instruct-GGUF/model.gguf \
#       --port 8080
# -----------------------------------------------------------------------------
LLM_API_URL = os.getenv('LLM_API_URL', 'http://localhost:8080/v1/chat/completions')
LLM_TEMPERATURE = 0.1  # 低温度，确保回答严谨
LLM_MAX_TOKENS = 1024

# ========== LLM 流式输出配置 ==========
# 流式输出相关参数，全部在这里配置，禁止在代码中硬编码
# 流式开关：True 开启 SSE 流式，False 走原非流式逻辑（兼容老接口）
LLM_STREAM_ENABLED = os.getenv('LLM_STREAM_ENABLED', 'true').lower() == 'true'
# 流式请求总超时（单位：秒）。流式生成时间较长，单独配置一个更大的值
LLM_STREAM_TIMEOUT = int(os.getenv('LLM_STREAM_TIMEOUT', 120))
# 流式响应中读取单个数据块的最大等待时间（单位：秒），用于检测流中断
LLM_STREAM_READ_TIMEOUT = int(os.getenv('LLM_STREAM_READ_TIMEOUT', 30))
# SSE 事件名定义（前后端约定，禁止散落在代码里）
SSE_EVENT_TOKEN = 'token'        # 单个 token 输出事件
SSE_EVENT_SOURCES = 'sources'    # 引用来源/分块信息事件
SSE_EVENT_LATENCY = 'latency'    # 阶段耗时统计事件
SSE_EVENT_DONE = 'done'          # 流结束事件
SSE_EVENT_ERROR = 'error'        # 流异常事件
# SSE 心跳消息间隔（单位：秒），防止代理/网关把长连接当成超时断开
SSE_HEARTBEAT_INTERVAL = int(os.getenv('SSE_HEARTBEAT_INTERVAL', 15))

# ========== 公网暴露模块配置（任务：ngrok-public-module） ==========
# -----------------------------------------------------------------------------
# ngrok 公网地址，用于从外网访问 LocalRAG-CS
# 支持通过环境变量覆盖，方便不同环境切换
# ngrok 启动命令: ngrok http 8080 --domain skedaddle-morphine-shamrock.ngrok-free.dev
# -----------------------------------------------------------------------------
PUBLIC_URL = os.getenv('PUBLIC_URL', 'https://skedaddle-morphine-shamrock.ngrok-free.dev')
# 公网服务端口（start_public.sh 启动时 uvicorn 监听此端口，与 ngrok 转发端口一致）
PUBLIC_PORT = int(os.getenv('PUBLIC_PORT', 8080))
# ngrok 可执行文件路径
NGROK_PATH = os.getenv('NGROK_PATH', '/usr/local/bin/ngrok')
# ngrok 管理面板地址
NGROK_ADMIN_URL = os.getenv('NGROK_ADMIN_URL', 'http://127.0.0.1:4040')

# LLM 系统提示词
LLM_SYSTEM_PROMPT = """你是一个专业的问答助手。请严格基于提供的上下文信息回答用户的问题。
要求：
1. 只使用上下文中的信息，不要编造内容
2. 如果上下文中没有答案，直接回答"抱歉，我无法回答这个问题"
3. 回答要简洁、准确，使用中文
4. 不要提及"根据上下文"之类的话，直接给出答案
"""

# ========== Redis 热点缓存配置（任务：localrag-redis-cache） ==========
# -----------------------------------------------------------------------------
# 用户态 Redis 部署位置：~/redis/，由 scripts/start_redis.sh 启动
# 缓存策略：完整答案缓存（query -> answer），命中直接返回，绕过检索+生成
# 失效策略：TTL 24h + maxmemory-policy=allkeys-lru（redis.conf 中配置）
# 降级保护：Redis 连接异常时自动跳过缓存，走原流程，不阻塞服务
# -----------------------------------------------------------------------------
# 缓存开关：True 启用 Redis 缓存，False 关闭（演示对比/排查问题时可关）
CACHE_ENABLED = os.getenv('CACHE_ENABLED', 'true').lower() == 'true'
# Redis 连接地址（用户态部署在本机，固定 127.0.0.1:6379）
CACHE_REDIS_HOST = os.getenv('CACHE_REDIS_HOST', '127.0.0.1')
CACHE_REDIS_PORT = int(os.getenv('CACHE_REDIS_PORT', 6379))
# Redis 数据库编号（0~15，默认 0；与项目独占，避免和其它业务串扰）
CACHE_REDIS_DB = int(os.getenv('CACHE_REDIS_DB', 0))
# Redis 密码（用户态部署默认无密码，生产环境务必设置）
CACHE_REDIS_PASSWORD = os.getenv('CACHE_REDIS_PASSWORD', None) or None
# Redis 操作超时（秒）。100ms 足够本机访问，超时直接走降级
CACHE_REDIS_TIMEOUT = float(os.getenv('CACHE_REDIS_TIMEOUT', 0.1))
# 缓存 Key 前缀（统一命名空间，方便定位/清理）
CACHE_KEY_PREFIX = 'localrag:cache:'
# 缓存 TTL（秒），任务方案要求 24 小时
CACHE_TTL_SECONDS = int(os.getenv('CACHE_TTL_SECONDS', 86400))
# 单 session 统计 key（命中数/未命中数/总响应时间累计，用于 /api/cache/stats）
CACHE_STATS_KEY = 'localrag:cache:stats'

# ========== 公网暴露配置 ==========
# -----------------------------------------------------------------------------
# ngrok 公网暴露模块配置
# 通过 ngrok 将 LocalRAG-CS 暴露到公网，方便外网访问
# 支持通过环境变量 PUBLIC_URL 覆盖，方便不同环境切换
# -----------------------------------------------------------------------------
# 公网访问地址（ngrok 分配的域名）
PUBLIC_URL = os.getenv('PUBLIC_URL', 'https://skedaddle-morphine-shamrock.ngrok-free.dev')
# ngrok 可执行文件路径（自动检测 PATH 中的 ngrok）
NGROK_PATH = os.getenv('NGROK_PATH', '/usr/local/bin/ngrok')
# ngrok 转发目标端口（ngrok 转发到本机的端口）
NGROK_TARGET_PORT = int(os.getenv('NGROK_TARGET_PORT', 8080))
# ngrok 管理面板地址
NGROK_ADMIN_URL = os.getenv('NGROK_ADMIN_URL', 'http://127.0.0.1:4040')

# ========== 数据导入接口配置 ==========
# -----------------------------------------------------------------------------
# Web 上传导入接口使用，统一放在配置文件，避免在路由中散落硬编码
# 降级说明：Redis 不可用时任务管理器会自动降级到内存状态存储
# -----------------------------------------------------------------------------
MAX_FILE_SIZE = int(os.getenv('MAX_FILE_SIZE', 100 * 1024 * 1024))
MAX_TEXT_SIZE = int(os.getenv('MAX_TEXT_SIZE', 10 * 1024 * 1024))
UPLOAD_TEMP_DIR = os.getenv('UPLOAD_TEMP_DIR', '/tmp/localrag-uploads')
TEXT_TEMP_DIR = os.getenv('TEXT_TEMP_DIR', '/tmp/localrag-text-uploads')
DEFAULT_CHUNK_SIZE = int(os.getenv('DEFAULT_CHUNK_SIZE', 512))
DEFAULT_CHUNK_OVERLAP = int(os.getenv('DEFAULT_CHUNK_OVERLAP', 50))
MAX_CONCURRENT_TASKS = int(os.getenv('MAX_CONCURRENT_TASKS', 2))
TASK_STATUS_TTL = int(os.getenv('TASK_STATUS_TTL', 86400))
SUPPORTED_EXTENSIONS = tuple(
    ext.strip().lower()
    for ext in os.getenv(
        'SUPPORTED_EXTENSIONS',
        '.pdf,.txt,.md,.docx,.pptx,.xlsx,.csv,.html,.htm'
    ).split(',')
    if ext.strip()
)

# ========== Agent 配置（任务：agent-architecture） ==========
# -----------------------------------------------------------------------------
# Agent 模式使用 DeepSeek-V4-Flash API 进行意图理解和工具调用规划
# 不占本地显存，走火山引擎 API
# 当 DeepSeek API 不可用时自动降级到原有 RAG 流程
# -----------------------------------------------------------------------------
# Agent 开关：True 启用 Agent 模式，False 时 /api/agent/ask 返回 503
AGENT_ENABLED = os.getenv('AGENT_ENABLED', 'true').lower() == 'true'
# DeepSeek API Key（优先读环境变量 DEEPSEEK_API_KEY，config.py 里的值作为兜底）
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', 'ark-8c848111-eaee-49f1-8d7c-66a2ba64d6f1-b38c9')
# DeepSeek API 地址（火山引擎）
DEEPSEEK_API_URL = os.getenv('DEEPSEEK_API_URL', 'https://ark.cn-beijing.volces.com/api/v3/chat/completions')
# DeepSeek 模型名称
DEEPSEEK_MODEL = os.getenv('DEEPSEEK_MODEL', 'ep-20260630143620-87j6b')
# Agent 最大工具调用轮数
AGENT_MAX_ROUNDS = int(os.getenv('AGENT_MAX_ROUNDS', 3))
# Agent 单次请求超时（秒）
AGENT_TIMEOUT = int(os.getenv('AGENT_TIMEOUT', 30))

# ========== 分类/标签管理配置 ==========
# -----------------------------------------------------------------------------
# 分类索引名称
CATEGORY_INDEX_NAME = os.getenv('CATEGORY_INDEX_NAME', 'cs_knowledge_categories')
# 分类索引Mapping
CATEGORY_INDEX_MAPPING = {
    "properties": {
        "cat_id": {"type": "keyword"},
        "name": {"type": "keyword"},
        "description": {"type": "text", "analyzer": "standard"},
        "color": {"type": "keyword"},
        "created_at": {"type": "date"},
        "doc_count": {"type": "integer"}
    }
}
