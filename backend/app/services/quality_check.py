"""质量稽核业务规则：支持读取目标表数据并执行校验表达式，记录稽核结果。

多租户改造要点：
- ``execute_check`` 不带上下文时保持历史整表扫描语义，兼容既有调用链；
- 带上 ``RequestContext`` 后按租户归属读取，先抽样后求值，避免对所有租户整表扫描；
- 规则的租户归属与权限在服务层强制校验，路由层不做业务判断。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store
from app.tenancy import (
    PERM_READ,
    RequestContext,
)

MODULE = "quality_check"
LIST_FIELDS = ["规则编号", "校验表名", "校验字段", "校验表达式", "阈值下界", "阈值上界", "阻断级别", "采样比例"]
REQUIRED_FIELDS = ["规则编号", "校验表名", "校验字段"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
NEGATIVE_ACTIONS = ["停用规则"]

# 单次稽核读取行数预算：超预算必须走抽样，杜绝整表全扫
READ_ROW_BUDGET = 100_000


def parse_sample_ratio(raw: Any) -> float:
    """采样比例兼容两种写法：0~1 的小数直接用，1~100 的整数按百分比解释。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 1.0
    if value > 1:
        value = value / 100.0
    return min(1.0, max(0.0, value))


class QualityCheckService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("规则编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        # 新登记规则默认归属当前调用租户；显式传入的归属以入参为准
        entry["租户归属"] = values.get("租户归属") or values.get("_tenant")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"稽核规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于质量稽核可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        return entry, f"稽核规则已{action}"

    # ---- 租户视角 ----

    def rules_for_tenant(self, ctx: RequestContext, *, status: str | None = None) -> list[dict[str, Any]]:
        """调用链可见的稽核规则：本租户归属 + 共享域；其他租户规则不可见。"""
        rules = []
        for rule in store.rows(MODULE):
            owner = rule.get("租户归属")
            if not ctx.can_access_owner(owner):
                continue
            if status and rule.get("status") != status:
                continue
            rules.append(rule)
        return rules

    def _load_rows(self, table: str, ctx: RequestContext | None) -> list[dict[str, Any]]:
        if ctx is None:
            # 兼容历史调用链：整表读取，找不到再回退 ODS 服务
            rows = store.table_rows(table)
            if not rows:
                from app.services.ods_layer import OdsLayerService
                rows = OdsLayerService().get_table_data(table)
            return rows
        return store.tenant_table_rows(table, ctx.tenant_id)

    def execute_check(
        self,
        entry_id: int,
        ctx: RequestContext | None = None,
        *,
        override_ratio: float | None = None,
    ) -> dict[str, Any]:
        """读取目标表数据，执行校验表达式，返回稽核结果。

        带 ``ctx`` 时：
        1. 校验读取权限与规则归属，越权直接抛 ``PermissionDeniedError``；
        2. 只读本租户归属行，按规则采样比例等距抽样，控制读取预算；
        3. 结果附带租户与采样口径，供分级稽核结果表汇总。
        """
        entry = self.get_entry(entry_id)
        if not entry:
            return {"ok": False, "message": "稽核规则不存在"}
        if ctx is not None:
            ctx.require(PERM_READ)
            ctx.require_owner(entry.get("租户归属"), resource=f"稽核规则 {entry.get('规则编号')}")
        table = entry.get("校验表名", "")
        field = entry.get("校验字段", "")
        expr = entry.get("校验表达式", "")
        lower = entry.get("阈值下界", "")
        upper = entry.get("阈值上界", "")
        candidate_rows = self._load_rows(table, ctx)
        ratio = override_ratio if override_ratio is not None else parse_sample_ratio(entry.get("采样比例"))
        # 读取预算保护：候选行数超预算时强制按比例抽样
        if len(candidate_rows) > READ_ROW_BUDGET and ratio >= 1:
            ratio = READ_ROW_BUDGET / len(candidate_rows)
        rows = store.sample_rows(candidate_rows, ratio)
        result = self.evaluate_rows(entry, rows, candidate_count=len(candidate_rows), ctx=ctx)
        store.save_quality_result(entry.get("规则编号", ""), result)
        return {"ok": True,
                "message": f"稽核完成：通过{result['passed']}/失败{result['failed']}（采样{result['sampled']}/{result['total']}）",
                "result": result}

    def evaluate_rows(
        self,
        entry: dict[str, Any],
        rows: list[dict[str, Any]],
        *,
        candidate_count: int | None = None,
        ctx: RequestContext | None = None,
    ) -> dict[str, Any]:
        """对给定样本执行规则求值，不做持久化；结果表服务与单规则执行共用此入口。"""
        table = entry.get("校验表名", "")
        field = entry.get("校验字段", "")
        expr = entry.get("校验表达式", "")
        lower = entry.get("阈值下界", "")
        upper = entry.get("阈值上界", "")
        total = candidate_count if candidate_count is not None else len(rows)
        failed = 0
        failed_rows: list[dict[str, Any]] = []
        for row in rows:
            val = row.get(field)
            if not self._check_value(val, expr, lower, upper):
                failed += 1
                failed_rows.append(row)
        sampled = len(rows)
        passed = sampled - failed
        return {
            "rule_id": entry.get("规则编号"),
            "rule_entry_id": entry.get("id"),
            "table": table,
            "field": field,
            "tenant_id": ctx.tenant_id if ctx is not None else entry.get("租户归属"),
            "total": total,
            "sampled": sampled,
            "sample_ratio": round(sampled / total, 4) if total else 0.0,
            "failed": failed,
            "passed": passed,
            "pass_rate": round(passed / sampled, 4) if sampled else 1.0,
            "checked_at": datetime.now().isoformat(),
            "failed_rows": failed_rows[:5],  # 只保留前5条，且不写入跨租户汇总
        }

    def _check_value(self, val: Any, expr: str, lower: str, upper: str) -> bool:
        if expr:
            # 简单表达式解析：>=0 AND <=999999 或 IN (...)
            if "AND" in expr.upper():
                parts = expr.upper().replace("AND", ",").split(",")
                for part in parts:
                    part = part.strip()
                    if ">=" in part:
                        try:
                            bound = float(part.split(">=")[1].strip())
                            if float(val or 0) < bound:
                                return False
                        except Exception:
                            pass
                    elif "<=" in part:
                        try:
                            bound = float(part.split("<=")[1].strip())
                            if float(val or 0) > bound:
                                return False
                        except Exception:
                            pass
            if "IN" in expr.upper():
                # 提取括号内值
                import re
                m = re.search(r"IN\s*\(([^)]+)\)", expr, re.I)
                if m:
                    allowed = [v.strip().strip("'") for v in m.group(1).split(",")]
                    if str(val) not in allowed:
                        return False
        if lower != "":
            try:
                if float(val or 0) < float(lower):
                    return False
            except Exception:
                pass
        if upper != "":
            try:
                if float(val or 0) > float(upper):
                    return False
            except Exception:
                pass
        return True
