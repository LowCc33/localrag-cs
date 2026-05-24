#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
问答接口路由
提供 POST /api/ask 接口，执行完整的RAG链路：
混合检索 -> 重排 -> LLM生成
"""

import asyncio
import json
import time
import logging
from typing import List
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse

# 配置日志
logger = logging.getLogger(__name__)

# 导入API模型
from schemas import (
    AskRequest, 
    AskResponse, 
    SourceDoc, 
    LatencyStats
)

# 导入依赖
from dependencies import (
    get_retriever,
    get_reranker,
    get_llm
)

# 导入配置（流式相关常量统一从 config.py 取，禁止硬编码）
import config

router = APIRouter(tags=["ask"])


@router.post("/api/ask", response_model=AskResponse)
async def ask_endpoint(
    request: AskRequest,
    retriever=Depends(get_retriever),
    reranker=Depends(get_reranker),
    llm=Depends(get_llm)
):
    """
    问答接口 - 执行完整的RAG链路
    
    ## 处理流程
    1. **混合检索**：使用 HybridRetriever 执行 ES + 向量混合检索
    2. **重排**：使用 RerankerClient 对检索结果重排序
    3. **构建上下文**：将重排后的文档组合成LLM上下文
    4. **生成答案**：使用 LLMClient 生成最终回答
    
    ## 参数说明
    - **question**: 用户问题（必填，1-1000字符）
    - **top_k**: 混合检索召回数量（默认10，范围1-50）
    - **rerank_top_k**: 重排后返回数量（默认3，范围1-10）
    - **index_name**: ES索引名称（默认"cs_knowledge_base"）
    
    ## 返回说明
    - **answer**: LLM生成的最终答案
    - **sources**: 检索到的源文档列表（包含分数和元数据）
    - **latency**: 各阶段耗时统计（毫秒）
    - **retrieved_count**: 混合召回的文档数量
    - **reranked_count**: 重排后的文档数量
    """
    
    # 记录整体开始时间
    total_start = time.perf_counter()
    
    try:
        # ========== 阶段1: 混合检索 ==========
        retrieval_start = time.perf_counter()
        
        # 执行混合检索
        hybrid_results = retriever.search(
            query=request.question,
            top_k=request.top_k,
            index_name=request.index_name
        )
        
        retrieval_ms = (time.perf_counter() - retrieval_start) * 1000
        retrieved_count = len(hybrid_results)
        
        # 如果混合检索没有结果，直接返回
        if not hybrid_results:
            total_ms = (time.perf_counter() - total_start) * 1000
            return AskResponse(
                question=request.question,
                answer="抱歉，知识库中没有找到与您问题相关的内容。\n\n可能原因：\n1. 您的问题比较宽泛，建议尝试更具体的术语\n2. 当前知识库覆盖范围有限\n\n您可以尝试换个方式提问。",
                sources=[],
                latency=LatencyStats(
                    total_ms=round(total_ms, 2),
                    retrieval_ms=round(retrieval_ms, 2),
                    rerank_ms=0.0,
                    llm_ms=0.0
                ),
                retrieved_count=0,
                reranked_count=0,
                status="success"
            )
        
        # ========== 阶段2: 重排 ==========
        rerank_start = time.perf_counter()
        
        try:
            # 提取文档列表（HybridRetriever返回的字段是 'answer'）
            documents = [doc.get('answer', '') for doc in hybrid_results]
            
            # 执行重排
            rerank_results = reranker.rerank(request.question, documents)
            
            # 提取分数并按原索引重组
            rerank_scores = [0.0] * len(hybrid_results)
            for item in rerank_results:
                idx = item['index']
                if idx < len(rerank_scores):
                    rerank_scores[idx] = item['score']
            
            # 将重排分数与文档组合
            scored_docs = list(zip(hybrid_results, rerank_scores))
            
            # 按重排分数降序排序
            scored_docs.sort(key=lambda x: x[1], reverse=True)
            
            # 取TOP-K，强制截断为3条（不管配置和降级情况）
            top_k_docs = scored_docs[:request.rerank_top_k]
            top_k_docs = top_k_docs[:3]  # 强制最多返回3条文档
            
        except Exception as e:
            # 重排服务不可用，降级到不重排，按检索分数排序后取前3条
            logger.warning(f"重排服务不可用，降级到不重排: {e}")
            # 按检索分数降序排序后取前3条
            sorted_results = sorted(hybrid_results, key=lambda x: x.get('score', x.get('_score', 0)), reverse=True)
            top_k_docs = [(doc, 0.0) for doc in sorted_results[:3]]  # 强制最多3条
        
        rerank_ms = (time.perf_counter() - rerank_start) * 1000
        reranked_count = len(top_k_docs)
        
        # ========== 阶段3: 构建上下文并生成答案 ==========
        llm_start = time.perf_counter()
        
        # 构建上下文文档
        context_docs = []
        source_docs = []
        
        for doc, rerank_score in top_k_docs:
            # 构建SourceDoc（字段对应HybridRetriever返回的格式）
            source_doc = SourceDoc(
                doc_id=doc.get('doc_id', ''),
                title=doc.get('question', ''),  # question 字段作为标题
                content=doc.get('answer', '')[:500],  # answer 字段作为内容
                source_file=doc.get('category', ''),  # category 临时作为来源标识
                es_score=doc.get('bm25_score', doc.get('score', 0.0)),  # BM25原始分数
                vector_score=doc.get('vector_score', 0.0),  # 向量模型原始分数
                bm25_score=doc.get('bm25_score', doc.get('score', 0.0)),  # BM25原始分数（别名）
                has_vector=doc.get('has_vector', True),  # 是否使用了向量检索
                rerank_score=round(rerank_score, 4)
            )
            source_docs.append(source_doc)
            
            # 构建上下文字符串
            context_docs.append(
                f"【文档: {doc.get('question', '未知标题')}】\n"
                f"{doc.get('answer', '')}"
            )
        
        # 组合上下文
        context = "\n\n".join(context_docs)
        
        # 调用LLM生成答案（context和query分开传）
        answer = llm.generate(context=context, query=request.question)
        
        llm_ms = (time.perf_counter() - llm_start) * 1000
        
        # 计算总耗时
        total_ms = (time.perf_counter() - total_start) * 1000
        
        # 如果答案包含"无法回答"或"抱歉"，清空引用文档
        if "无法回答" in answer or "抱歉" in answer:
            source_docs = []
        
        # ========== 组装响应 ==========
        return AskResponse(
            question=request.question,
            answer=answer,
            sources=source_docs,
            latency=LatencyStats(
                total_ms=round(total_ms, 2),
                retrieval_ms=round(retrieval_ms, 2),
                rerank_ms=round(rerank_ms, 2),
                llm_ms=round(llm_ms, 2)
            ),
            retrieved_count=retrieved_count,
            reranked_count=reranked_count,
            status="success"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        # 计算已消耗的耗时
        total_ms = (time.perf_counter() - total_start) * 1000
        
        # 返回错误响应
        raise HTTPException(
            status_code=500,
            detail={
                "error": "Internal Server Error",
                "message": "处理请求时发生错误",
                "detail": str(e),
                "latency_ms": round(total_ms, 2)
            }
        )


# ============================================================================
# 流式问答接口（SSE）
# ----------------------------------------------------------------------------
# 设计要点：
# 1. 复用原 /api/ask 的检索+重排逻辑，仅把 LLM 生成阶段改为流式推送。
# 2. 协议：Server-Sent Events，按 config.py 里约定的事件名分发：
#    - event: token   data: {"text": "..."}          单字/词增量
#    - event: sources data: {"sources": [...]}        所有引用与分块信息（生成结束前推一次）
#    - event: latency data: {"retrieval_ms":..., ...} 各阶段耗时（结束前推一次）
#    - event: done    data: {"status":"success"}      结束标记
#    - event: error   data: {"message":"..."}         异常事件
# 3. 兼容性：原 POST /api/ask 非流式接口不动，前端可继续调用。
# ============================================================================


def _format_sse(event: str, data: dict) -> str:
    """
    将事件 + 数据格式化为 SSE 规范的报文。

    SSE 规范：
        event: <事件名>\n
        data: <一行 JSON>\n
        \n               ← 空行作为单条消息结束标志
    """
    # ensure_ascii=False 让中文 token 不被转义成 \u
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


@router.post("/api/ask/stream")
async def ask_stream_endpoint(
    request: AskRequest,
    retriever=Depends(get_retriever),
    reranker=Depends(get_reranker),
    llm=Depends(get_llm)
):
    """
    问答接口（流式 SSE 版）

    与 /api/ask 行为一致，但 LLM 阶段改为按 Token 流式推送。
    前端可使用 fetch + ReadableStream 或 EventSource 解析事件。

    返回事件序列（按时间顺序）：
        token (多次) → sources → latency → done
    任何阶段异常都会推 error 事件后接 done 事件。
    """

    # 流式开关：未启用时直接 404，避免出现协议不一致
    if not config.LLM_STREAM_ENABLED:
        raise HTTPException(status_code=404, detail="流式接口未启用")

    total_start = time.perf_counter()

    # ---------- 阶段1+阶段2：检索+重排（同步执行，与原接口一致）----------
    # 这里不能放进 generator 里 yield 前再算，否则首字延迟会被打破。
    # 一旦失败，直接走 SSE error 事件，前端可优雅降级。
    try:
        retrieval_start = time.perf_counter()
        hybrid_results = retriever.search(
            query=request.question,
            top_k=request.top_k,
            index_name=request.index_name
        )
        retrieval_ms = (time.perf_counter() - retrieval_start) * 1000
        retrieved_count = len(hybrid_results)
    except Exception as e:
        logger.error(f"流式问答-检索阶段异常: {e}", exc_info=True)

        async def _err_gen():
            yield _format_sse(config.SSE_EVENT_ERROR, {"message": f"检索失败: {str(e)}"})
            yield _format_sse(config.SSE_EVENT_DONE, {"status": "error"})

        return StreamingResponse(_err_gen(), media_type="text/event-stream; charset=utf-8")

    # 检索为空：仍按 SSE 推一段固定提示，保证前端处理路径统一
    if not hybrid_results:
        empty_text = (
            "抱歉，知识库中没有找到与您问题相关的内容。\n\n"
            "可能原因：\n1. 您的问题比较宽泛，建议尝试更具体的术语\n"
            "2. 当前知识库覆盖范围有限\n\n您可以尝试换个方式提问。"
        )

        async def _empty_gen():
            # 把固定文本按 1 个事件推完即可，不需要逐字
            yield _format_sse(config.SSE_EVENT_TOKEN, {"text": empty_text})
            yield _format_sse(config.SSE_EVENT_SOURCES, {"sources": []})
            yield _format_sse(config.SSE_EVENT_LATENCY, {
                "total_ms": round((time.perf_counter() - total_start) * 1000, 2),
                "retrieval_ms": round(retrieval_ms, 2),
                "rerank_ms": 0.0,
                "llm_ms": 0.0,
            })
            yield _format_sse(config.SSE_EVENT_DONE, {
                "status": "success",
                "retrieved_count": 0,
                "reranked_count": 0,
            })

        return StreamingResponse(_empty_gen(), media_type="text/event-stream; charset=utf-8")

    # ---------- 重排 ----------
    rerank_start = time.perf_counter()
    try:
        documents = [doc.get('answer', '') for doc in hybrid_results]
        rerank_results = reranker.rerank(request.question, documents)

        rerank_scores = [0.0] * len(hybrid_results)
        for item in rerank_results:
            idx = item['index']
            if idx < len(rerank_scores):
                rerank_scores[idx] = item['score']

        scored_docs = list(zip(hybrid_results, rerank_scores))
        scored_docs.sort(key=lambda x: x[1], reverse=True)
        # 与非流式接口保持一致：强制最多 3 条
        top_k_docs = scored_docs[: request.rerank_top_k][:3]
    except Exception as e:
        # 重排服务不可用，降级到不重排（与原接口一致）
        logger.warning(f"流式问答-重排服务不可用，降级到不重排: {e}")
        sorted_results = sorted(
            hybrid_results,
            key=lambda x: x.get('score', x.get('_score', 0)),
            reverse=True,
        )
        top_k_docs = [(doc, 0.0) for doc in sorted_results[:3]]

    rerank_ms = (time.perf_counter() - rerank_start) * 1000
    reranked_count = len(top_k_docs)

    # 准备上下文 & 源文档（先构造好，便于流结束时一次性下发 sources）
    context_docs = []
    source_docs = []
    for doc, rerank_score in top_k_docs:
        source_doc = SourceDoc(
            doc_id=doc.get('doc_id', ''),
            title=doc.get('question', ''),
            content=doc.get('answer', '')[:500],
            source_file=doc.get('category', ''),
            es_score=doc.get('bm25_score', doc.get('score', 0.0)),
            vector_score=doc.get('vector_score', 0.0),
            bm25_score=doc.get('bm25_score', doc.get('score', 0.0)),
            has_vector=doc.get('has_vector', True),
            rerank_score=round(rerank_score, 4),
        )
        source_docs.append(source_doc)
        context_docs.append(
            f"【文档: {doc.get('question', '未知标题')}】\n{doc.get('answer', '')}"
        )
    context = "\n\n".join(context_docs)

    # ---------- 阶段3：流式 LLM 生成 ----------
    # 注意：llm.generate_stream 是同步生成器，我们在 async generator 里用 to_thread
    # 逐个 token 拉出来，避免阻塞事件循环（FastAPI 是 asyncio 驱动）。
    async def event_generator():
        llm_start = time.perf_counter()
        produced_any = False  # 是否成功输出过 token，用于异常判断
        full_answer_parts: List[str] = []

        try:
            # 取出同步生成器
            sync_gen = llm.generate_stream(context=context, query=request.question)

            # 把同步 next() 包成线程调用，避免阻塞
            loop = asyncio.get_event_loop()
            sentinel = object()

            while True:
                token = await loop.run_in_executor(
                    None,
                    lambda: next(sync_gen, sentinel)
                )
                if token is sentinel:
                    break
                if not token:
                    continue
                produced_any = True
                full_answer_parts.append(token)
                # 推一个 token 事件给前端
                yield _format_sse(config.SSE_EVENT_TOKEN, {"text": token})

        except Exception as e:
            # 兜底：把异常翻译为 error 事件，不让连接以裸异常关闭
            logger.error(f"流式问答-LLM 阶段异常: {e}", exc_info=True)
            yield _format_sse(config.SSE_EVENT_ERROR, {"message": f"生成失败: {str(e)}"})

        llm_ms = (time.perf_counter() - llm_start) * 1000
        total_ms = (time.perf_counter() - total_start) * 1000

        # 判断是否需要清空引用（与非流式逻辑保持一致）
        full_answer = "".join(full_answer_parts)
        if ("无法回答" in full_answer) or ("抱歉" in full_answer):
            final_sources = []
        else:
            final_sources = [s.model_dump() for s in source_docs]

        # 生成结束后，推 sources / latency / done 三个收尾事件
        yield _format_sse(config.SSE_EVENT_SOURCES, {"sources": final_sources})
        yield _format_sse(config.SSE_EVENT_LATENCY, {
            "total_ms": round(total_ms, 2),
            "retrieval_ms": round(retrieval_ms, 2),
            "rerank_ms": round(rerank_ms, 2),
            "llm_ms": round(llm_ms, 2),
        })
        yield _format_sse(config.SSE_EVENT_DONE, {
            "status": "success" if produced_any else "empty",
            "retrieved_count": retrieved_count,
            "reranked_count": reranked_count,
        })

    # 关键响应头：
    # - Cache-Control: no-cache  → 避免被中间代理缓存
    # - X-Accel-Buffering: no    → 关闭 nginx 缓冲，保证逐 token 直达浏览器
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
