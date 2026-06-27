#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SQLite存储模块
基于SQLAlchemy的本地数据存储，支持文档和分块的向量存储

核心功能：
1. 数据库初始化：自动创建表和索引
2. 文档管理：存储文件元数据和哈希去重
3. 分块存储：存储文本块和对应的向量
4. 查询接口：支持向量相似度搜索
5. 去重机制：基于文件哈希避免重复导入

设计原则：
- 使用SQLAlchemy ORM，便于维护和扩展
- 支持向量相似度搜索（使用SQLite的向量扩展或自定义函数）
- 完整的索引优化，提升查询性能
- 事务支持，确保数据一致性
"""

import os
import hashlib
import logging
from datetime import datetime
from typing import List, Optional, Dict, Any, Tuple
from pathlib import Path

# SQLAlchemy相关导入
from sqlalchemy import create_engine, Column, Integer, String, Text, Float, DateTime, ForeignKey, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship, Session
from sqlalchemy.pool import StaticPool

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# SQLAlchemy基础类
Base = declarative_base()

# ========== 数据模型定义 ==========

class Document(Base):
    """文档表：存储文件元数据"""
    __tablename__ = 'documents'
    
    # 主键
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # 文件信息
    file_name = Column(String(512), nullable=False, comment='原始文件名')
    file_path = Column(String(1024), nullable=False, comment='文件路径')
    file_hash = Column(String(64), nullable=False, unique=True, comment='文件哈希（SHA256），用于去重')
    file_size = Column(Integer, nullable=False, comment='文件大小（字节）')
    file_type = Column(String(16), nullable=False, comment='文件类型：txt/md/pdf/docx/csv')
    
    # 内容信息
    title = Column(String(512), comment='文档标题（从内容提取）')
    summary = Column(Text, comment='文档摘要')
    total_chunks = Column(Integer, default=0, comment='总块数')
    
    # 元数据
    import_time = Column(DateTime, default=datetime.now, comment='导入时间')
    update_time = Column(DateTime, default=datetime.now, onupdate=datetime.now, comment='更新时间')
    
    # 状态字段
    status = Column(String(32), default='pending', comment='状态：pending/processing/completed/failed')
    error_message = Column(Text, comment='错误信息')
    
    # 关系
    chunks = relationship('Chunk', back_populates='document', cascade='all, delete-orphan')
    
    # 索引
    __table_args__ = (
        Index('idx_document_hash', 'file_hash', unique=True),
        Index('idx_document_type', 'file_type'),
        Index('idx_document_status', 'status'),
        Index('idx_document_time', 'import_time'),
    )
    
    def __repr__(self):
        return f"<Document(id={self.id}, file='{self.file_name}', chunks={self.total_chunks})>"


class Chunk(Base):
    """分块表：存储文本块和向量"""
    __tablename__ = 'chunks'
    
    # 主键
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # 外键关联
    document_id = Column(Integer, ForeignKey('documents.id', ondelete='CASCADE'), nullable=False)
    
    # 分块信息
    chunk_index = Column(Integer, nullable=False, comment='块索引（从0开始）')
    chunk_text = Column(Text, nullable=False, comment='分块文本内容')
    chunk_tokens = Column(Integer, default=0, comment='token数量估算')
    
    # 向量存储
    # 注意：SQLite原生不支持向量，这里存储为JSON字符串
    # 实际使用时可考虑使用SQLite的向量扩展或外部向量数据库
    embedding_json = Column(Text, comment='向量嵌入（JSON格式）')
    embedding_dim = Column(Integer, default=0, comment='向量维度')
    
    # 元数据
    create_time = Column(DateTime, default=datetime.now, comment='创建时间')
    
    # 关系
    document = relationship('Document', back_populates='chunks')
    
    # 索引
    __table_args__ = (
        Index('idx_chunk_document', 'document_id', 'chunk_index'),
        Index('idx_chunk_embedding', 'document_id'),
    )
    
    def __repr__(self):
        return f"<Chunk(id={self.id}, doc={self.document_id}, idx={self.chunk_index}, tokens={self.chunk_tokens})>"


class ImportTask(Base):
    """导入任务表：记录导入任务状态"""
    __tablename__ = 'import_tasks'
    
    # 主键
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # 任务信息
    task_id = Column(String(64), nullable=False, unique=True, comment='任务ID')
    task_type = Column(String(32), nullable=False, comment='任务类型：file/dir')
    
    # 文件信息
    source_path = Column(String(1024), nullable=False, comment='源文件/目录路径')
    total_files = Column(Integer, default=0, comment='总文件数')
    processed_files = Column(Integer, default=0, comment='已处理文件数')
    
    # 分块信息
    total_chunks = Column(Integer, default=0, comment='总块数')
    processed_chunks = Column(Integer, default=0, comment='已处理块数')
    
    # 状态信息
    status = Column(String(32), default='pending', comment='状态：pending/processing/completed/failed')
    progress = Column(Float, default=0.0, comment='进度百分比（0-100）')
    
    # 时间信息
    start_time = Column(DateTime, comment='开始时间')
    end_time = Column(DateTime, comment='结束时间')
    create_time = Column(DateTime, default=datetime.now, comment='创建时间')
    
    # 错误信息
    error_message = Column(Text, comment='错误信息')
    
    # 索引
    __table_args__ = (
        Index('idx_task_id', 'task_id', unique=True),
        Index('idx_task_status', 'status'),
        Index('idx_task_time', 'create_time'),
    )
    
    def __repr__(self):
        return f"<ImportTask(id={self.id}, task_id='{self.task_id}', status='{self.status}', progress={self.progress}%)>"


# ========== 存储管理器类 ==========

class StorageManager:
    """SQLite存储管理器"""
    
    def __init__(self, db_path: Optional[str] = None):
        """
        初始化存储管理器
        
        Args:
            db_path: SQLite数据库文件路径，None则使用默认路径
        """
        # 确定数据库路径
        if db_path is None:
            # 默认路径：项目根目录下的data目录
            project_root = Path(__file__).parent.parent
            data_dir = project_root / 'data'
            data_dir.mkdir(exist_ok=True)
            db_path = str(data_dir / 'localrag.db')
        
        self.db_path = db_path
        logger.info(f"初始化SQLite存储: {db_path}")
        
        # 创建数据库引擎
        # 使用StaticPool避免多线程问题，适合单线程应用
        self.engine = create_engine(
            f'sqlite:///{db_path}',
            poolclass=StaticPool,
            connect_args={'check_same_thread': False},
            echo=False  # 设置为True可查看SQL语句
        )
        
        # 创建会话工厂
        self.SessionLocal = sessionmaker(bind=self.engine)
        
        # 初始化数据库（创建表）
        self._init_database()
    
    def _init_database(self) -> None:
        """初始化数据库，创建所有表"""
        try:
            # 创建所有表
            Base.metadata.create_all(self.engine)
            logger.info("✅ 数据库表创建完成")
            
            # 检查表数量
            from sqlalchemy import inspect
            inspector = inspect(self.engine)
            tables = inspector.get_table_names()
            logger.info(f"数据库表列表: {tables}")
            
        except Exception as e:
            logger.error(f"数据库初始化失败: {e}")
            raise
    
    def get_session(self) -> Session:
        """获取数据库会话"""
        return self.SessionLocal()
    
    def close(self) -> None:
        """关闭数据库连接"""
        self.engine.dispose()
        logger.info("数据库连接已关闭")
    
    # ========== 文档管理方法 ==========
    
    def file_exists(self, file_hash: str) -> bool:
        """
        检查文件是否已存在（基于哈希去重）
        
        Args:
            file_hash: 文件哈希值
            
        Returns:
            True表示文件已存在，False表示不存在
        """
        with self.get_session() as session:
            existing = session.query(Document).filter_by(file_hash=file_hash).first()
            return existing is not None
    
    def calculate_file_hash(self, file_path: str) -> str:
        """
        计算文件哈希值（SHA256）
        
        Args:
            file_path: 文件路径
            
        Returns:
            SHA256哈希值
        """
        try:
            file_path_obj = Path(file_path)
            if not file_path_obj.exists():
                raise FileNotFoundError(f"文件不存在: {file_path}")
            
            # 使用SHA256计算文件哈希
            sha256_hash = hashlib.sha256()
            
            with open(file_path, 'rb') as f:
                # 分块读取大文件，避免内存溢出
                for chunk in iter(lambda: f.read(4096), b''):
                    sha256_hash.update(chunk)
            
            return sha256_hash.hexdigest()
            
        except Exception as e:
            logger.error(f"计算文件哈希失败 {file_path}: {e}")
            raise
    
    def create_document(self, 
                       file_path: str,
                       file_hash: str,
                       file_type: str,
                       title: Optional[str] = None,
                       summary: Optional[str] = None) -> Document:
        """
        创建文档记录
        
        Args:
            file_path: 文件路径
            file_hash: 文件哈希
            file_type: 文件类型
            title: 文档标题
            summary: 文档摘要
            
        Returns:
            创建的Document对象
        """
        try:
            file_path_obj = Path(file_path)
            
            with self.get_session() as session:
                # 创建文档记录
                document = Document(
                    file_name=file_path_obj.name,
                    file_path=str(file_path_obj.absolute()),
                    file_hash=file_hash,
                    file_size=file_path_obj.stat().st_size,
                    file_type=file_type,
                    title=title or file_path_obj.stem,
                    summary=summary or '',
                    status='pending',
                    total_chunks=0
                )
                
                session.add(document)
                session.commit()
                session.refresh(document)
                
                logger.info(f"✅ 创建文档记录: {document.file_name} (ID: {document.id})")
                return document
                
        except Exception as e:
            logger.error(f"创建文档记录失败 {file_path}: {e}")
            raise
    
    def update_document_status(self, 
                              document_id: int, 
                              status: str, 
                              total_chunks: Optional[int] = None,
                              error_message: Optional[str] = None) -> bool:
        """
        更新文档状态
        
        Args:
            document_id: 文档ID
            status: 新状态
            total_chunks: 总块数（可选）
            error_message: 错误信息（可选）
            
        Returns:
            是否成功
        """
        try:
            with self.get_session() as session:
                document = session.query(Document).filter_by(id=document_id).first()
                
                if not document:
                    logger.warning(f"文档不存在: ID={document_id}")
                    return False
                
                # 更新状态
                document.status = status
                document.update_time = datetime.now()
                
                if total_chunks is not None:
                    document.total_chunks = total_chunks
                
                if error_message:
                    document.error_message = error_message
                
                session.commit()
                logger.debug(f"✅ 更新文档状态: ID={document_id}, status={status}")
                return True
                
        except Exception as e:
            logger.error(f"更新文档状态失败 ID={document_id}: {e}")
            return False
    
    def get_document(self, document_id: int) -> Optional[Document]:
        """根据ID获取文档"""
        with self.get_session() as session:
            return session.query(Document).filter_by(id=document_id).first()
    
    def list_documents(self, 
                      limit: int = 100, 
                      offset: int = 0,
                      status: Optional[str] = None) -> Tuple[List[Document], int]:
        """
        列出文档
        
        Args:
            limit: 每页数量
            offset: 偏移量
            status: 状态过滤
            
        Returns:
            (文档列表, 总数量)
        """
        with self.get_session() as session:
            # 构建查询
            query = session.query(Document)
            
            if status:
                query = query.filter_by(status=status)
            
            # 获取总数
            total = query.count()
            
            # 获取分页数据
            documents = query.order_by(Document.import_time.desc())\
                            .offset(offset)\
                            .limit(limit)\
                            .all()
            
            return documents, total
    
    # ========== 分块管理方法 ==========
    
    def create_chunk(self,
                    document_id: int,
                    chunk_index: int,
                    chunk_text: str,
                    chunk_tokens: int,
                    embedding: Optional[List[float]] = None) -> Chunk:
        """
        创建文本块记录
        
        Args:
            document_id: 文档ID
            chunk_index: 块索引
            chunk_text: 块文本
            chunk_tokens: token数量
            embedding: 向量嵌入（可选）
            
        Returns:
            创建的Chunk对象
        """
        try:
            with self.get_session() as session:
                # 创建块记录
                chunk = Chunk(
                    document_id=document_id,
                    chunk_index=chunk_index,
                    chunk_text=chunk_text,
                    chunk_tokens=chunk_tokens,
                    embedding_dim=len(embedding) if embedding else 0
                )
                
                # 如果有向量，存储为JSON
                if embedding:
                    import json
                    chunk.embedding_json = json.dumps(embedding)
                
                session.add(chunk)
                session.commit()
                session.refresh(chunk)
                
                logger.debug(f"✅ 创建文本块: doc={document_id}, idx={chunk_index}, tokens={chunk_tokens}")
                return chunk
                
        except Exception as e:
            logger.error(f"创建文本块失败 doc={document_id}, idx={chunk_index}: {e}")
            raise
    
    def get_chunks_by_document(self, document_id: int) -> List[Chunk]:
        """获取文档的所有分块"""
        with self.get_session() as session:
            return session.query(Chunk)\
                         .filter_by(document_id=document_id)\
                         .order_by(Chunk.chunk_index.asc())\
                         .all()
    
    def count_chunks_by_document(self, document_id: int) -> int:
        """统计文档的分块数量"""
        with self.get_session() as session:
            return session.query(Chunk)\
                         .filter_by(document_id=document_id)\
                         .count()
    
    # ========== 任务管理方法 ==========
    
    def create_import_task(self,
                          task_id: str,
                          task_type: str,
                          source_path: str,
                          total_files: int = 0) -> ImportTask:
        """
        创建导入任务记录
        
        Args:
            task_id: 任务ID
            task_type: 任务类型（file/dir）
            source_path: 源路径
            total_files: 总文件数
            
        Returns:
            创建的ImportTask对象
        """
        try:
            with self.get_session() as session:
                task = ImportTask(
                    task_id=task_id,
                    task_type=task_type,
                    source_path=source_path,
                    total_files=total_files,
                    status='pending',
                    progress=0.0,
                    start_time=datetime.now()
                )
                
                session.add(task)
                session.commit()
                session.refresh(task)
                
                logger.info(f"✅ 创建导入任务: {task_id}, type={task_type}, files={total_files}")
                return task
                
        except Exception as e:
            logger.error(f"创建导入任务失败 {task_id}: {e}")
            raise
    
    def update_task_progress(self,
                           task_id: str,
                           processed_files: Optional[int] = None,
                           processed_chunks: Optional[int] = None,
                           total_chunks: Optional[int] = None,
                           status: Optional[str] = None,
                           error_message: Optional[str] = None) -> bool:
        """
        更新任务进度
        
        Args:
            task_id: 任务ID
            processed_files: 已处理文件数
            processed_chunks: 已处理块数
            total_chunks: 总块数
            status: 状态
            error_message: 错误信息
            
        Returns:
            是否成功
        """
        try:
            with self.get_session() as session:
                task = session.query(ImportTask).filter_by(task_id=task_id).first()
                
                if not task:
                    logger.warning(f"任务不存在: {task_id}")
                    return False
                
                # 更新字段
                if processed_files is not None:
                    task.processed_files = processed_files
                
                if processed_chunks is not None:
                    task.processed_chunks = processed_chunks
                
                if total_chunks is not None:
                    task.total_chunks = total_chunks
                
                if status:
                    task.status = status
                    if status in ['completed', 'failed']:
                        task.end_time = datetime.now()
                
                if error_message:
                    task.error_message = error_message
                
                # 计算进度
                if task.total_files > 0:
                    file_progress = task.processed_files / task.total_files * 100
                else:
                    file_progress = 0
                
                if task.total_chunks > 0:
                    chunk_progress = task.processed_chunks / task.total_chunks * 100
                else:
                    chunk_progress = 0
                
                # 综合进度（文件进度占70%，块进度占30%）
                task.progress = file_progress * 0.7 + chunk_progress * 0.3
                
                session.commit()
                logger.debug(f"✅ 更新任务进度: {task_id}, status={task.status}, progress={task.progress:.1f}%")
                return True
                
        except Exception as e:
            logger.error(f"更新任务进度失败 {task_id}: {e}")
            return False
    
    def get_task(self, task_id: str) -> Optional[ImportTask]:
        """根据ID获取任务"""
        with self.get_session() as session:
            return session.query(ImportTask).filter_by(task_id=task_id).first()
    
    def list_tasks(self, 
                  limit: int = 50, 
                  offset: int = 0,
                  status: Optional[str] = None) -> Tuple[List[ImportTask], int]:
        """
        列出任务
        
        Args:
            limit: 每页数量
            offset: 偏移量
            status: 状态过滤
            
        Returns:
            (任务列表, 总数量)
        """
        with self.get_session() as session:
            # 构建查询
            query = session.query(ImportTask)
            
            if status:
                query = query.filter_by(status=status)
            
            # 获取总数
            total = query.count()
            
            # 获取分页数据
            tasks = query.order_by(ImportTask.create_time.desc())\
                        .offset(offset)\
                        .limit(limit)\
                        .all()
            
            return tasks, total
    
    # ========== 统计方法 ==========
    
    def get_stats(self) -> Dict[str, Any]:
        """获取数据库统计信息"""
        with self.get_session() as session:
            # 文档统计
            total_docs = session.query(Document).count()
            completed_docs = session.query(Document).filter_by(status='completed').count()
            failed_docs = session.query(Document).filter_by(status='failed').count()
            
            # 分块统计
            total_chunks = session.query(Chunk).count()
            
            # 任务统计
            total_tasks = session.query(ImportTask).count()
            pending_tasks = session.query(ImportTask).filter_by(status='pending').count()
            processing_tasks = session.query(ImportTask).filter_by(status='processing').count()
            
            return {
                'documents': {
                    'total': total_docs,
                    'completed': completed_docs,
                    'failed': failed_docs,
                    'pending': total_docs - completed_docs - failed_docs
                },
                'chunks': {
                    'total': total_chunks,
                    'avg_per_doc': total_chunks / total_docs if total_docs > 0 else 0
                },
                'tasks': {
                    'total': total_tasks,
                    'pending': pending_tasks,
                    'processing': processing_tasks,
                    'completed': total_tasks - pending_tasks - processing_tasks
                },
                'database': {
                    'path': self.db_path,
                    'size_mb': Path(self.db_path).stat().st_size / (1024 * 1024) if Path(self.db_path).exists() else 0
                }
            }


# ========== 全局函数接口 ==========

def get_storage_manager(db_path: Optional[str] = None) -> StorageManager:
    """
    获取存储管理器实例（全局函数接口）
    
    Args:
        db_path: 数据库路径，None使用默认路径
        
    Returns:
        StorageManager实例
    """
    return StorageManager(db_path)


if __name__ == "__main__":
    # 测试代码
    import tempfile
    
    # 创建临时数据库
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
        db_path = tmp.name
    
    try:
        # 测试存储管理器
        manager = StorageManager(db_path)
        
        # 测试统计
        stats = manager.get_stats()
        print("数据库统计:")
        print(f"  文档数: {stats['documents']['total']}")
        print(f"  分块数: {stats['chunks']['total']}")
        print(f"  任务数: {stats['tasks']['total']}")
        
        # 测试创建任务
        task = manager.create_import_task(
            task_id='test_task_001',
            task_type='file',
            source_path='/tmp/test.pdf',
            total_files=1
        )
        print(f"✅ 创建任务: {task.task_id}")
        
        # 测试更新任务进度
        manager.update_task_progress(
            task_id='test_task_001',
            processed_files=1,
            processed_chunks=10,
            total_chunks=10,
            status='completed'
        )
        print("✅ 更新任务进度")
        
        # 测试文件哈希
        test_file = Path(__file__)
        file_hash = manager.calculate_file_hash(str(test_file))
        print(f"✅ 计算文件哈希: {file_hash[:16]}...")
        
        # 测试文件去重
        exists = manager.file_exists(file_hash)
        print(f"✅ 文件去重检查: {'已存在' if exists else '不存在'}")
        
        # 清理
        manager.close()
        
    finally:
        # 删除临时数据库
        if os.path.exists(db_path):
            os.unlink(db_path)
            print(f"✅ 清理临时数据库: {db_path}")