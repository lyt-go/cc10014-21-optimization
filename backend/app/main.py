"""数仓分层治理平台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.routers import ROUTERS
from app.store import store
from app.tenancy import PermissionDeniedError, StateConflictError

app = FastAPI(title="数仓分层治理平台", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(PermissionDeniedError)
def handle_permission_denied(_: Request, exc: PermissionDeniedError) -> JSONResponse:
    """归属/权限拒绝统一 403，保证越权操作在依赖层和服务层都被拦截。"""
    return JSONResponse(status_code=403,
                        content={"detail": {"message": exc.message, "reason": exc.reason}})


@app.exception_handler(StateConflictError)
def handle_state_conflict(_: Request, exc: StateConflictError) -> JSONResponse:
    """状态机跳级、发布边界、待裁决冲突统一 409（not_found 场景为 404）。"""
    status_code = 404 if exc.reason == "not_found" else 409
    return JSONResponse(status_code=status_code,
                        content={"detail": {"message": exc.message, "reason": exc.reason}})


for module in ROUTERS:
    app.include_router(module.router)


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪。"""
    return {"ok": True, "app": settings.app_name, "modules": len(store.module_names())}


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()
