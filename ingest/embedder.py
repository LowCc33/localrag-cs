#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
嵌入模型模块
使用llama-cpp-python加载bge-small-zh-v1.5模型进行文本向量化

核心功能：
1. 模型加载：从GGUF文件加载bge-small-zh-v1.5模型
2. 文本编码：将文本转换为向量表示
3. 批量处理：支持批量编码，提升性能
4. 错误处理：模型加载失败时的降级逻辑
5. 配置管理：模型路径、参数等可配置

设计原则：
- 最小依赖：优先使用llama-cpp-python，备选方案使用sentence-transformers
- 性能优化：支持批量编码，减少模型调用次数
- 错误容忍：模型加载失败时提供降级方案
- 配置灵活：模型路径、参数等可通过环境变量配置
"""

import os
import logging
import time
from typing import List, Optional
from pathlib import Path
from dataclasses import dataclass

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class EmbedderConfig:
    """嵌入器配置"""
    model_path: str = "models/bge-small-zh-v1.5-q4_0.gguf"  # 模型文件路径
    model_type: str = "bge-small-zh-v1.5"                   # 模型类型
    n_ctx: int = 512                                        # 上下文长度
    n_gpu_layers: int = 0                                   # GPU层数（0=仅CPU）
    n_threads: int = 4                                      # 线程数
    n_batch: int = 512                                      # 批处理大小
    use_mmap: bool = True                                   # 使用内存映射
    use_mlock: bool = False                                 # 锁定内存
    embedding_size: int = 512                               # 向量维度
    
    @classmethod
    def from_env(cls) -> 'EmbedderConfig':
        """从环境变量创建配置"""
        config = cls()
        
        # 从环境变量覆盖配置
        if os.environ.get('EMBED_MODEL_PATH'):
            config.model_path = os.environ['EMBED_MODEL_PATH']
        
        if os.environ.get('EMBED_MODEL_TYPE'):
            config.model_type = os.environ['EMBED_MODEL_TYPE']
        
        if os.environ.get('EMBED_N_CTX'):
            config.n_ctx = int(os.environ['EMBED_N_CTX'])
        
        if os.environ.get('EMBED_N_GPU_LAYERS'):
            config.n_gpu_layers = int(os.environ['EMBED_N_GPU_LAYERS'])
        
        if os.environ.get('EMBED_N_THREADS'):
            config.n_threads = int(os.environ['EMBED_N_THREADS'])
        
        if os.environ.get('EMBED_N_BATCH'):
            config.n_batch = int(os.environ['EMBED_N_BATCH'])
        
        if os.environ.get('EMBED_USE_MMAP'):
            config.use_mmap = os.environ['EMBED_USE_MMAP'].lower() == 'true'
        
        if os.environ.get('EMBED_USE_MLOCK'):
            config.use_mlock = os.environ['EMBED_USE_MLOCK'].lower() == 'true'
        
        return config


class BaseEmbedder:
    """嵌入器基类"""
    
    def __init__(self, config: Optional[EmbedderConfig] = None):
        """
        初始化嵌入器
        
        Args:
            config: 嵌入器配置，None使用默认配置
        """
        self.config = config or EmbedderConfig()
        self._model = None
        self._is_initialized = False
        
    def initialize(self) -> bool:
        """
        初始化模型
        
        Returns:
            是否初始化成功
        """
        raise NotImplementedError("子类必须实现initialize方法")
    
    def encode(self, text: str) -> List[float]:
        """
        编码单个文本
        
        Args:
            text: 输入文本
            
        Returns:
            向量表示
        """
        raise NotImplementedError("子类必须实现encode方法")
    
    def encode_batch(self, texts: List[str]) -> List[List[float]]:
        """
        批量编码文本
        
        Args:
            texts: 文本列表
            
        Returns:
            向量列表
        """
        raise NotImplementedError("子类必须实现encode_batch方法")
    
    def get_dimension(self) -> int:
        """
        获取向量维度
        
        Returns:
            向量维度
        """
        return self.config.embedding_size
    
    def is_available(self) -> bool:
        """
        检查嵌入器是否可用
        
        Returns:
            是否可用
        """
        return self._is_initialized and self._model is not None


class LlamaCppEmbedder(BaseEmbedder):
    """基于llama-cpp-python的嵌入器"""
    
    def __init__(self, config: Optional[EmbedderConfig] = None):
        super().__init__(config)
        self._llama_cpp_available = self._check_llama_cpp_availability()
    
    def _check_llama_cpp_availability(self) -> bool:
        """检查llama-cpp-python是否可用"""
        try:
            import importlib.util
            spec = importlib.util.find_spec("llama_cpp")
            return spec is not None
        except Exception:
            logger.warning("llama-cpp-python库未安装，将尝试使用备选方案")
            return False
    
    def initialize(self) -> bool:
        """初始化llama-cpp模型"""
        if not self._llama_cpp_available:
            logger.error("llama-cpp-python不可用，无法初始化嵌入器")
            return False
        
        try:
            from llama_cpp import Llama
            
            # 检查模型文件是否存在
            model_path = Path(self.config.model_path)
            if not model_path.exists():
                logger.error(f"模型文件不存在: {self.config.model_path}")
                logger.info("请下载模型文件: https://huggingface.co/bge-small-zh-v1.5")
                return False
            
            logger.info(f"加载嵌入模型: {self.config.model_path}")
            logger.info(f"模型参数: n_ctx={self.config.n_ctx}, n_threads={self.config.n_threads}")
            
            # 创建模型实例
            self._model = Llama(
                model_path=self.config.model_path,
                n_ctx=self.config.n_ctx,
                n_gpu_layers=self.config.n_gpu_layers,
                n_threads=self.config.n_threads,
                n_batch=self.config.n_batch,
                use_mmap=self.config.use_mmap,
                use_mlock=self.config.use_mlock,
                embedding=True,  # 启用嵌入模式
                verbose=False
            )
            
            # 测试模型
            test_embedding = self._model.create_embedding("测试")
            if test_embedding and 'data' in test_embedding:
                embedding_dim = len(test_embedding['data'][0]['embedding'])
                self.config.embedding_size = embedding_dim
                logger.info(f"✅ 模型加载成功，向量维度: {embedding_dim}")
                self._is_initialized = True
                return True
            else:
                logger.error("模型测试失败")
                return False
                
        except Exception as e:
            logger.error(f"模型加载失败: {e}")
            return False
    
    def encode(self, text: str) -> List[float]:
        """编码单个文本"""
        if not self.is_available():
            raise RuntimeError("嵌入器未初始化或不可用")
        
        try:
            # 使用llama-cpp创建嵌入
            result = self._model.create_embedding(text)
            
            if result and 'data' in result and len(result['data']) > 0:
                return result['data'][0]['embedding']
            else:
                logger.error(f"嵌入生成失败: {text[:50]}...")
                # 返回零向量作为降级
                return [0.0] * self.get_dimension()
                
        except Exception as e:
            logger.error(f"编码失败: {e}")
            # 返回零向量作为降级
            return [0.0] * self.get_dimension()
    
    def encode_batch(self, texts: List[str]) -> List[List[float]]:
        """批量编码文本"""
        if not self.is_available():
            raise RuntimeError("嵌入器未初始化或不可用")
        
        embeddings = []
        
        for i, text in enumerate(texts):
            try:
                embedding = self.encode(text)
                embeddings.append(embedding)
                
                # 每处理10个文本记录一次进度
                if (i + 1) % 10 == 0:
                    logger.debug(f"批量编码进度: {i + 1}/{len(texts)}")
                    
            except Exception as e:
                logger.warning(f"批量编码失败第{i}个文本: {e}")
                # 添加零向量占位
                embeddings.append([0.0] * self.get_dimension())
        
        return embeddings


class SentenceTransformerEmbedder(BaseEmbedder):
    """基于sentence-transformers的嵌入器（备选方案）"""
    
    def __init__(self, config: Optional[EmbedderConfig] = None):
        super().__init__(config)
        self._sentence_transformers_available = self._check_sentence_transformers_availability()
    
    def _check_sentence_transformers_availability(self) -> bool:
        """检查sentence-transformers是否可用"""
        try:
            import importlib.util
            spec = importlib.util.find_spec("sentence_transformers")
            return spec is not None
        except Exception:
            logger.warning("sentence-transformers库未安装")
            return False
    
    def initialize(self) -> bool:
        """初始化sentence-transformers模型"""
        if not self._sentence_transformers_available:
            logger.error("sentence-transformers不可用")
            return False
        
        try:
            from sentence_transformers import SentenceTransformer
            
            # 使用BGE小型中文模型
            model_name = "BAAI/bge-small-zh-v1.5"
            logger.info(f"加载sentence-transformers模型: {model_name}")
            
            # 创建模型实例
            self._model = SentenceTransformer(model_name)
            
            # 测试模型
            test_embedding = self._model.encode(["测试"])
            if test_embedding is not None:
                embedding_dim = test_embedding.shape[1]
                self.config.embedding_size = embedding_dim
                logger.info(f"✅ 模型加载成功，向量维度: {embedding_dim}")
                self._is_initialized = True
                return True
            else:
                logger.error("模型测试失败")
                return False
                
        except Exception as e:
            logger.error(f"模型加载失败: {e}")
            return False
    
    def encode(self, text: str) -> List[float]:
        """编码单个文本"""
        if not self.is_available():
            raise RuntimeError("嵌入器未初始化或不可用")
        
        try:
            # 使用sentence-transformers编码
            embedding = self._model.encode([text])[0]
            return embedding.tolist()
                
        except Exception as e:
            logger.error(f"编码失败: {e}")
            # 返回零向量作为降级
            return [0.0] * self.get_dimension()
    
    def encode_batch(self, texts: List[str]) -> List[List[float]]:
        """批量编码文本"""
        if not self.is_available():
            raise RuntimeError("嵌入器未初始化或不可用")
        
        try:
            # sentence-transformers原生支持批量编码
            embeddings = self._model.encode(texts)
            return embeddings.tolist()
            
        except Exception as e:
            logger.error(f"批量编码失败: {e}")
            # 降级为逐个编码
            return super().encode_batch(texts)


class DummyEmbedder(BaseEmbedder):
    """虚拟嵌入器（用于测试和降级）"""
    
    def initialize(self) -> bool:
        """初始化虚拟嵌入器"""
        logger.warning("使用虚拟嵌入器（仅用于测试）")
        self._is_initialized = True
        return True
    
    def encode(self, text: str) -> List[float]:
        """生成随机向量（用于测试）"""
        import random
        
        # 生成伪随机向量（基于文本哈希）
        import hashlib
        seed = int(hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()[:8], 16)
        random.seed(seed)
        
        return [random.random() * 2 - 1 for _ in range(self.get_dimension())]
    
    def encode_batch(self, texts: List[str]) -> List[List[float]]:
        """批量生成随机向量"""
        return [self.encode(text) for text in texts]


class EmbedderFactory:
    """嵌入器工厂"""
    
    @staticmethod
    def create_embedder(config: Optional[EmbedderConfig] = None, 
                       fallback: bool = True) -> BaseEmbedder:
        """
        创建嵌入器实例
        
        Args:
            config: 嵌入器配置
            fallback: 是否启用降级方案
            
        Returns:
            嵌入器实例
        """
        config = config or EmbedderConfig.from_env()
        
        # 尝试创建llama-cpp嵌入器
        llama_embedder = LlamaCppEmbedder(config)
        if llama_embedder._llama_cpp_available:
            logger.info("尝试使用llama-cpp嵌入器")
            if llama_embedder.initialize():
                return llama_embedder
            elif not fallback:
                raise RuntimeError("llama-cpp嵌入器初始化失败")
        
        # 尝试创建sentence-transformers嵌入器
        st_embedder = SentenceTransformerEmbedder(config)
        if st_embedder._sentence_transformers_available:
            logger.info("尝试使用sentence-transformers嵌入器")
            if st_embedder.initialize():
                return st_embedder
            elif not fallback:
                raise RuntimeError("sentence-transformers嵌入器初始化失败")
        
        # 使用虚拟嵌入器作为最后手段
        if fallback:
            logger.warning("所有嵌入器都不可用，使用虚拟嵌入器")
            dummy_embedder = DummyEmbedder(config)
            dummy_embedder.initialize()
            return dummy_embedder
        else:
            raise RuntimeError("没有可用的嵌入器")
    
    @staticmethod
    def get_available_embedders() -> List[str]:
        """获取可用的嵌入器类型"""
        available = []
        
        # 检查llama-cpp
        try:
            import importlib.util
            if importlib.util.find_spec("llama_cpp"):
                available.append("llama-cpp")
        except Exception:
            pass
        
        # 检查sentence-transformers
        try:
            import importlib.util
            if importlib.util.find_spec("sentence_transformers"):
                available.append("sentence-transformers")
        except Exception:
            pass
        
        # 虚拟嵌入器总是可用
        available.append("dummy")
        
        return available


# ========== 全局函数接口 ==========

def create_embedder(config: Optional[EmbedderConfig] = None) -> BaseEmbedder:
    """
    创建嵌入器（全局函数接口）
    
    Args:
        config: 嵌入器配置
        
    Returns:
        嵌入器实例
    """
    return EmbedderFactory.create_embedder(config)


def encode_text(text: str, config: Optional[EmbedderConfig] = None) -> List[float]:
    """
    编码文本（全局函数接口）
    
    Args:
        text: 输入文本
        config: 嵌入器配置
        
    Returns:
        向量表示
    """
    embedder = create_embedder(config)
    return embedder.encode(text)


def encode_batch(texts: List[str], config: Optional[EmbedderConfig] = None) -> List[List[float]]:
    """
    批量编码文本（全局函数接口）
    
    Args:
        texts: 文本列表
        config: 嵌入器配置
        
    Returns:
        向量列表
    """
    embedder = create_embedder(config)
    return embedder.encode_batch(texts)


if __name__ == "__main__":
    # 测试代码
    print("测试嵌入器模块...")
    
    # 检查可用嵌入器
    available = EmbedderFactory.get_available_embedders()
    print(f"可用的嵌入器: {available}")
    
    # 创建嵌入器
    try:
        embedder = create_embedder()
        print(f"✅ 嵌入器创建成功，类型: {embedder.__class__.__name__}")
        print(f"向量维度: {embedder.get_dimension()}")
        
        # 测试编码
        test_texts = ["这是一个测试文本", "这是另一个测试文本"]
        
        print("\n测试单个编码:")
        start = time.time()
        embedding = embedder.encode(test_texts[0])
        elapsed = time.time() - start
        print(f"  文本: '{test_texts[0]}'")
        print(f"  向量维度: {len(embedding)}")
        print(f"  前5个值: {embedding[:5]}")
        print(f"  耗时: {elapsed:.3f}秒")
        
        print("\n测试批量编码:")
        start = time.time()
        embeddings = embedder.encode_batch(test_texts)
        elapsed = time.time() - start
        print(f"  文本数量: {len(test_texts)}")
        print(f"  向量数量: {len(embeddings)}")
        print(f"  耗时: {elapsed:.3f}秒 ({elapsed/len(test_texts):.3f}秒/文本)")
        
    except Exception as e:
        print(f"❌ 嵌入器测试失败: {e}")
        import traceback
        traceback.print_exc()