#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LocalRAG-CS API 主程序
FastAPI 应用入口，整合所有路由和中间件

启动命令:
    uvicorn api.app:app --host 0.0.0.0 --port 8000 --reload

访问地址:
    - API文档: http://localhost:8000/docs
    - 后台界面: http://localhost:8000/
    - 健康检查: http://localhost:8000/api/health
"""

import os
import sys
import time
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

# 配置日志（在导入其他模块之前）
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# 添加项目根目录到Python路径
# 确保可以导入 es_client, embedding 等模块
PROJECT_ROOT = Path(__file__).parent.parent.absolute()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
    logger.info(f"添加项目根目录到Python路径: {PROJECT_ROOT}")

# FastAPI 相关导入
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

# 导入依赖管理
from dependencies import initialize_clients

# 导入路由
from routes import health, ask, cache as cache_route, session as session_route, export as export_route, chunks as chunks_route, public as public_route
from api import ingest_router
from api import agent_routes


# ============== 应用生命周期管理 ==============

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI 应用生命周期管理
    
    startup: 初始化所有客户端连接
    shutdown: 清理资源
    """
    # ===== Startup =====
    logger.info("=" * 60)
    logger.info("🚀 FastAPI 应用启动中...")
    logger.info("=" * 60)
    
    start_time = time.time()
    
    try:
        # 初始化所有客户端
        initialize_clients()
        
        init_time = time.time() - start_time
        logger.info("=" * 60)
        logger.info(f"✅ 应用启动完成 (耗时: {init_time:.2f}s)")
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error("=" * 60)
        logger.error(f"❌ 应用启动失败: {e}")
        logger.error("=" * 60)
        # 即使启动失败也继续运行，让健康检查接口报告具体错误
    
    yield
    
    # ===== Shutdown =====
    logger.info("=" * 60)
    logger.info("🛑 FastAPI 应用关闭中...")
    logger.info("=" * 60)
    
    # 清理资源（如果需要）
    # TODO: 关闭ES连接、释放模型资源等
    
    logger.info("✅ 应用关闭完成")


# ============== FastAPI 应用实例 ==============

# 创建 FastAPI 应用实例
app = FastAPI(
    title="LocalRAG-CS API",
    description="""
    LocalRAG-CS 客服知识库问答系统 API
    
    提供基于混合检索（ES + 向量）+ Rerank + LLM 的智能问答能力
    
    ## 主要功能
    - **/api/ask**: 问答接口，执行完整RAG链路
    - **/api/health**: 健康检查，查看各服务状态
    - **/api/ingest/**: 数据导入接口，支持文件上传和文本导入
    - **/**: 后台管理界面（Web UI）
    """,
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan
)

# ============== 中间件配置 ==============

# CORS 中间件（允许跨域请求）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应该限制为特定域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# GZip 压缩中间件（响应内容 > 1000 字节时自动压缩）
app.add_middleware(GZipMiddleware, minimum_size=1000)


# ============== 请求日志中间件 ==============

@app.middleware("http")
async def log_requests(request: Request, call_next):
    """
    请求日志中间件
    记录每个请求的详细信息，便于调试和监控
    """
    start_time = time.time()
    
    # 生成请求ID（简化版，生产环境可用uuid）
    request_id = f"{int(start_time * 1000):x}"
    
    # 记录请求开始
    logger.info(f"[{request_id}] → {request.method} {request.url.path}")
    
    # 处理请求
    try:
        response = await call_next(request)
        
        # 计算处理时间
        process_time = (time.time() - start_time) * 1000
        
        # 记录响应
        logger.info(
            f"[{request_id}] ← {response.status_code} "
            f"({process_time:.2f}ms)"
        )
        
        # 添加响应头
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time"] = str(round(process_time, 2))
        
        return response
        
    except Exception as e:
        # 记录错误
        process_time = (time.time() - start_time) * 1000
        logger.error(
            f"[{request_id}] ✗ ERROR: {str(e)} "
            f"({process_time:.2f}ms)"
        )
        raise


# ============== 模板配置 ==============

