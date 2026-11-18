"""多租户上下文与权限矩阵。

租户归属与权限全部在这一处收口：
- 调用方身份由请求头 ``X-Tenant-Id`` / ``X-Tenant-Role`` 携带，默认进入订单租户管理员视角；
- 角色权限采用白名单矩阵，业务服务只判断 ``can(权限点)``，不自行解释角色；
- ``SHARED`` 是共享域，共享域内容对所有租户只读。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import Header

# 共享域标识：共享数据不属于任何业务租户，仅可只读查看
SHARED_TENANT = "SHARED"

# 权限点：稽核结果表的每个写动作都对应一个显式权限
PERM_READ = "audit:read"
PERM_SUBMIT = "audit:submit"
PERM_REVIEW = "audit:review"
PERM_PUBLISH = "audit:publish"
PERM_CROSS_SUMMARY = "cross:summary"

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "稽核管理员": {PERM_READ, PERM_SUBMIT, PERM_REVIEW, PERM_PUBLISH, PERM_CROSS_SUMMARY},
    "稽核员": {PERM_READ, PERM_SUBMIT, PERM_REVIEW},
    "查看员": {PERM_READ},
}

# HTTP 请求头只能承载 ASCII，角色头传角色码，服务端统一映射到中文角色名
ROLE_CODES: dict[str, str] = {
    "admin": "稽核管理员",
    "auditor": "稽核员",
    "viewer": "查看员",
}

# 租户目录：id -> 展示信息
TENANTS: dict[str, dict[str, str]] = {
    "t_order": {"name": "订单租户", "system": "订单系统"},
    "t_payment": {"name": "支付租户", "system": "支付系统"},
    "t_user": {"name": "用户租户", "system": "用户系统"},
    SHARED_TENANT: {"name": "共享域", "system": "跨租户共享"},
}

DEFAULT_TENANT = "t_order"
DEFAULT_ROLE = "稽核管理员"


class PermissionDeniedError(Exception):
    """归属或权限校验失败，路由层统一映射为 403。"""

    def __init__(self, message: str, *, reason: str = "forbidden") -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason


class StateConflictError(Exception):
    """状态机单向推进冲突或发布边界冲突，路由层统一映射为 409。"""

    def __init__(self, message: str, *, reason: str = "conflict") -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason


@dataclass
class RequestContext:
    """一次调用的租户身份快照。"""

    tenant_id: str = DEFAULT_TENANT
    role: str = DEFAULT_ROLE
    permissions: set[str] = field(default_factory=lambda: set(ROLE_PERMISSIONS[DEFAULT_ROLE]))

    @property
    def tenant_name(self) -> str:
        return TENANTS.get(self.tenant_id, {}).get("name", self.tenant_id)

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    def effective_tenants(self) -> list[str]:
        """本调用可见的归属范围：本租户 + 共享域。"""
        return [self.tenant_id, SHARED_TENANT]

    def owns(self, owner_tenant: str | None) -> bool:
        """归属判断：无归属的历史数据走兼容路径，共享域不归任何业务租户所有。"""
        if owner_tenant is None:
            return True
        return owner_tenant == self.tenant_id

    def can_access_owner(self, owner_tenant: str | None) -> bool:
        """可读范围：本租户归属 + 共享域只读。"""
        if owner_tenant is None:
            return True
        return owner_tenant in self.effective_tenants()

    def require(self, permission: str) -> None:
        if not self.can(permission):
            raise PermissionDeniedError(
                f"租户「{self.tenant_name}」的角色「{self.role}」缺少权限点 {permission}，操作已拦截",
                reason="missing_permission",
            )

    def require_owner(self, owner_tenant: str | None, resource: str = "资源") -> None:
        if not self.owns(owner_tenant):
            owner_name = TENANTS.get(owner_tenant or "", {}).get("name", owner_tenant or "未知归属")
            raise PermissionDeniedError(
                f"{resource}归属「{owner_name}」，当前租户「{self.tenant_name}」无权跨归属操作",
                reason="cross_tenant_denied",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "tenant_name": self.tenant_name,
            "role": self.role,
            "permissions": sorted(self.permissions),
            "effective_tenants": self.effective_tenants(),
        }


def build_context(tenant_id: str | None, role: str | None) -> RequestContext:
    """从请求头构造上下文；非法租户/角色直接拒绝而不是静默回落。

    角色头接受 ASCII 角色码（admin/auditor/viewer），也兼容直接传中文角色名。
    """
    tenant = (tenant_id or DEFAULT_TENANT).strip()
    if tenant not in TENANTS:
        raise PermissionDeniedError(f"未知租户标识：{tenant}", reason="unknown_tenant")
    raw_role = (role or "admin").strip()
    role_name = ROLE_CODES.get(raw_role, raw_role)
    if role_name not in ROLE_PERMISSIONS:
        raise PermissionDeniedError(f"未知角色：{raw_role}", reason="unknown_role")
    return RequestContext(tenant_id=tenant, role=role_name,
                          permissions=set(ROLE_PERMISSIONS[role_name]))


def get_context(
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_tenant_role: str | None = Header(default=None, alias="X-Tenant-Role"),
) -> RequestContext:
    """FastAPI 依赖：把请求头解析成租户上下文。"""
    return build_context(x_tenant_id, x_tenant_role)


def tenant_catalog() -> dict[str, Any]:
    """供前端渲染租户/角色切换器的目录数据。"""
    return {
        "tenants": [{"id": tid, "name": meta["name"], "system": meta["system"],
                     "shared": tid == SHARED_TENANT}
                    for tid, meta in TENANTS.items()],
        "roles": [{"name": role, "code": code, "permissions": sorted(perms)}
                  for code, role in ROLE_CODES.items()
                  for perms in [ROLE_PERMISSIONS[role]]],
        "default_tenant": DEFAULT_TENANT,
        "default_role": DEFAULT_ROLE,
        "shared_tenant": SHARED_TENANT,
    }
