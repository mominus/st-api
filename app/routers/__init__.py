"""
API Routers
包含 OpenAI、Anthropic、Gemini 兼容路由和管理 API 路由
"""

from app.routers.openai import router as openai_router
from app.routers.anthropic import router as anthropic_router
from app.routers.gemini import router as gemini_router
from app.routers.admin import router as admin_router

__all__ = ["openai_router", "anthropic_router", "gemini_router", "admin_router"]