# 配置 Jinja2 模板引擎
# 注意：模板文件放在项目根目录 templates/ 目录下
templates = Jinja2Templates(
    directory=str(Path(__file__).parent.parent / "templates")
)


# ============== 路由注册 ==============

# 包含 health 路由
app.include_router(health.router)

# 包含 ask 路由
app.include_router(ask.router)

# 包含 cache 管理路由（/api/cache/stats /api/cache/flush）
app.include_router(cache_route.router)

# 包含数据导入路由（/api/ingest/upload /api/ingest/text 等）
app.include_router(ingest_router.router)

# 包含会话管理路由（/api/session/new /api/sessions 等）
app.include_router(session_route.router)

# 包含导出路由（/api/export/pdf /api/export/docx）
app.include_router(export_route.router)

# 包含 chunk 管理路由（/api/chunks）
app.include_router(chunks_route.router)

# 包含公网暴露路由（/api/public/health）
app.include_router(public_route.router)

# 包含 Agent 路由（/api/agent/ask）
app.include_router(agent_routes.router)


# ============== 页面路由 ==============

@app.get("/", response_class=HTMLResponse)
@app.get("/ingest", response_class=HTMLResponse)
@app.get("/chunks", response_class=HTMLResponse)
@app.get("/agent", response_class=HTMLResponse)
async def admin_page(request: Request):
    """
    后台管理首页 / 知识库管理页面 / Chunk 管理页面 / Agent 对话页面
    返回对应页面的 HTML
    """
    page_map = {
        "/ingest": "ingest.html",
        "/chunks": "chunks.html",
        "/agent": "agent.html",
    }
    page_file = page_map.get(request.url.path, "index.html")
    return templates.TemplateResponse(
        request=request,
        name=page_file,
        context={
            "api_base_url": "/api",
            "cache_buster": int(time.time()),
        }
    )


@app.get("/agent-v{version}")
async def agent_page_v(request: Request, version: str):
    """带版本号的 Agent 页面，绕过浏览器缓存"""
    return templates.TemplateResponse(
        request=request,
        name="agent.html",
        context={
            "api_base_url": "/api",
            "cache_buster": int(time.time()),
        }
    )


@app.get("/ping")
async def ping():
    """
    简单的Ping接口，用于快速检查服务是否存活
    返回公网地址信息，方便外网访问时确认
    """
    from config import PUBLIC_URL
    return {
        "status": "pong",
        "timestamp": datetime.now().isoformat(),
        "service": "localrag-cs-api",
        "public_url": PUBLIC_URL
    }


# ============== 错误处理 ==============

@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    """404 错误处理"""
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=404,
            content={
                "error": "Not Found",
                "message": f"接口 {request.url.path} 不存在",
                "path": request.url.path
            }
        )
    # 非API路由返回HTML 404页面
    return HTMLResponse(
        content="""
        <!DOCTYPE html>
        <html>
        <head><title>404 - 页面未找到</title></head>
        <body>
            <h1>404 - 页面未找到</h1>
            <p>您访问的页面不存在。</p>
            <p><a href="/">返回首页</a></p>
        </body>
        </html>
        """,
        status_code=404
    )


@app.exception_handler(500)
async def internal_error_handler(request: Request, exc):
    """500 错误处理"""
    logger.error(f"500错误: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal Server Error",
            "message": "服务器内部错误，请稍后重试"
        }
    )


# ============== 主程序入口 ==============

if __name__ == "__main__":
    import uvicorn
    
    # 从环境变量获取配置
    host = os.environ.get("API_HOST", "0.0.0.0")
    port = int(os.environ.get("API_PORT", 8000))
    reload = os.environ.get("API_RELOAD", "false").lower() == "true"
    
    logger.info("=" * 60)
    logger.info("🚀 启动 LocalRAG-CS API 服务")
    logger.info(f"   地址: http://{host}:{port}")
    logger.info(f"   文档: http://{host}:{port}/docs")
    logger.info(f"   热重载: {reload}")
    logger.info("=" * 60)
    
    uvicorn.run(
        "api.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info"
    )
