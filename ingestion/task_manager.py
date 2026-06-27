#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据导入异步任务管理器
提供任务状态跟踪、进度更新、Redis持久化等功能

核心功能：
1. 任务创建和状态管理
2. 进度实时更新（每处理10个chunk更新一次）
3. 任务状态查询
4. 错误处理和异常记录

设计原则：
- 异步执行：使用 ThreadPoolExecutor 不阻塞主进程
- 状态持久化：任务状态存储在 Redis 中
- 进度实时性：定期更新进度，但不过于频繁（每10个chunk）
- 错误容忍：单个文件失败不影响整体任务
- 资源友好：限制并发任务数，避免资源耗尽
"""

import os
import sys
import time
import json
import uuid
import logging
from typing import Dict, List, Optional, Any
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime

# 添加项目根目录到Python路径
sys.path.insert(0, str(Path(__file__).parent.parent))

# 导入项目配置和Redis客户端
import config
from ingestion.pipeline import process_single_file
from ingestion.parsers import get_supported_extensions

# 配置日志
logger = logging.getLogger(__name__)

# ========== Redis客户端封装 ==========

class RedisClient:
    """Redis客户端封装，提供任务状态存储和查询功能"""
    
    def __init__(self):
        """初始化Redis连接"""
        self._redis = None
        self._connect()
    
    def _connect(self):
        """连接Redis服务器"""
        try:
            import redis
            self._redis = redis.Redis(
                host=config.CACHE_REDIS_HOST,
                port=config.CACHE_REDIS_PORT,
                db=config.CACHE_REDIS_DB,
                password=config.CACHE_REDIS_PASSWORD,
                socket_timeout=config.CACHE_REDIS_TIMEOUT,
                decode_responses=True
            )
            # 测试连接
            self._redis.ping()
            logger.info("✅ Redis连接成功")
        except Exception as e:
            logger.error(f"❌ Redis连接失败: {e}")
            # 降级处理：使用内存存储（仅限开发环境）
            self._redis = None
            self._memory_store = {}
    
    def _get_key(self, task_id: str) -> str:
        """生成Redis键名"""
        return f"localrag:ingest:task:{task_id}"
    
    def save_task_status(self, task_id: str, status_data: Dict[str, Any]) -> bool:
        """保存任务状态到Redis"""
        try:
            if self._redis:
                key = self._get_key(task_id)
                # 设置过期时间（24小时）
                self._redis.setex(key, 86400, json.dumps(status_data))
            else:
                # 降级：内存存储
                self._memory_store[task_id] = status_data
            return True
        except Exception as e:
            logger.error(f"保存任务状态失败: {e}")
            return False
    
    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """从Redis获取任务状态"""
        try:
            if self._redis:
                key = self._get_key(task_id)
                data = self._redis.get(key)
                if data:
                    return json.loads(data)
            else:
                # 降级：内存存储
                return self._memory_store.get(task_id)
        except Exception as e:
            logger.error(f"获取任务状态失败: {e}")
        return None
    
    def delete_task_status(self, task_id: str) -> bool:
        """删除任务状态"""
        try:
            if self._redis:
                key = self._get_key(task_id)
                self._redis.delete(key)
            else:
                # 降级：内存存储
                self._memory_store.pop(task_id, None)
            return True
        except Exception as e:
            logger.error(f"删除任务状态失败: {e}")
            return False

# ========== 任务管理器 ==========

class TaskManager:
    """异步任务管理器，负责创建、执行和跟踪导入任务"""
    
    def __init__(self, max_workers: int = 2):
        """
        初始化任务管理器
        
        Args:
            max_workers: 最大并发工作线程数，默认2避免资源耗尽
        """
        self.redis_client = RedisClient()
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.active_tasks = {}  # task_id -> future 映射
        
        logger.info(f"✅ 任务管理器初始化完成 (最大并发数: {max_workers})")
    
    def create_task(self, file_paths: List[str], chunk_size: int = 512, 
                   chunk_overlap: int = 50, metadata: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        创建新的导入任务
        
        Args:
            file_paths: 文件路径列表
            chunk_size: 分块大小
            chunk_overlap: 分块重叠大小
            metadata: 元数据字典
            
        Returns:
            任务信息字典，包含task_id和初始状态
        """
        # 生成唯一任务ID
        task_id = str(uuid.uuid4())[:8]
        
        # 过滤支持的文件类型
        supported_exts = get_supported_extensions()
        valid_files = []
        invalid_files = []
        
        for file_path in file_paths:
            path = Path(file_path)
            if not path.exists():
                invalid_files.append(f"{file_path} (文件不存在)")
            elif path.suffix.lower() not in supported_exts:
                invalid_files.append(f"{file_path} (不支持的文件类型: {path.suffix})")
            else:
                valid_files.append(str(path.absolute()))
        
        # 构建初始任务状态
        task_status = {
            "task_id": task_id,
            "status": "pending",  # pending -> processing -> completed/failed
            "progress": 0,
            "total_files": len(valid_files),
            "processed_files": 0,
            "total_chunks": 0,  # 将在处理过程中更新
            "processed_chunks": 0,
            "errors": invalid_files,
            "valid_files": valid_files,
            "invalid_files": invalid_files,
            "start_time": datetime.now().isoformat(),
            "end_time": None,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "metadata": metadata or {}
        }
        
        # 保存初始状态到Redis
        self.redis_client.save_task_status(task_id, task_status)
        
        logger.info(f"✅ 创建任务 {task_id}: {len(valid_files)}个有效文件, {len(invalid_files)}个无效文件")
        
        # 提交异步任务
        future = self.executor.submit(
            self._process_task, task_id, valid_files, chunk_size, chunk_overlap, metadata or {}
        )
        self.active_tasks[task_id] = future
        
        return {
            "task_id": task_id,
            "message": "任务已创建",
            "status": "pending",
            "valid_files": len(valid_files),
            "invalid_files": len(invalid_files)
        }
    
    def _process_task(self, task_id: str, file_paths: List[str], 
                     chunk_size: int, chunk_overlap: int, metadata: Dict[str, Any]) -> None:
        """
        异步处理任务（在工作线程中执行）
        
        Args:
            task_id: 任务ID
            file_paths: 有效文件路径列表
            chunk_size: 分块大小
            chunk_overlap: 分块重叠大小
            metadata: 元数据字典
        """
        logger.info(f"🚀 开始处理任务 {task_id}: {len(file_paths)}个文件")
        
        # 更新状态为处理中
        self._update_task_status(task_id, {
            "status": "processing",
            "progress": 0
        })
        
        total_chunks = 0
        processed_chunks = 0
        processed_files = 0
        errors = []
        
        try:
            # 先统计总chunk数（用于进度计算）
            logger.info(f"📊 任务 {task_id}: 正在统计文件信息...")
            for file_path in file_paths:
                try:
                    # 这里可以调用pipeline的统计功能，暂时简化处理
                    # 实际应该根据文件大小估算chunk数
                    file_size = os.path.getsize(file_path)
                    estimated_chunks = max(1, file_size // (chunk_size * 1000))
                    total_chunks += estimated_chunks
                except Exception as e:
                    errors.append(f"{file_path}: 统计失败 - {str(e)}")
            
            # 更新总chunk数
            if total_chunks > 0:
                self._update_task_status(task_id, {
                    "total_chunks": total_chunks
                })
            
            # 处理每个文件
            for i, file_path in enumerate(file_paths):
                try:
                    logger.info(f"📄 任务 {task_id}: 处理文件 {i+1}/{len(file_paths)}: {Path(file_path).name}")
                    
                    # 调用pipeline处理单个文件
                    stats = process_single_file(
                        file_path=file_path,
                        chunk_size=chunk_size,
                        overlap_size=chunk_overlap  # 修复：参数名改为overlap_size
                        # metadata参数已移除，因为process_single_file不接受该参数
                    )
                    
                    # 更新进度
                    processed_files += 1
                    processed_chunks += stats.chunks_count  # 修复：改为chunks_count
                    
                    # 每处理10个chunk更新一次进度（避免过于频繁）
                    if processed_chunks % 10 == 0 or i == len(file_paths) - 1:
                        progress = int((processed_files / len(file_paths)) * 100) if file_paths else 0
                        self._update_task_status(task_id, {
                            "processed_files": processed_files,
                            "processed_chunks": processed_chunks,
                            "progress": progress
                        })
                    
                    logger.info(f"✅ 任务 {task_id}: 文件处理完成 - {stats}")
                    
                except Exception as e:
                    error_msg = f"{file_path}: 处理失败 - {str(e)}"
                    errors.append(error_msg)
                    logger.error(f"❌ 任务 {task_id}: {error_msg}")
                    
                    # 更新错误列表
                    self._update_task_status(task_id, {
                        "errors": errors
                    })
            
            # 任务完成
            final_progress = 100
            self._update_task_status(task_id, {
                "status": "completed",
                "progress": final_progress,
                "processed_files": processed_files,
                "processed_chunks": processed_chunks,
                "end_time": datetime.now().isoformat(),
                "errors": errors
            })
            
            logger.info(f"🎉 任务 {task_id} 完成: {processed_files}/{len(file_paths)}个文件, {processed_chunks}个chunk")
            
        except Exception as e:
            # 任务整体失败
            logger.error(f"💥 任务 {task_id} 失败: {e}")
            self._update_task_status(task_id, {
                "status": "failed",
                "progress": 0,
                "end_time": datetime.now().isoformat(),
                "errors": errors + [f"任务整体失败: {str(e)}"]
            })
        
        finally:
            # 从活动任务中移除
            self.active_tasks.pop(task_id, None)
    
    def _update_task_status(self, task_id: str, updates: Dict[str, Any]) -> None:
        """
        更新任务状态
        
        Args:
            task_id: 任务ID
            updates: 要更新的字段
        """
        try:
            # 获取当前状态
            current_status = self.redis_client.get_task_status(task_id)
            if not current_status:
                logger.warning(f"任务 {task_id} 状态不存在")
                return
            
            # 合并更新
            current_status.update(updates)
            
            # 保存到Redis
            self.redis_client.save_task_status(task_id, current_status)
            
        except Exception as e:
            logger.error(f"更新任务状态失败: {e}")
    
    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        获取任务状态
        
        Args:
            task_id: 任务ID
            
        Returns:
            任务状态字典，如果任务不存在则返回None
        """
        status = self.redis_client.get_task_status(task_id)
        
        # 如果任务在活动列表中，检查是否已完成
        if task_id in self.active_tasks:
            future = self.active_tasks[task_id]
            if future.done():
                try:
                    future.result()  # 触发异常（如果有）
                except Exception as e:
                    logger.error(f"任务 {task_id} 执行异常: {e}")
        
        return status
    
    def get_documents_list(self, limit: int = 100, offset: int = 0) -> Dict[str, Any]:
        """
        获取已导入的文档列表
        
        从ES查询文档列表，按source_file去重聚合。
        
        Args:
            limit: 返回数量限制
            offset: 偏移量
            
        Returns:
            文档列表信息
        """
        try:
            es = self._get_es_client()
            index_name = config.ES_INDEX_NAME
            
            if not es.index_exists(index_name):
                return {"total": 0, "documents": [], "limit": limit, "offset": offset}
            
            # 按source_file聚合，获取每个文件的首条记录和chunk数
            agg_resp = es.client.search(
                index=index_name,
                body={
                    "size": 0,
                    "aggs": {
                        "by_source": {
                            "terms": {
                                "field": "source_file.keyword",
                                "size": 10000,
                                "order": {"max_create_time": "desc"}
                            },
                            "aggs": {
                                "max_create_time": {
                                    "max": {"field": "create_time"}
                                },
                                "top_hit": {
                                    "top_hits": {
                                        "size": 1,
                                        "_source": ["doc_id", "question", "source_file", "category", "create_time"]
                                    }
                                }
                            }
                        }
                    }
                }
            )
            
            buckets = agg_resp.get('aggregations', {}).get('by_source', {}).get('buckets', [])
            total = len(buckets)
            
            # 分页
            page_buckets = buckets[offset:offset + limit]
            
            documents = []
            for bucket in page_buckets:
                source_file = bucket.get('key', '')
                chunks_count = bucket.get('doc_count', 0)
                top_hit = bucket.get('top_hit', {}).get('hits', {}).get('hits', [])
                
                doc_id = ''
                title = source_file
                import_time = ''
                
                if top_hit:
                    src = top_hit[0].get('_source', {})
                    doc_id = src.get('doc_id', '')
                    title = src.get('question', '') or src.get('source_file', '') or source_file
                    import_time = src.get('create_time', '')
                
                # 文件类型
                ext = Path(source_file).suffix.lower().lstrip('.')
                file_type = ext if ext else 'unknown'
                
                documents.append({
                    "doc_id": doc_id,
                    "title": title,
                    "file_type": file_type,
                    "chunks": chunks_count,
                    "import_time": import_time
                })
            
            logger.info(f"文档列表查询: 共{total}个文件，返回{len(documents)}条")
            
            return {
                "total": total,
                "documents": documents,
                "limit": limit,
                "offset": offset
            }
            
        except Exception as e:
            logger.error(f"获取文档列表失败: {e}")
            return {
                "total": 0,
                "documents": [],
                "limit": limit,
                "offset": offset
            }
    
    def get_global_stats(self) -> Dict[str, Any]:
        """
        获取全局统计信息
        
        从ES查询真实统计数据。
        
        Returns:
            全局统计信息字典
        """
        try:
            es = self._get_es_client()
            index_name = config.ES_INDEX_NAME
            
            if not es.index_exists(index_name):
                return self._empty_stats()
            
            # 总文档数（按source_file去重）
            agg_docs = es.client.search(
                index=index_name,
                body={
                    "size": 0,
                    "aggs": {
                        "by_source": {
                            "cardinality": {"field": "source_file.keyword"}
                        }
                    }
                }
            )
            total_documents = agg_docs.get('aggregations', {}).get('by_source', {}).get('value', 0)
            
            # 总chunks数
            count_resp = es.client.count(index=index_name)
            total_chunks = count_resp.get('count', 0)
            
            # 最近导入时间
            recent_resp = es.client.search(
                index=index_name,
                body={
                    "size": 1,
                    "sort": [{"create_time": {"order": "desc"}}],
                    "_source": ["create_time"]
                }
            )
            recent_hits = recent_resp.get('hits', {}).get('hits', [])
            last_import_time = None
            if recent_hits:
                last_import_time = recent_hits[0].get('_source', {}).get('create_time')
            
            # 存储空间估算（每个chunk约2KB文本 + 4KB向量 ≈ 6KB）
            storage_used_mb = total_chunks * 6.0 / 1024.0
            
            # 24小时内的任务统计
            from datetime import datetime, timedelta
            now = datetime.now()
            cutoff = (now - timedelta(hours=24)).isoformat()
            
            completed_tasks_24h = 0
            failed_tasks_24h = 0
            # 从Redis获取所有任务状态（如果有）
            try:
                all_tasks = self.redis_client.get_all_task_statuses() if hasattr(self.redis_client, 'get_all_task_statuses') else {}
                for task in all_tasks.values():
                    if task.get('start_time', '') >= cutoff:
                        if task.get('status') == 'completed':
                            completed_tasks_24h += 1
                        elif task.get('status') == 'failed':
                            failed_tasks_24h += 1
            except Exception:
                pass
            
            stats = {
                "total_documents": total_documents,
                "total_chunks": total_chunks,
                "total_tokens": total_chunks * 200,  # 估算：每个chunk约200 token
                "storage_used_mb": round(storage_used_mb, 2),
                "last_import_time": last_import_time,
                "active_tasks": len(self.active_tasks),
                "completed_tasks_24h": completed_tasks_24h,
                "failed_tasks_24h": failed_tasks_24h,
                "avg_processing_time_sec": round(self._calc_avg_processing_time(), 2)
            }
            
            logger.debug(f"全局统计: {stats}")
            return stats
            
        except Exception as e:
            logger.error(f"获取全局统计失败: {e}")
            return self._empty_stats()
    
    def delete_document(self, doc_id: str) -> Dict[str, Any]:
        """
        删除文档及其所有chunks
        
        通过ES按doc_id删除所有匹配的文档。
        
        Args:
            doc_id: 文档ID
            
        Returns:
            删除操作结果
        """
        try:
            logger.info(f"正在删除文档: {doc_id}")
            
            es = self._get_es_client()
            index_name = config.ES_INDEX_NAME
            
            if not es.index_exists(index_name):
                return {
                    "success": False,
                    "message": f"索引 {index_name} 不存在",
                    "deleted_doc_id": doc_id,
                    "deleted_chunks": 0
                }
            
            # 按doc_id删除
            # 先查询匹配的文档数
            count_resp = es.client.count(
                index=index_name,
                body={
                    "query": {
                        "term": {"doc_id.keyword": doc_id}
                    }
                }
            )
            match_count = count_resp.get('count', 0)
            
            if match_count == 0:
                # 尝试按source_file前缀匹配（doc_id格式：文件名_索引）
                count_resp = es.client.count(
                    index=index_name,
                    body={
                        "query": {
                            "wildcard": {"doc_id.keyword": f"{doc_id}_*"}
                        }
                    }
                )
                match_count = count_resp.get('count', 0)
            
            if match_count == 0:
                logger.warning(f"未找到匹配的文档: {doc_id}")
                return {
                    "success": False,
                    "message": f"未找到文档: {doc_id}",
                    "deleted_doc_id": doc_id,
                    "deleted_chunks": 0
                }
            
            # 执行删除
            delete_resp = es.client.delete_by_query(
                index=index_name,
                body={
                    "query": {
                        "bool": {
                            "should": [
                                {"term": {"doc_id.keyword": doc_id}},
                                {"wildcard": {"doc_id.keyword": f"{doc_id}_*"}}
                            ]
                        }
                    }
                },
                refresh=True
            )
            
            deleted = delete_resp.get('deleted', 0)
            
            result = {
                "success": True,
                "message": f"文档 {doc_id} 删除成功",
                "deleted_doc_id": doc_id,
                "deleted_chunks": deleted
            }
            
            logger.info(f"文档删除成功: {doc_id}, 删除chunks: {deleted}")
            return result
            
        except Exception as e:
            logger.error(f"删除文档失败: {e}")
            return {
                "success": False,
                "message": f"删除文档失败: {str(e)}",
                "deleted_doc_id": doc_id,
                "deleted_chunks": 0
            }
    
    def create_task_with_metadata(self, file_paths: List[str], chunk_size: int = 512, 
                                 chunk_overlap: int = 50, metadata: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        创建带元数据的导入任务
        
        Args:
            file_paths: 文件路径列表
            chunk_size: 分块大小
            chunk_overlap: 分块重叠大小
            metadata: 元数据字典
            
        Returns:
            任务信息字典
        """
        # 先创建基础任务
        task_info = self.create_task(file_paths, chunk_size, chunk_overlap)
        
        # 获取任务ID
        task_id = task_info["task_id"]
        
        # 更新任务状态，添加元数据
        if metadata:
            current_status = self.redis_client.get_task_status(task_id)
            if current_status:
                current_status["metadata"] = metadata
                self.redis_client.save_task_status(task_id, current_status)
                
                logger.info(f"任务 {task_id} 添加元数据: {metadata}")
        
        return task_info
    
    def create_text_import_task(self, text: str, title: str = "未命名文档",
                               chunk_size: int = 512, chunk_overlap: int = 50) -> str:
        """
        创建文本导入任务
        
        Args:
            text: 要导入的文本内容
            title: 文档标题
            chunk_size: 分块大小
            chunk_overlap: 分块重叠大小
            
        Returns:
            任务ID
        """
        # 生成唯一任务ID
        task_id = str(uuid.uuid4())[:8]
        
        # 创建临时文件保存文本内容
        import tempfile
        temp_dir = Path(tempfile.gettempdir()) / "localrag-text-import"
        temp_dir.mkdir(parents=True, exist_ok=True)
        
        temp_file = temp_dir / f"{task_id}.txt"
        temp_file.write_text(text, encoding="utf-8")
        
        # 构建初始任务状态
        task_status = {
            "task_id": task_id,
            "status": "pending",
            "progress": 0,
            "total_files": 1,
            "processed_files": 0,
            "total_chunks": 0,
            "processed_chunks": 0,
            "errors": [],
            "valid_files": [str(temp_file.absolute())],
            "invalid_files": [],
            "start_time": datetime.now().isoformat(),
            "end_time": None,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "metadata": {
                "source": "text",
                "title": title,
                "text_length": len(text)
            }
        }
        
        # 保存初始状态到Redis
        self.redis_client.save_task_status(task_id, task_status)
        
        logger.info(f"✅ 创建文本导入任务 {task_id}: 标题={title}, 长度={len(text)}字符")
        
        # 提交异步任务
        future = self.executor.submit(
            self._process_task, task_id, [str(temp_file.absolute())], chunk_size, chunk_overlap
        )
        self.active_tasks[task_id] = future
        
        return task_id
    
    def get_total_documents(self) -> int:
        """
        获取总文档数
        
        Returns:
            总文档数
        """
        # 这里应该从数据库获取实际文档数
        # 暂时返回模拟数据
        return 42
    
    def get_total_chunks(self) -> int:
        """
        获取总chunks数
        
        Returns:
            总chunks数
        """
        # 这里应该从数据库获取实际chunks数
        # 暂时返回模拟数据
        return 1256
    
    def get_total_size_mb(self) -> float:
        """
        获取总占用空间(MB)
        
        Returns:
            总占用空间(MB)
        """
        # 这里应该从数据库获取实际占用空间
        # 暂时返回模拟数据
        return 15.8
    
    def get_last_import_time(self) -> Optional[str]:
        """
        获取最后导入时间
        
        Returns:
            最后导入时间字符串，或None
        """
        # 这里应该从数据库获取最后导入时间
        # 暂时返回模拟数据
        return datetime.now().isoformat()
    

    
    def cleanup_old_tasks(self, max_age_hours: int = 24) -> int:
        """
        清理过期任务（超过指定时间的任务）
        
        Args:
            max_age_hours: 最大保留时间（小时）
            
        Returns:
            清理的任务数量
        """
        # 注意：Redis有自动过期机制，这里主要清理内存中的引用
        # 实际生产环境中可能需要更复杂的清理逻辑
        cleaned = 0
        
        for task_id in list(self.active_tasks.keys()):
            future = self.active_tasks[task_id]
            if future.done():
                # 任务已完成，清理引用
                self.active_tasks.pop(task_id, None)
                cleaned += 1
        
        logger.info(f"🧹 清理了 {cleaned} 个已完成的任务")
        return cleaned
    
    def _get_es_client(self):
        """
        获取或创建ES客户端
        
        Returns:
            ElasticsearchClient 实例
        """
        if not hasattr(self, '_es_client') or self._es_client is None:
            from core.es_client import ElasticsearchClient
            self._es_client = ElasticsearchClient()
        return self._es_client
    
    def _empty_stats(self) -> Dict[str, Any]:
        """返回空的统计信息"""
        return {
            "total_documents": 0,
            "total_chunks": 0,
            "total_tokens": 0,
            "storage_used_mb": 0.0,
            "last_import_time": None,
            "active_tasks": len(self.active_tasks),
            "completed_tasks_24h": 0,
            "failed_tasks_24h": 0,
            "avg_processing_time_sec": 0.0
        }
    
    def _calc_avg_processing_time(self) -> float:
        """
        计算平均处理时间
        
        Returns:
            平均处理时间（秒）
        """
        times = []
        try:
            all_tasks = self.redis_client.get_all_task_statuses() if hasattr(self.redis_client, 'get_all_task_statuses') else {}
            for task in all_tasks.values():
                if task.get('start_time') and task.get('end_time'):
                    try:
                        start = datetime.fromisoformat(task['start_time'])
                        end = datetime.fromisoformat(task['end_time'])
                        times.append((end - start).total_seconds())
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass
        
        if not times:
            return 0.0
        return sum(times) / len(times)
    
    def shutdown(self):
        """关闭任务管理器，清理资源"""
        logger.info("🛑 关闭任务管理器...")
        self.executor.shutdown(wait=True)
        logger.info("✅ 任务管理器已关闭")

# ========== 全局任务管理器实例 ==========

# 创建全局任务管理器实例
_task_manager = None

def get_task_manager() -> TaskManager:
    """获取全局任务管理器实例（单例模式）"""
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskManager()
    return _task_manager

# ========== 测试代码 ==========

if __name__ == "__main__":
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # 测试任务管理器
    manager = TaskManager(max_workers=1)
    
    try:
        # 创建测试任务
        test_files = ["test.pdf"]  # 需要实际存在的测试文件
        task_info = manager.create_task(test_files)
        
        print(f"创建任务: {task_info}")
        
        # 等待任务完成
        import time
        for i in range(10):
            status = manager.get_task_status(task_info["task_id"])
            print(f"任务状态: {status}")
            if status and status.get("status") in ["completed", "failed"]:
                break
            time.sleep(1)
        
    finally:
        manager.shutdown()