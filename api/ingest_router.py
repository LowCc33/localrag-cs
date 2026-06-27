#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据导入模块 API 路由
提供文件上传、文本导入、任务状态查询、全局统计、文档删除等功能

接口列表：
1. POST /api/ingest/upload     - 上传文件并创建异步导入任务
2. POST /api/ingest/text       - 直接传文本内容导入
3. GET  /api/ingest/status/{task_id} - 查询任务进度和状态
4. GET  /api/ingest/stats      - 获取全局统计信息
5. DELETE /api/ingest/{doc_id} - 删除文档及其所有chunks
6. GET  /api/ingest/documents  - 获取已导入文档列表
7. GET  /api/ingest/supported-formats - 获取支持的文件格式
8. GET  /api/ingest/health     - 健康检查

设计原则：
- 异步处理：上传后立即返回，后台异步处理
- 进度跟踪：提供实时进度查询
- 错误处理：友好的错误信息和状态码
- 配置化：所有参数从config.py读取，支持环境变量覆盖
- 文档生成：自动生成 Swagger/OpenAPI 文档
"""

import sys
import logging
import uuid
from typing import List, Optional, Dict, Any
from pathlib import Path

# FastAPI 相关导入
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Query, Path as PathParam
from pydantic import BaseModel, Field

# 添加项目根目录到Python路径
sys.path.insert(0, str(Path(__file__).parent.parent))

# 导入配置和任务管理器
import config
from ingestion.task_manager import get_task_manager

# 配置日志
logger = logging.getLogger(__name__)

# ========== 数据模型定义 ==========

class TaskCreateResponse(BaseModel):
    """任务创建响应模型"""
    task_id: str = Field(..., description="任务ID，用于查询进度")
    message: str = Field(..., description="任务创建消息")
    status: str = Field(..., description="任务状态: pending/processing/completed/failed")
    valid_files: int = Field(..., description="有效文件数量")
    invalid_files: int = Field(..., description="无效文件数量")
    
    class Config:
        json_schema_extra = {
            "example": {
                "task_id": "abc123",
                "message": "任务已创建",
                "status": "pending",
                "valid_files": 1,
                "invalid_files": 0
            }
        }

class TextIngestRequest(BaseModel):
    """文本导入请求模型"""
    text: str = Field(..., description="要导入的文本内容")
    title: str = Field(..., description="文档标题")
    source: str = Field("manual", description="来源，如: manual/api/webhook")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="额外元数据")
    chunk_size: int = Field(config.DEFAULT_CHUNK_SIZE, description="分块大小（字符数）", ge=100, le=2000)
    chunk_overlap: int = Field(config.DEFAULT_CHUNK_OVERLAP, description="分块重叠大小（字符数）", ge=0, le=200)
    
    class Config:
        json_schema_extra = {
            "example": {
                "text": "这是要导入的文本内容，可以是FAQ、知识库、帮助文档等。",
                "title": "用户手册",
                "source": "manual",
                "metadata": {"category": "help", "author": "admin"},
                "chunk_size": config.DEFAULT_CHUNK_SIZE,
                "chunk_overlap": config.DEFAULT_CHUNK_OVERLAP
            }
        }

class GlobalStatsResponse(BaseModel):
    """全局统计响应模型"""
    total_documents: int = Field(..., description="总文档数")
    total_chunks: int = Field(..., description="总chunk数")
    total_tokens: int = Field(..., description="总token数")
    storage_used_mb: float = Field(..., description="存储使用量(MB)")
    last_import_time: Optional[str] = Field(None, description="最后导入时间")
    active_tasks: int = Field(..., description="活跃任务数")
    completed_tasks_24h: int = Field(..., description="24小时内完成的任务数")
    failed_tasks_24h: int = Field(..., description="24小时内失败的任务数")
    avg_processing_time_sec: float = Field(..., description="平均处理时间(秒)")
    
    class Config:
        json_schema_extra = {
            "example": {
                "total_documents": 1567,
                "total_chunks": 89234,
                "total_tokens": 2345678,
                "storage_used_mb": 124.5,
                "last_import_time": "2026-06-05T14:30:25",
                "active_tasks": 3,
                "completed_tasks_24h": 42,
                "failed_tasks_24h": 2,
                "avg_processing_time_sec": 8.7
            }
        }

class DeleteResponse(BaseModel):
    """删除响应模型"""
    success: bool = Field(..., description="是否删除成功")
    message: str = Field(..., description="操作结果消息")
    deleted_doc_id: str = Field(..., description="被删除的文档ID")
    deleted_chunks: int = Field(..., description="删除的chunk数量")
    
    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "message": "文档删除成功",
                "deleted_doc_id": "doc_001",
                "deleted_chunks": 45
            }
        }

class TaskStatusResponse(BaseModel):
    """任务状态响应模型"""
    task_id: str = Field(..., description="任务ID")
    status: str = Field(..., description="任务状态: pending/processing/completed/failed")
    progress: int = Field(..., description="进度百分比 (0-100)")
    total_files: int = Field(..., description="总文件数")
    processed_files: int = Field(..., description="已处理文件数")
    total_chunks: int = Field(..., description="总chunk数")
    processed_chunks: int = Field(..., description="已处理chunk数")
    errors: List[str] = Field(default_factory=list, description="错误信息列表")
    start_time: Optional[str] = Field(None, description="任务开始时间")
    end_time: Optional[str] = Field(None, description="任务结束时间")
    
    class Config:
        json_schema_extra = {
            "example": {
                "task_id": "abc123",
                "status": "processing",
                "progress": 78,
                "total_files": 1,
                "processed_files": 0,
                "total_chunks": 156,
                "processed_chunks": 122,
                "errors": [],
                "start_time": "2026-06-05T11:26:02",
                "end_time": None
            }
        }

class DocumentItem(BaseModel):
    """文档项模型"""
    doc_id: str = Field(..., description="文档ID")
    title: str = Field(..., description="文档标题/文件名")
    file_type: str = Field(..., description="文件类型")
    chunks: int = Field(..., description="分块数量")
    import_time: str = Field(..., description="导入时间")
    
    class Config:
        json_schema_extra = {
            "example": {
                "doc_id": "doc_001",
                "title": "用户手册.pdf",
                "file_type": "pdf",
                "chunks": 45,
                "import_time": "2026-06-05T10:30:00"
            }
        }

class DocumentsListResponse(BaseModel):
    """文档列表响应模型"""
    total: int = Field(..., description="总文档数")
    documents: List[DocumentItem] = Field(..., description="文档列表")
    limit: int = Field(..., description="每页限制")
    offset: int = Field(..., description="偏移量")
    
    class Config:
        json_schema_extra = {
            "example": {
                "total": 150,
                "documents": [
                    {
                        "doc_id": "doc_001",
                        "title": "用户手册.pdf",
                        "file_type": "pdf",
                        "chunks": 45,
                        "import_time": "2026-06-05T10:30:00"
                    }
                ],
                "limit": 100,
                "offset": 0
            }
        }

class ErrorResponse(BaseModel):
    """错误响应模型"""
    error: str = Field(..., description="错误类型")
    message: str = Field(..., description="错误详情")
    detail: Optional[str] = Field(None, description="详细错误信息")
    
    class Config:
        json_schema_extra = {
            "example": {
                "error": "FileTooLarge",
                "message": "文件大小超过限制",
                "detail": "单个文件不能超过100MB"
            }
        }

# ========== 常量定义 ==========

# 从配置读取参数
MAX_FILE_SIZE = config.MAX_FILE_SIZE
MAX_TEXT_SIZE = config.MAX_TEXT_SIZE
UPLOAD_TEMP_DIR = Path(config.UPLOAD_TEMP_DIR)
TEXT_TEMP_DIR = Path(config.TEXT_TEMP_DIR)
DEFAULT_CHUNK_SIZE = config.DEFAULT_CHUNK_SIZE
DEFAULT_CHUNK_OVERLAP = config.DEFAULT_CHUNK_OVERLAP

# 支持的文件类型
SUPPORTED_EXTENSIONS = {
    ext: f"{ext.upper().lstrip('.')}文件" for ext in config.SUPPORTED_EXTENSIONS
}

# ========== 工具函数 ==========

def save_upload_file(upload_file: UploadFile, save_dir: Path) -> str:
    """
    保存上传的文件到临时目录
    
    Args:
        upload_file: 上传的文件对象
        save_dir: 保存目录
        
    Returns:
        保存后的文件路径
        
    Raises:
        HTTPException: 文件保存失败
    """
    try:
        # 确保保存目录存在
        save_dir.mkdir(parents=True, exist_ok=True)
        
        # 生成安全的文件名
        filename = upload_file.filename
        if not filename:
            raise HTTPException(status_code=400, detail="文件名不能为空")
        
        # 检查文件扩展名
        file_ext = Path(filename).suffix.lower()
        if file_ext not in SUPPORTED_EXTENSIONS:
            raise HTTPException(
                status_code=400, 
                detail=f"不支持的文件类型: {file_ext}。支持的类型: {', '.join(SUPPORTED_EXTENSIONS.keys())}"
            )
        
        # 构建保存路径
        save_path = save_dir / filename
        
        # 保存文件
        with open(save_path, "wb") as buffer:
            content = upload_file.file.read()
            
            # 检查文件大小
            if len(content) > MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=400,
                    detail=f"文件大小超过限制: {len(content)}字节 > {MAX_FILE_SIZE}字节 ({MAX_FILE_SIZE // (1024*1024)}MB)"
                )
            
            buffer.write(content)
        
        logger.info(f"✅ 文件保存成功: {save_path} ({len(content)}字节)")
        return str(save_path)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"文件保存失败: {e}")
        raise HTTPException(status_code=500, detail=f"文件保存失败: {str(e)}")

def cleanup_temp_files(file_paths: List[str]) -> None:
    """
    清理临时文件
    
    Args:
        file_paths: 要清理的文件路径列表
    """
    for file_path in file_paths:
        try:
            path = Path(file_path)
            if path.exists():
                path.unlink()
                logger.debug(f"清理临时文件: {file_path}")
        except Exception as e:
            logger.warning(f"清理临时文件失败 {file_path}: {e}")

# ========== FastAPI 路由 ==========

# 创建路由实例
router = APIRouter(
    prefix="/api/ingest",
    tags=["ingest"],
    responses={
        400: {"model": ErrorResponse, "description": "请求参数错误"},
        404: {"model": ErrorResponse, "description": "任务不存在"},
        500: {"model": ErrorResponse, "description": "服务器内部错误"}
    }
)

@router.post(
    "/upload",
    response_model=TaskCreateResponse,
    summary="上传文件并创建导入任务",
    description="""
    上传一个或多个文件，创建异步导入任务。
    
    特点：
    - 支持多文件上传
    - 立即返回任务ID，后台异步处理
    - 文件大小限制：单个文件不超过配置限制
    - 支持的文件类型：PDF, TXT, MD, DOCX, PPTX, XLSX, CSV, HTML
    
    返回：
    - task_id: 用于查询任务进度的唯一标识
    - status: 任务初始状态（pending）
    - valid_files: 有效的文件数量
    - invalid_files: 无效的文件数量（格式不支持或不存在）
    """,
    response_description="任务创建成功，返回任务信息"
)
async def upload_files(
    files: List[UploadFile] = File(..., description="要上传的文件列表"),
    chunk_size: int = Form(DEFAULT_CHUNK_SIZE, description="分块大小（字符数）", ge=100, le=2000),
    chunk_overlap: int = Form(DEFAULT_CHUNK_OVERLAP, description="分块重叠大小（字符数）", ge=0, le=200)
) -> TaskCreateResponse:
    """
    上传文件并创建异步导入任务
    
    Args:
        files: 上传的文件列表
        chunk_size: 文本分块大小，默认512字符
        chunk_overlap: 分块重叠大小，默认50字符
        
    Returns:
        任务创建响应，包含task_id和初始状态
    """
    logger.info(f"📤 收到文件上传请求: {len(files)}个文件")
    
    # 检查是否有文件
    if not files:
        raise HTTPException(status_code=400, detail="请至少上传一个文件")
    
    # 创建临时目录
    temp_dir = UPLOAD_TEMP_DIR
    saved_paths = []
    
    try:
        # 保存所有上传的文件
        for upload_file in files:
            try:
                file_path = save_upload_file(upload_file, temp_dir)
                saved_paths.append(file_path)
                logger.info(f"✅ 文件保存成功: {upload_file.filename} -> {file_path}")
            except HTTPException as e:
                logger.warning(f"文件 {upload_file.filename} 保存失败: {e.detail}")
                # 继续处理其他文件
                continue
        
        # 检查是否有成功保存的文件
        if not saved_paths:
            raise HTTPException(status_code=400, detail="没有有效的文件可处理")
        
        # 获取任务管理器
        task_manager = get_task_manager()
        
        # 创建导入任务（异步）
        task_info = task_manager.create_task(
            file_paths=saved_paths,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap
        )
        
        logger.info(f"✅ 创建导入任务: {task_info['task_id']}")
        
        # 返回任务信息
        return TaskCreateResponse(**task_info)
        
    except Exception as e:
        logger.error(f"文件上传处理失败: {e}")
        
        # 清理已保存的临时文件
        if saved_paths:
            cleanup_temp_files(saved_paths)
        
        # 返回错误响应
        if isinstance(e, HTTPException):
            raise e
        else:
            raise HTTPException(status_code=500, detail=f"服务器内部错误: {str(e)}")
    
    finally:
        # 注意：这里不清理文件，因为任务管理器需要这些文件
        # 任务管理器会在处理完成后清理文件
        pass

@router.post(
    "/text",
    response_model=TaskCreateResponse,
    summary="直接导入文本内容",
    description="""
    直接传入文本内容创建导入任务，无需上传文件。
    
    适用场景：
    - API调用直接插入知识
    - Webhook接收文本内容
    - 手动录入FAQ/帮助文档
    
    特点：
    - 支持元数据附加
    - 自动分块处理
    - 异步后台导入
    
    返回：
    - task_id: 用于查询任务进度的唯一标识
    - status: 任务初始状态（pending）
    """,
    response_description="文本导入任务创建成功"
)
async def ingest_text(
    request: TextIngestRequest
) -> TaskCreateResponse:
    """
    直接导入文本内容
    
    Args:
        request: 文本导入请求参数
        
    Returns:
        任务创建响应，包含task_id和初始状态
    """
    logger.info(f"📝 收到文本导入请求: {request.title} ({len(request.text)}字符)")
    
    # 检查文本大小
    if len(request.text.encode('utf-8')) > MAX_TEXT_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"文本大小超过限制: {len(request.text.encode('utf-8'))}字节 > {MAX_TEXT_SIZE}字节 ({MAX_TEXT_SIZE // (1024*1024)}MB)"
        )
    
    try:
        # 获取任务管理器
        task_manager = get_task_manager()
        
        # 创建临时文件保存文本内容
        temp_dir = TEXT_TEMP_DIR
        temp_dir.mkdir(parents=True, exist_ok=True)
        
        # 生成文件名
        filename = f"{request.title.replace(' ', '_')}_{uuid.uuid4().hex[:8]}.txt"
        file_path = temp_dir / filename
        
        # 保存文本到文件
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(request.text)
        
        logger.info(f"✅ 文本保存到临时文件: {file_path}")
        
        # 创建导入任务（异步）
        task_info = task_manager.create_task(
            file_paths=[str(file_path)],
            chunk_size=request.chunk_size,
            chunk_overlap=request.chunk_overlap,
            metadata={
                "title": request.title,
                "source": request.source,
                **request.metadata
            }
        )
        
        logger.info(f"✅ 创建文本导入任务: {task_info['task_id']}")
        
        # 返回任务信息
        return TaskCreateResponse(**task_info)
        
    except Exception as e:
        logger.error(f"文本导入处理失败: {e}")
        raise HTTPException(status_code=500, detail=f"文本导入失败: {str(e)}")

@router.get(
    "/status/{task_id}",
    response_model=TaskStatusResponse,
    summary="查询任务进度",
    description="""
    根据任务ID查询导入任务的进度和状态。
    
    状态说明：
    - pending: 任务已创建，等待处理
    - processing: 任务正在处理中
    - completed: 任务已完成
    - failed: 任务失败
    
    进度信息：
    - progress: 总体进度百分比
    - total_files/processed_files: 文件处理进度
    - total_chunks/processed_chunks: chunk处理进度
    - errors: 处理过程中的错误信息
    """,
    response_description="任务状态和进度信息"
)
async def get_task_status(
    task_id: str = PathParam(..., description="任务ID", example="abc123")
) -> TaskStatusResponse:
    """
    查询任务进度和状态
    
    Args:
        task_id: 任务ID
        
    Returns:
        任务状态和进度信息
    """
    logger.info(f"📊 查询任务状态: {task_id}")
    
    try:
        # 获取任务管理器
        task_manager = get_task_manager()
        
        # 查询任务状态
        status = task_manager.get_task_status(task_id)
        
        if not status:
            raise HTTPException(
                status_code=404,
                detail=f"任务不存在或已过期: {task_id}"
            )
        
        logger.debug(f"任务 {task_id} 状态: {status.get('status')}, 进度: {status.get('progress')}%")
        
        # 转换为响应模型
        return TaskStatusResponse(**status)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"查询任务状态失败: {e}")
        raise HTTPException(status_code=500, detail=f"查询任务状态失败: {str(e)}")

@router.get(
    "/stats",
    response_model=GlobalStatsResponse,
    summary="获取全局统计信息",
    description="""
    获取数据导入模块的全局统计信息，包括：
    - 文档/Chunk/Token总数
    - 存储使用情况
    - 任务处理统计
    - 性能指标
    
    用于监控和运维分析。
    """,
    response_description="全局统计信息"
)
async def get_global_stats() -> GlobalStatsResponse:
    """
    获取全局统计信息
    
    Returns:
        全局统计信息
    """
    logger.info("📊 查询全局统计信息")
    
    try:
        # 获取任务管理器
        task_manager = get_task_manager()
        
        # 获取统计信息
        stats = task_manager.get_global_stats()
        
        logger.info(f"📊 返回全局统计: {stats.get('total_documents', 0)}个文档")
        
        # 转换为响应模型
        return GlobalStatsResponse(**stats)
        
    except Exception as e:
        logger.error(f"查询全局统计失败: {e}")
        raise HTTPException(status_code=500, detail=f"查询统计信息失败: {str(e)}")

@router.delete(
    "/{doc_id}",
    response_model=DeleteResponse,
    summary="删除文档",
    description="""
    根据文档ID删除文档及其所有chunks。
    
    注意：
    - 删除操作不可逆
    - 会删除该文档对应的所有chunks
    - 文档ID可以从 /api/ingest/documents 接口获取
    
    返回：
    - success: 是否删除成功
    - deleted_chunks: 删除的chunk数量
    - message: 操作结果消息
    """,
    response_description="文档删除结果"
)
async def delete_document(
    doc_id: str = PathParam(..., description="文档ID", example="doc_001")
) -> DeleteResponse:
    """
    删除文档
    
    Args:
        doc_id: 文档ID
        
    Returns:
        删除操作结果
    """
    logger.info(f"🗑️  请求删除文档: {doc_id}")
    
    try:
        # 获取任务管理器
        task_manager = get_task_manager()
        
        # 执行删除操作
        result = task_manager.delete_document(doc_id)
        
        if result["success"]:
            logger.info(f"✅ 文档删除成功: {doc_id}, 删除chunks: {result.get('deleted_chunks', 0)}")
        else:
            logger.warning(f"⚠️  文档删除失败: {doc_id}, 原因: {result.get('message')}")
        
        # 转换为响应模型
        return DeleteResponse(**result)
        
    except Exception as e:
        logger.error(f"删除文档失败: {e}")
        raise HTTPException(status_code=500, detail=f"删除文档失败: {str(e)}")

@router.get(
    "/documents",
    response_model=DocumentsListResponse,
    summary="获取已导入文档列表",
    description="""
    获取系统中已导入的文档列表。
    
    支持分页参数：
    - limit: 每页返回的文档数量（默认100，最大500）
    - offset: 分页偏移量（默认0）
    
    返回信息：
    - total: 总文档数
    - documents: 文档列表，包含文档基本信息
    - limit/offset: 实际使用的分页参数
    """,
    response_description="文档列表信息"
)
async def get_documents_list(
    limit: int = Query(100, description="每页返回数量", ge=1, le=500),
    offset: int = Query(0, description="分页偏移量", ge=0)
) -> DocumentsListResponse:
    """
    获取已导入文档列表
    
    Args:
        limit: 每页返回数量
        offset: 分页偏移量
        
    Returns:
        文档列表信息
    """
    logger.info(f"📚 查询文档列表: limit={limit}, offset={offset}")
    
    try:
        # 获取任务管理器
        task_manager = get_task_manager()
        
        # 获取文档列表
        result = task_manager.get_documents_list(limit=limit, offset=offset)
        
        logger.info(f"📚 返回文档列表: {result.get('total')}个文档")
        
        # 转换为响应模型
        return DocumentsListResponse(**result)
        
    except Exception as e:
        logger.error(f"查询文档列表失败: {e}")
        raise HTTPException(status_code=500, detail=f"查询文档列表失败: {str(e)}")

@router.get(
    "/supported-formats",
    summary="获取支持的文件格式",
    description="返回系统支持上传和处理的文件格式列表",
    response_description="支持的文件格式信息"
)
async def get_supported_formats():
    """
    获取支持的文件格式列表
    
    Returns:
        支持的文件格式信息
    """
    return {
        "supported_formats": SUPPORTED_EXTENSIONS,
        "max_file_size_mb": MAX_FILE_SIZE // (1024 * 1024),
        "max_file_size_bytes": MAX_FILE_SIZE,
        "max_text_size_mb": MAX_TEXT_SIZE // (1024 * 1024),
        "default_chunk_size": DEFAULT_CHUNK_SIZE,
        "default_chunk_overlap": DEFAULT_CHUNK_OVERLAP,
        "upload_temp_dir": str(UPLOAD_TEMP_DIR),
        "text_temp_dir": str(TEXT_TEMP_DIR),
        "max_concurrent_tasks": config.MAX_CONCURRENT_TASKS,
        "task_status_ttl_hours": config.TASK_STATUS_TTL // 3600
    }

@router.get("/health", summary="导入模块健康检查")
async def health_check():
    """导入模块健康检查接口"""
    try:
        # 获取任务管理器
        task_manager = get_task_manager()
        
        # 检查状态
        status = {
            "status": "healthy",
            "module": "ingest-api",
            "active_tasks": len(task_manager.active_tasks),
            "max_file_size_mb": MAX_FILE_SIZE // (1024 * 1024),
            "supported_formats": len(SUPPORTED_EXTENSIONS),
            "config": {
                "upload_temp_dir": str(UPLOAD_TEMP_DIR),
                "text_temp_dir": str(TEXT_TEMP_DIR),
                "default_chunk_size": DEFAULT_CHUNK_SIZE,
                "default_chunk_overlap": DEFAULT_CHUNK_OVERLAP
            }
        }
        
        return status
        
    except Exception as e:
        logger.error(f"健康检查失败: {e}")
        return {
            "status": "unhealthy",
            "module": "ingest-api",
            "error": str(e)
        }

# ========== 模块初始化 ==========

def initialize_router():
    """初始化路由模块"""
    logger.info("✅ 数据导入API路由初始化完成")
    return router