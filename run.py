#!/usr/bin/env python3
"""
API Service 启动脚本
"""

import os
import sys
from pathlib import Path

# 确保项目根目录在 Python 路径中
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# 加载环境变量
from dotenv import load_dotenv
load_dotenv()

from app import __version__ as APP_VERSION


def main():
    """启动 API 服务器"""
    import uvicorn
    
    # 从环境变量获取配置
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    debug = os.getenv("DEBUG", "false").lower() == "true"
    workers = max(1, int(os.getenv("UVICORN_WORKERS", "1")))
    backlog = max(128, int(os.getenv("UVICORN_BACKLOG", "2048")))
    timeout_keep_alive = max(1, int(os.getenv("UVICORN_TIMEOUT_KEEP_ALIVE", "10")))
    display_workers = 1 if debug else workers

    print(f"""
╔═══════════════════════════════════════════════════════════════╗
║                      API Service v{APP_VERSION:<5}                       ║
╠═══════════════════════════════════════════════════════════════╣
║  Server: http://{host}:{port:<5}                                ║
║  Debug: {'ON ' if debug else 'OFF'}                                               ║
║  Workers: {display_workers:<3}                                               ║
╚═══════════════════════════════════════════════════════════════╝
    """)

    uvicorn_kwargs = {
        "app": "app.main:app",
        "host": host,
        "port": port,
        "reload": debug,
        "log_level": "debug" if debug else "info",
        "backlog": backlog,
        "timeout_keep_alive": timeout_keep_alive,
    }
    if not debug and workers > 1:
        uvicorn_kwargs["workers"] = workers

    # 启动服务器
    uvicorn.run(**uvicorn_kwargs)


if __name__ == "__main__":
    main()
