#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公网暴露模块路由
提供公网健康检查、ngrok 状态查询等接口
所有接口不影响本地 localhost 访问
"""

import time
import subprocess
from datetime import datetime

import urllib3
import requests
from fastapi import APIRouter

from config import PUBLIC_URL, NGROK_ADMIN_URL

# 禁用 SSL 警告（ngrok 免费版使用自签名证书）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

router = APIRouter(tags=["public"])


def _check_ngrok_process() -> dict:
    """
    检查 ngrok 进程状态
    
    Returns:
        包含进程状态信息的字典
    """
    result = {
        "running": False,
        "pid": None,
        "error": None
    }
    
    try:
        # 通过 pgrep 查找 ngrok 进程
        proc = subprocess.run(
            ["pgrep", "-f", "ngrok"],
            capture_output=True, text=True, timeout=5
        )
        if proc.returncode == 0 and proc.stdout.strip():
            pids = proc.stdout.strip().split()
            result["running"] = True
            result["pid"] = pids[0]
        else:
            result["error"] = "ngrok 进程未运行"
    except Exception as e:
        result["error"] = f"检查 ngrok 进程失败: {str(e)}"
    
    return result


def _check_ngrok_tunnel() -> dict:
    """
    检查 ngrok 隧道状态
    
    Returns:
        包含隧道状态信息的字典
    """
    result = {
        "connected": False,
        "public_url": None,
        "error": None
    }
    
    try:
        resp = requests.get(
            f"{NGROK_ADMIN_URL}/api/tunnels",
            timeout=5, verify=False  # nosec B501: ngrok免费版使用自签名证书，需要跳过验证
        )
        if resp.status_code == 200:
            data = resp.json()
            tunnels = data.get("tunnels", [])
            if tunnels:
                result["connected"] = True
                result["public_url"] = tunnels[0].get("public_url")
            else:
                result["error"] = "没有活跃的隧道"
        else:
            result["error"] = f"ngrok API 返回状态码 {resp.status_code}"
    except requests.exceptions.ConnectionError:
        result["error"] = "无法连接 ngrok 管理面板"
    except Exception as e:
        result["error"] = f"检查隧道失败: {str(e)}"
    
    return result


def _check_local_api() -> dict:
    """
    检查本地 API 服务状态
    
    Returns:
        包含 API 状态信息的字典
    """
    result = {
        "running": False,
        "latency_ms": None,
        "error": None
    }
    
    try:
        start = time.perf_counter()
        resp = requests.get(
            "http://localhost:8000/ping",
            timeout=5
        )
        latency = (time.perf_counter() - start) * 1000
        if resp.status_code == 200:
            result["running"] = True
            result["latency_ms"] = round(latency, 2)
        else:
            result["error"] = f"API 返回状态码 {resp.status_code}"
    except requests.exceptions.ConnectionError:
        result["error"] = "无法连接本地 API 服务"
    except Exception as e:
        result["error"] = f"检查 API 失败: {str(e)}"
    
    return result


def _check_public_access() -> dict:
    """
    检查公网地址是否可达
    
    Returns:
        包含公网可达性信息的字典
    """
    result = {
        "reachable": False,
        "latency_ms": None,
        "error": None
    }
    
    try:
        start = time.perf_counter()
        resp = requests.get(
            f"{PUBLIC_URL}/ping",
            timeout=10, verify=False  # nosec B501: ngrok免费版使用自签名证书，需要跳过验证
        )
        latency = (time.perf_counter() - start) * 1000
        if resp.status_code == 200:
            result["reachable"] = True
            result["latency_ms"] = round(latency, 2)
        else:
            result["error"] = f"公网地址返回状态码 {resp.status_code}"
    except requests.exceptions.ConnectionError:
        result["error"] = "无法连接公网地址（ngrok 可能未运行）"
    except requests.exceptions.Timeout:
        result["error"] = "公网地址连接超时"
    except Exception as e:
        result["error"] = f"检查公网地址失败: {str(e)}"
    
    return result


@router.get("/api/public/health")
async def public_health():
    """
    公网健康检查接口
    
    检查：
    - ngrok 进程是否在运行
    - ngrok 隧道是否已建立
    - 本地 API 服务是否正常
    - 公网地址是否可达
    
    Returns:
        详细的公网暴露状态信息
    """
    ngrok_process = _check_ngrok_process()
    ngrok_tunnel = _check_ngrok_tunnel()
    local_api = _check_local_api()
    public_access = _check_public_access()
    
    # 综合判断整体状态
    all_ok = (
        ngrok_process["running"]
        and ngrok_tunnel["connected"]
        and local_api["running"]
    )
    
    return {
        "status": "healthy" if all_ok else "degraded",
        "timestamp": datetime.now().isoformat(),
        "public_url": ngrok_tunnel.get("public_url") or PUBLIC_URL,
        "services": {
            "ngrok_process": {
                "status": "ok" if ngrok_process["running"] else "error",
                "pid": ngrok_process["pid"],
                "error": ngrok_process["error"]
            },
            "ngrok_tunnel": {
                "status": "ok" if ngrok_tunnel["connected"] else "error",
                "public_url": ngrok_tunnel["public_url"],
                "error": ngrok_tunnel["error"]
            },
            "local_api": {
                "status": "ok" if local_api["running"] else "error",
                "latency_ms": local_api["latency_ms"],
                "error": local_api["error"]
            },
            "public_access": {
                "status": "ok" if public_access["reachable"] else "error",
                "latency_ms": public_access["latency_ms"],
                "error": public_access["error"]
            }
        }
    }
