"""分级稽核结果表接口。

所有接口都解析租户上下文，归属与权限拒绝返回 403，状态机/边界冲突返回 409。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.schemas import ActionResult, ActionPayload, PageResult
from app.services.audit_result import AuditResultService
from app.tenancy import (
    PermissionDeniedError,
    RequestContext,
    StateConflictError,
    get_context,
    tenant_catalog,
)

router = APIRouter(prefix="/api/audit_result", tags=["分级稽核结果表"])

service = AuditResultService()


def _raise_http(error: Exception) -> None:
    """把领域异常映射成 HTTP 状态码。"""
    from fastapi import HTTPException
    if isinstance(error, PermissionDeniedError):
        raise HTTPException(status_code=403, detail={"message": error.message, "reason": error.reason})
    if isinstance(error, StateConflictError):
        status_code = 404 if error.reason == "not_found" else 409
        raise HTTPException(status_code=status_code,
                            detail={"message": error.message, "reason": error.reason})
    raise error


@router.get("/catalog")
def catalog() -> dict:
    """租户、角色与权限目录，供前端渲染切换器。"""
    return tenant_catalog()


@router.get("/context")
def context(ctx: RequestContext = Depends(get_context)) -> dict:
    """回显当前调用方身份与权限点，前端据此控制按钮可用性。"""
    return ctx.to_dict()


@router.get("", response_model=PageResult[dict])
def list_results(
    status: str | None = Query(default=None, description="待复核、复核中、待发布、已发布"),
    keyword: str | None = Query(default=None, description="按结果编号检索"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
    ctx: RequestContext = Depends(get_context),
) -> PageResult[dict]:
    """按归属和权限过滤结果表：本租户明细 + 权限内的匿名汇总。"""
    try:
        items, total = service.list_results(ctx, status=status, keyword=keyword, page=page, size=size)
    except (PermissionDeniedError, StateConflictError) as error:
        _raise_http(error)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{result_id}")
def get_result(result_id: int, ctx: RequestContext = Depends(get_context)) -> dict:
    """复核单条结果表；越权访问他租户明细返回 403。"""
    try:
        return service.get_result(result_id, ctx)
    except (PermissionDeniedError, StateConflictError) as error:
        _raise_http(error)


@router.post("", response_model=ActionResult)
def create_batch(
    payload: dict | None = None,
    ctx: RequestContext = Depends(get_context),
) -> ActionResult:
    """打开质量稽核入口并提交：按租户归属抽样稽核，生成分级结果表。"""
    payload = payload or {}
    try:
        entry = service.create_audit_batch(
            ctx,
            batch_no=payload.get("batch_no"),
            sample_ratio=payload.get("sample_ratio"),
            rule_ids=payload.get("rule_ids"),
            include_cross_summary=bool(payload.get("include_cross_summary", True)),
        )
    except (PermissionDeniedError, StateConflictError) as error:
        _raise_http(error)
    conflict = entry.get("conflict")
    message = "分级稽核结果表已生成"
    if conflict:
        message = f"分级稽核结果表已生成，但存在待裁决冲突：{conflict.get('message')}"
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{result_id}/actions", response_model=ActionResult)
def run_action(
    result_id: int,
    payload: ActionPayload,
    ctx: RequestContext = Depends(get_context),
) -> ActionResult:
    """状态单向推进：开始复核、复核通过、确认发布；跳级与越权一律拦截。"""
    try:
        entry = service.run_action(result_id, payload.action.strip(), ctx)
    except (PermissionDeniedError, StateConflictError) as error:
        _raise_http(error)
    return ActionResult(ok=True, message=f"分级稽核结果表已{payload.action.strip()}", entry=entry)
