"""
API Service 主应用入口

- 注册所有路由
- 配置 CORS
- 静态文件服务
- 启动时数据库初始化
"""

# 首先加载环境变量
from dotenv import load_dotenv
load_dotenv()

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path
import os
import secrets

from app.routers import openai_router, anthropic_router, gemini_router, admin_router
from app.models.database import init_database, close_database
from app.services.auth import get_auth_service
from app import __version__ as APP_VERSION
import logging

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
# 降低第三方 HTTP 客户端日志噪音
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# 从环境变量获取隐藏的后台路径，默认生成随机路径
# 在 .env 中设置: ADMIN_PATH=your-secret-path
# 访问后台: https://your-domain.com/your-secret-path
ADMIN_PATH = os.getenv("ADMIN_PATH", secrets.token_urlsafe(16))

# Worker 代理共享密钥（可选）
# 作用：当启用时，仅允许携带正确 x-proxy-secret 请求头的代理流量访问 API，
# 防止外部直接绕过 Cloudflare Worker 访问源站。
PROXY_SHARED_SECRET = os.getenv("PROXY_SHARED_SECRET")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理 - 启动时初始化数据库，关闭时清理资源"""
    # 启动时：初始化数据库
    await init_database()
    
    # 确保默认管理员存在
    auth_service = get_auth_service()
    await auth_service.ensure_default_admin()
    
    # 打印后台访问路径（仅在启动时显示一次）
    print(f"\n{'='*50}")
    print(f"Admin panel path: /{ADMIN_PATH}")
    print(f"{'='*50}\n")
    
    yield
    # 关闭时：清理数据库连接
    await close_database()


# 创建 FastAPI 应用实例
# 禁用公开的 API 文档，避免暴露系统信息
app = FastAPI(
    title="API Service",
    description="API Service",
    version=APP_VERSION,
    docs_url=None,  # 禁用 Swagger UI
    redoc_url=None,  # 禁用 ReDoc
    openapi_url=None,  # 禁用 OpenAPI schema
    lifespan=lifespan
)

# 配置 CORS（在注册路由之前配置）
cors_origins = os.getenv("CORS_ORIGINS", "*")
if cors_origins == "*":
    origins = ["*"]
else:
    origins = [origin.strip() for origin in cors_origins.split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Worker 共享密钥校验中间件
@app.middleware("http")
async def verify_worker_proxy_secret(request: Request, call_next):
    """校验 Worker 代理共享密钥，限制 API 仅可通过受信任代理访问。"""
    if PROXY_SHARED_SECRET:
        path = request.url.path
        is_protected_api = (
            path == "/v1" or
            path.startswith("/v1/") or
            path == "/v1beta" or
            path.startswith("/v1beta/")
        )

        if is_protected_api:
            provided = request.headers.get("x-proxy-secret", "")
            if provided != PROXY_SHARED_SECRET:
                return JSONResponse(
                    status_code=403,
                    content={"error": {"message": "Forbidden", "type": "permission_error"}}
                )

    return await call_next(request)


# 请求日志中间件（带性能统计）
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """记录所有请求的路径和方法，并统计性能数据"""
    import time
    from app.services.connection_pool import get_concurrency_limiter
    
    path = request.url.path
    # 仅记录 API 请求，降低静态资源和页面请求日志噪音
    if (
        path.startswith("/api/") or
        path.startswith("/v1") or
        path.startswith("/anthropic/") or
        path.startswith("/gemini/")
    ):
        logger.info(f"[REQUEST] {request.method} {path}")
    
    # 只统计 API 请求（排除静态文件和管理后台）
    should_track = (
        path.startswith("/v1/") or 
        path.startswith("/anthropic/") or 
        path.startswith("/gemini/")
    )
    
    if should_track:
        limiter = get_concurrency_limiter()
        start_time = time.time()
        try:
            async with limiter.acquire_request():
                response = await call_next(request)
                # 记录响应时间
                elapsed = (time.time() - start_time) * 1000  # 毫秒
                limiter.record_response_time(elapsed)
                return response
        except Exception as e:
            # 请求被拒绝或出错
            logger.warning(f"Request rejected or failed: {e}")
            raise
    else:
        response = await call_next(request)
        return response

# 注册 OpenAI 兼容路由
app.include_router(openai_router)

# 注册 Anthropic 兼容路由
app.include_router(anthropic_router)

# 注册 Gemini 兼容路由
app.include_router(gemini_router)

# 注册管理 API 路由
app.include_router(admin_router)

# 静态文件路径
static_path = Path(__file__).parent / "static"


@app.get("/")
async def root():
    """根路径 - 只返回服务状态，不暴露任何系统信息"""
    return {"status": "ok"}


@app.get("/health")
async def health_check():
    """健康检查端点 - 只返回服务状态"""
    return {"status": "ok"}


# 使用动态路由处理隐藏的后台路径
@app.get(f"/{ADMIN_PATH}", response_class=FileResponse)
async def admin_page():
    """管理后台页面 - 隐藏路径"""
    admin_file = static_path / "admin.html"
    if admin_file.exists():
        return FileResponse(str(admin_file), media_type="text/html")
    return JSONResponse({"status": "ok"}, status_code=200)


# 静态文件服务（放在最后，避免覆盖其他路由）
# 注意：静态文件仍然可以通过 /static/ 访问，但不会暴露后台入口
if static_path.exists():
    app.mount("/static", StaticFiles(directory=str(static_path)), name="static")
