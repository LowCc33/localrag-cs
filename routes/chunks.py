#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chunk 管理路由
提供 chunk 的搜索、查看、编辑、删除接口
"""

import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Query, Path
from pydantic import BaseModel, Field

import config
from dependencies import get_client_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chunks", tags=["chunks"])


# ========== 数据模型 ==========

class ChunkItem(BaseModel):
    """单个 chunk 数据模型"""
    chunk_id: str = Field(..., description="chunk 的 ES _id")
    doc_id: str = Field("", description="文档 ID")
    title: str = Field("", description="文档标题")
    content: str = Field("", description="chunk 内容")
    chunk_index: int = Field(0, description="chunk 序号")
    source_file: str = Field("", description="源文件名")
    create_time: str = Field("", description="创建时间")
    char_count: int = Field(0, description="字符数")

    class Config:
        from_attributes = True


class ChunkSearchResponse(BaseModel):
    """chunk 搜索响应"""
    total: int = Field(0, description="总条数")
    chunks: list[ChunkItem] = Field([], description="chunk 列表")
    page: int = Field(1, description="当前页码")
    size: int = Field(20, description="每页条数")

    class Config:
        from_attributes = True


class ChunkUpdateRequest(BaseModel):
    """更新 chunk 请求"""
    content: str = Field(..., description="新的 chunk 内容", min_length=1)

    class Config:
        from_attributes = True


class ChunkUpdateResponse(BaseModel):
    """更新 chunk 响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field("", description="提示信息")

    class Config:
        from_attributes = True


