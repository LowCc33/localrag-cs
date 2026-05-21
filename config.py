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

# LLM 系统提示词
LLM_SYSTEM_PROMPT = """你是一个专业的问答助手。请严格基于提供的上下文信息回答用户的问题。
要求：
1. 只使用上下文中的信息，不要编造内容
2. 如果上下文中没有答案，直接回答"抱歉，我无法回答这个问题"
3. 回答要简洁、准确，使用中文
4. 不要提及"根据上下文"之类的话，直接给出答案
"""
