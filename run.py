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


def main():
    """启动 API 服务器"""
    import uvicorn
    
    # 从环境变量获取配置
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    debug = os.getenv("DEBUG", "false").lower() == "true"
    
    print(f"""
╔═══════════════════════════════════════════════════════════════╗
║                      API Service v1.0.0                       ║
╠═══════════════════════════════════════════════════════════════╣
║  Server: http://{host}:{port:<5}                                ║
║  Debug: {'ON ' if debug else 'OFF'}                                               ║
╚═══════════════════════════════════════════════════════════════╝
    """)
    
    # 启动服务器
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=debug,
        log_level="debug" if debug else "info"
    )


if __name__ == "__main__":
    main()
