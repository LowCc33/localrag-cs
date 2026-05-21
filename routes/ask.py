#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
问答接口路由
提供 POST /api/ask 接口，执行完整的RAG链路：
混合检索 -> 重排 -> LLM生成
"""

import time
import logging
from typing import List
from fastapi import APIRouter, HTTPException, Depends

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