class ChunkDeleteResponse(BaseModel):
    """删除 chunk 响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field("", description="提示信息")

    class Config:
        from_attributes = True


# ========== 辅助函数 ==========

def _get_es():
    """获取 ES 客户端"""
    manager = get_client_manager()
    es = manager.get_client('es')
    if not es:
        raise HTTPException(status_code=503, detail="ES 客户端不可用")
    return es


# ========== API 接口 ==========

@router.get(
    "",
    response_model=ChunkSearchResponse,
    summary="搜索 chunk",
    description="按关键词搜索 chunk 内容，支持分页和按文档 ID 过滤"
)
async def search_chunks(
    keyword: Optional[str] = Query(None, description="搜索关键词"),
    doc_id: Optional[str] = Query(None, description="按文档 ID 过滤"),
    page: int = Query(1, description="页码", ge=1),
    size: int = Query(20, description="每页条数", ge=1, le=100)
):
    """搜索 chunk 列表"""
    try:
        es = _get_es()
        index_name = config.ES_INDEX_NAME
        from_val = (page - 1) * size

        # 构建查询
        must_conditions = []

        if keyword:
            # 关键词搜索 answer（内容）和 question（标题）字段
            must_conditions.append({
                "multi_match": {
                    "query": keyword,
                    "fields": ["answer", "question", "content", "title"]
                }
            })

        if doc_id:
            must_conditions.append({
                "term": {"doc_id": doc_id}
            })

        # 查询体
        body = {
            "from": from_val,
            "size": size,
            "sort": [{"create_time": {"order": "desc"}}],
            "_source": ["doc_id", "question", "answer", "title", "content", "chunk_index", "source_file", "create_time"]
        }

        if must_conditions:
            body["query"] = {"bool": {"must": must_conditions}}
        else:
            body["query"] = {"match_all": {}}

        # 执行搜索
        resp = es.client.search(index=index_name, body=body)
        total = resp.get("hits", {}).get("total", {}).get("value", 0)
        hits = resp.get("hits", {}).get("hits", [])

        chunks = []
        for hit in hits:
            src = hit.get("_source", {})
            # 兼容两种字段名：新数据用 title/content，旧数据用 question/answer
            title = src.get("title") or src.get("question") or ""
            content = src.get("content") or src.get("answer") or ""
            chunks.append(ChunkItem(
                chunk_id=hit["_id"],
                doc_id=src.get("doc_id", ""),
                title=title,
                content=content,
                chunk_index=src.get("chunk_index", 0),
                source_file=src.get("source_file", ""),
                create_time=src.get("create_time", ""),
                char_count=len(content)
            ))

        return ChunkSearchResponse(
            total=total,
            chunks=chunks,
            page=page,
            size=size
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"搜索 chunk 失败: {e}")
        raise HTTPException(status_code=500, detail=f"搜索 chunk 失败: {str(e)}")


@router.get(
    "/{chunk_id}",
    response_model=ChunkItem,
    summary="获取单个 chunk",
    description="根据 ES _id 获取单个 chunk 的完整信息"
)
async def get_chunk(
    chunk_id: str = Path(..., description="chunk 的 ES _id")
):
    """获取单个 chunk"""
    try:
        es = _get_es()
        index_name = config.ES_INDEX_NAME

        doc = es.get_document(index_name, chunk_id)
        if not doc:
            raise HTTPException(status_code=404, detail=f"chunk {chunk_id} 不存在")

        title = doc.get("title") or doc.get("question") or ""
        content = doc.get("content") or doc.get("answer") or ""
        return ChunkItem(
            chunk_id=chunk_id,
            doc_id=doc.get("doc_id", ""),
            title=title,
            content=content,
            chunk_index=doc.get("chunk_index", 0),
            source_file=doc.get("source_file", ""),
            create_time=doc.get("create_time", ""),
            char_count=len(content)
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取 chunk 失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取 chunk 失败: {str(e)}")


@router.put(
    "/{chunk_id}",
    response_model=ChunkUpdateResponse,
    summary="更新 chunk 内容",
    description="修改指定 chunk 的 content 字段，同时更新 embedding"
)
async def update_chunk(
    chunk_id: str = Path(..., description="chunk 的 ES _id"),
    req: ChunkUpdateRequest = None
):
    """更新 chunk 内容"""
    try:
        es = _get_es()
        index_name = config.ES_INDEX_NAME

        # 先检查 chunk 是否存在
        existing = es.get_document(index_name, chunk_id)
        if not existing:
            raise HTTPException(status_code=404, detail=f"chunk {chunk_id} 不存在")

        # 确定更新哪个字段
        # 新数据用 content，旧数据用 answer
        content_field = "content" if "content" in existing else "answer"

        # 更新内容
        update_body = {
            "doc": {
                content_field: req.content
            }
        }

        # 尝试重新生成 embedding
        try:
            import requests
            emb_resp = requests.post(
                config.EMBEDDING_API_URL,
                json={"content": req.content},
                timeout=10
            )
            if emb_resp.ok:
                emb_data = emb_resp.json()
                embedding = emb_data.get("embedding") or emb_data.get("data", [{}])[0].get("embedding")
                if embedding:
                    update_body["doc"]["embedding"] = embedding
        except Exception as emb_err:
            logger.warning(f"更新 embedding 失败（降级处理）: {emb_err}")

        # 执行更新
        es.client.update(index=index_name, id=chunk_id, body=update_body, refresh=True)

        logger.info(f"✅ chunk 更新成功: {chunk_id}")
        return ChunkUpdateResponse(success=True, message="更新成功")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"更新 chunk 失败: {e}")
        raise HTTPException(status_code=500, detail=f"更新 chunk 失败: {str(e)}")


@router.delete(
    "/{chunk_id}",
    response_model=ChunkDeleteResponse,
    summary="删除 chunk",
    description="从 ES 索引中删除指定 chunk"
)
async def delete_chunk(
    chunk_id: str = Path(..., description="chunk 的 ES _id")
):
    """删除 chunk"""
    try:
        es = _get_es()
        index_name = config.ES_INDEX_NAME

        # 检查 chunk 是否存在
        existing = es.get_document(index_name, chunk_id)
        if not existing:
            raise HTTPException(status_code=404, detail=f"chunk {chunk_id} 不存在")

        # 执行删除
        es.client.delete(index=index_name, id=chunk_id, refresh=True)

        logger.info(f"✅ chunk 删除成功: {chunk_id}")
        return ChunkDeleteResponse(success=True, message="删除成功")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除 chunk 失败: {e}")
        raise HTTPException(status_code=500, detail=f"删除 chunk 失败: {str(e)}")
