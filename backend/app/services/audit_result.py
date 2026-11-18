"""分级稽核结果表业务规则。

职责边界：
- 按租户归属抽样稽核可访问的稽核规则，生成分级结果表；
- 结果表明细按租户隔离，汇总只保留匿名口径，禁止出现可反推单租户的行数；
- 状态只能沿「待复核 → 复核中 → 待发布 → 已发布」单向推进，拒绝跳级与回退；
- 权限与归属在服务层双重校验：缺权限或跨归属写操作直接拒绝；
- 确认发布做明细/汇总边界一致性校验，跨模块持久化到数据发布侧。

尚未裁决的两项策略（结果表确认冲突、稽核规则优先级）显式以冲突状态暴露，
不做静默选择：冲突批次必须先由人工处置，发布动作返回 409。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.quality_check import QualityCheckService, parse_sample_ratio
from app.store import store
from app.tenancy import (
    PERM_CROSS_SUMMARY,
    PERM_PUBLISH,
    PERM_READ,
    PERM_REVIEW,
    PERM_SUBMIT,
    RequestContext,
    SHARED_TENANT,
    PermissionDeniedError,
    StateConflictError,
)

MODULE = "audit_result"
STATUS_ORDER = ["待复核", "复核中", "待发布", "已发布"]
ACTION_RULES = {
    "提交稽核": "待复核",
    "开始复核": "复核中",
    "复核通过": "待发布",
    "确认发布": "已发布",
}
# 状态机：只允许推进到相邻的下一个状态
NEXT_STATUS: dict[str, str] = {
    STATUS_ORDER[0]: STATUS_ORDER[1],
    STATUS_ORDER[1]: STATUS_ORDER[2],
    STATUS_ORDER[2]: STATUS_ORDER[3],
}
ACTION_PERMISSION = {
    "提交稽核": PERM_SUBMIT,
    "开始复核": PERM_REVIEW,
    "复核通过": PERM_REVIEW,
    "确认发布": PERM_PUBLISH,
}
LIST_FIELDS = [
    "结果编号", "批次编号", "租户归属", "稽核等级", "规则通过率",
    "采样比例", "明细规则数", "跨租户汇总", "汇总口径", "创建时间",
]
# 分级阈值：按失败率（失败样本/抽样样本）从低到高划分
GRADE_ORDER = ["A", "B", "C", "D"]
GRADE_LABELS = {"A": "A级（低风险）", "B": "B级（关注）", "C": "C级（预警）", "D": "D级（阻断）"}
GRADE_THRESHOLDS = [(0.0, "A"), (0.05, "B"), (0.2, "C"), (0.5, "D")]


def grade_for_failure_rate(failure_rate: float) -> str:
    grade = "A"
    for threshold, candidate in GRADE_THRESHOLDS:
        if failure_rate >= threshold:
            grade = candidate
    return grade


class AuditResultService:
    # ---- 查询 ----

    def list_results(
        self,
        ctx: RequestContext,
        *,
        status: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """按归属与权限过滤结果表：本租户明细 + 有权限时的匿名汇总，绝不暴露他人明细。"""
        ctx.require(PERM_READ)
        rows = []
        for entry in store.audit_results():
            if not ctx.can_access_owner(entry.get("租户归属")):
                continue
            if status and entry.get("status") != status:
                continue
            if keyword and keyword.casefold() not in str(entry.get("结果编号", "")).casefold():
                continue
            rows.append(self._project_for_context(entry, ctx))
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_result(self, result_id: int, ctx: RequestContext) -> dict[str, Any]:
        ctx.require(PERM_READ)
        entry = store.find_audit_result(result_id)
        if entry is None:
            raise StateConflictError(f"分级稽核结果 {result_id} 不存在或已归档", reason="not_found")
        if not ctx.can_access_owner(entry.get("租户归属")):
            raise PermissionDeniedError(
                "结果表归属其他租户，当前租户无权查看明细", reason="cross_tenant_denied")
        return self._project_for_context(entry, ctx)

    # ---- 生成结果表 ----

    def create_audit_batch(
        self,
        ctx: RequestContext,
        *,
        batch_no: str | None = None,
        sample_ratio: float | None = None,
        rule_ids: list[int] | None = None,
        include_cross_summary: bool = True,
    ) -> dict[str, Any]:
        """按租户归属抽样稽核并生成分级结果表。"""
        ctx.require(PERM_SUBMIT)
        qc = QualityCheckService()
        rules = self._select_rules(ctx, rule_ids)
        if not rules:
            raise StateConflictError("当前租户归属下没有可稽核的生效规则，无法生成结果表",
                                     reason="no_rules")
        # 逐规则按归属抽样求值；规则的权限与归属沿调用链（ctx）定位
        details: list[dict[str, Any]] = []
        for rule in rules:
            result = qc.execute_check(
                int(rule["id"]), ctx,
                override_ratio=sample_ratio if sample_ratio is not None
                else parse_sample_ratio(rule.get("采样比例")),
            )["result"]
            details.append({
                "rule_id": result["rule_id"],
                "table": result["table"],
                "field": result["field"],
                "tenant_id": rule.get("租户归属") or ctx.tenant_id,
                "total": result["total"],
                "sampled": result["sampled"],
                "passed": result["passed"],
                "failed": result["failed"],
                "pass_rate": result["pass_rate"],
                "failure_rate": round(1 - result["pass_rate"], 4),
            })
        aggregate = self._aggregate_details(details)
        # 跨租户匿名汇总：仅在拥有跨租户汇总权限时写入，且只保留不可反推的口径
        cross_summary = None
        if include_cross_summary:
            cross_summary = self._build_cross_summary(ctx, qc, sample_ratio)
        # 同一批次内规则结论互相矛盾 => 确认冲突，待人工裁决
        conflict = self._detect_conflict(details, cross_summary)
        seq = len(store.audit_results()) + 1
        entry = {
            "结果编号": f"AUD-{datetime.now().strftime('%Y%m%d')}-{seq:03d}",
            "批次编号": batch_no or f"BATCH-{datetime.now().strftime('%Y%m%d')}-{seq:03d}",
            "租户归属": ctx.tenant_id,
            "owner_tenant": ctx.tenant_id,
            "status": STATUS_ORDER[0],
            "稽核等级": GRADE_LABELS[aggregate["grade"]],
            "grade": aggregate["grade"],
            "规则通过率": aggregate["pass_rate_text"],
            "pass_rate": aggregate["pass_rate"],
            "采样比例": aggregate["sample_ratio_text"],
            "sample_ratio": aggregate["sample_ratio"],
            "明细规则数": len(details),
            "跨租户汇总": "含匿名汇总" if cross_summary else "不含",
            "汇总口径": "匿名跨租户汇总" if cross_summary else "仅本租户明细",
            "创建时间": datetime.now().isoformat(),
            "created_by_role": ctx.role,
            "details": details,
            "cross_summary": cross_summary,
            "conflict": conflict,
            "published_snapshot": None,
            "pending": True,
            "abnormal": aggregate["grade"] in ("C", "D") or conflict is not None,
        }
        store.add_audit_result(entry)
        store.save_version(MODULE, entry["id"], entry)
        return self._project_for_context(entry, ctx)

    def _select_rules(self, ctx: RequestContext, rule_ids: list[int] | None) -> list[dict[str, Any]]:
        rules = QualityCheckService().rules_for_tenant(ctx, status="已生效")
        if rule_ids:
            wanted = set(rule_ids)
            picked = [rule for rule in rules if int(rule.get("id", 0)) in wanted]
            missing = wanted - {int(rule["id"]) for rule in picked}
            if missing:
                raise PermissionDeniedError(
                    f"规则 {sorted(missing)} 不属于当前租户或未生效，归属边界拒绝纳入结果表",
                    reason="cross_tenant_denied")
            return picked
        return list(rules)

    def _aggregate_details(self, details: list[dict[str, Any]]) -> dict[str, Any]:
        sampled = sum(item["sampled"] for item in details)
        failed = sum(item["failed"] for item in details)
        passed = sum(item["passed"] for item in details)
        failure_rate = round(failed / sampled, 4) if sampled else 0.0
        pass_rate = round(passed / sampled, 4) if sampled else 1.0
        ratio = round(sum(item["sampled"] for item in details)
                      / sum(item["total"] for item in details), 4) \
            if sum(item["total"] for item in details) else 0.0
        return {
            "grade": grade_for_failure_rate(failure_rate),
            "failure_rate": failure_rate,
            "pass_rate": pass_rate,
            "pass_rate_text": f"{pass_rate * 100:.2f}%",
            "sample_ratio": ratio,
            "sample_ratio_text": f"{ratio * 100:.1f}%",
            "sampled": sampled,
            "passed": passed,
            "failed": failed,
        }

    def _build_cross_summary(
        self,
        ctx: RequestContext,
        qc: QualityCheckService,
        sample_ratio: float | None,
    ) -> dict[str, Any] | None:
        """跨租户汇总：只输出匿名聚合（通过率、样本总量、等级分布），不输出任何租户行数。"""
        if not ctx.can(PERM_CROSS_SUMMARY):
            return None
        contributions: list[dict[str, Any]] = []
        tenant_count = 0
        for rule in store.rows("quality_check"):
            owner = rule.get("租户归属")
            if rule.get("status") != "已生效":
                continue
            if owner in (None, ctx.tenant_id, SHARED_TENANT):
                continue
            tenant_count += 1
            # 用受限上下文求值：只授予读取，沿调用链定位规则归属
            scoped = RequestContext(tenant_id=owner, role="查看员")
            result = qc.execute_check(
                int(rule["id"]), scoped,
                override_ratio=sample_ratio if sample_ratio is not None
                else parse_sample_ratio(rule.get("采样比例")),
            )["result"]
            contributions.append({
                "pass_rate": result["pass_rate"],
                "sampled": result["sampled"],
                "passed": result["passed"],
                "failed": result["failed"],
            })
        if not contributions:
            return {
                "mode": "anonymous",
                "tenant_count": 0,
                "rule_count": 0,
                "overall_pass_rate": "100.00%",
                "grade_distribution": {grade: 0 for grade in GRADE_ORDER},
                "note": "无可汇总的其他租户规则",
            }
        sampled = sum(item["sampled"] for item in contributions)
        passed = sum(item["passed"] for item in contributions)
        overall = round(passed / sampled, 4) if sampled else 1.0
        distribution = {grade: 0 for grade in GRADE_ORDER}
        for item in contributions:
            distribution[grade_for_failure_rate(1 - item["pass_rate"])] += 1
        return {
            "mode": "anonymous",
            "tenant_count": tenant_count,
            "rule_count": len(contributions),
            "overall_pass_rate": f"{overall * 100:.2f}%",
            "total_sampled": sampled,
            "grade_distribution": distribution,
            # 显式声明：不含任何租户标识与单租户行数，无法反推
            "contains_tenant_rows": False,
            "generated_at": datetime.now().isoformat(),
        }

    def _detect_conflict(
        self,
        details: list[dict[str, Any]],
        cross_summary: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """确认冲突检测：同批次规则结论互相矛盾（存在全通过规则、也存在阻断级规则）即标记待裁决。

        规则优先级尚未裁决，因此不能自行决定以哪条规则为准，冲突批次禁止直接发布。
        """
        has_clean_rule = any(item["failure_rate"] == 0 for item in details)
        has_block_rule = any(item["failure_rate"] >= 0.5 for item in details)
        if details and has_clean_rule and has_block_rule:
            return {
                "type": "rule_verdict_conflict",
                "message": "同批次存在全部通过与阻断级失败并存的规则，确认冲突处理规则尚未裁决，暂不允许发布",
                "rule_ids": [item["rule_id"] for item in details
                             if item["failure_rate"] == 0 or item["failure_rate"] >= 0.5],
                "pending_decision": "结果表确认冲突与稽核规则优先级均待裁决",
            }
        if cross_summary and cross_summary.get("rule_count"):
            # 本租户全通过、跨租户匿名口径却出现 D 级分布：口径冲突，同样待裁决
            all_pass = details and all(item["failure_rate"] == 0 for item in details)
            if all_pass and cross_summary.get("grade_distribution", {}).get("D", 0) > 0:
                return {
                    "type": "summary_scope_conflict",
                    "message": "本租户明细全部通过，但匿名跨租户汇总出现阻断级规则，口径冲突待裁决",
                    "pending_decision": "结果表确认冲突与稽核规则优先级均待裁决",
                }
        return None

    # ---- 状态机 ----

    def run_action(self, result_id: int, action: str, ctx: RequestContext) -> dict[str, Any]:
        ctx.require(PERM_READ)
        entry = store.find_audit_result(result_id)
        if entry is None:
            raise StateConflictError(f"分级稽核结果 {result_id} 不存在或已归档", reason="not_found")
        ctx.require_owner(entry.get("租户归属"), resource=f"分级稽核结果 {entry.get('结果编号')}")
        if action not in ACTION_RULES:
            raise StateConflictError(f"动作「{action}」不属于分级稽核结果表可执行范围",
                                     reason="illegal_action")
        permission = ACTION_PERMISSION[action]
        if not ctx.can(permission):
            raise PermissionDeniedError(
                f"角色「{ctx.role}」缺少权限点 {permission}，操作已拦截",
                reason="missing_permission")
        current = entry["status"]
        if current == STATUS_ORDER[-1]:
            raise StateConflictError("结果表已发布，状态为终态，不允许继续推进或回退",
                                     reason="terminal_state")
        expected = NEXT_STATUS[current]
        target = ACTION_RULES[action]
        # 单向推进：动作目标必须恰好是当前状态的下一个相邻状态，跳级/回退一律拒绝
        if target != expected:
            raise StateConflictError(
                f"状态只能单向推进：当前「{current}」的下一状态是「{expected}」，"
                f"动作「{action}」试图跳级到「{target}」，已拒绝",
                reason="status_skip")
        if action == "确认发布":
            self._guard_publish(entry, ctx)
            self._persist_to_publish(entry, ctx)
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry[f"{action}_at"] = datetime.now().isoformat()
        entry[f"{action}_by"] = ctx.role
        store.save_version(MODULE, entry["id"], entry)
        return self._project_for_context(entry, ctx)

    def _guard_publish(self, entry: dict[str, Any], ctx: RequestContext) -> None:
        """发布前边界一致性校验：明细隔离与汇总发布必须同时成立。"""
        details = entry.get("details", [])
        for item in details:
            if item.get("tenant_id") in (ctx.tenant_id, SHARED_TENANT) or item.get("tenant_id") is None:
                continue
            raise StateConflictError(
                "结果表明细包含当前租户未授权的他租户数据，发布将越过归属边界，已拦截",
                reason="boundary_breach")
        cross = entry.get("cross_summary")
        if cross:
            if not ctx.can(PERM_CROSS_SUMMARY):
                raise StateConflictError("匿名汇总需要跨租户汇总权限，当前身份不满足发布条件",
                                         reason="missing_cross_permission")
            if cross.get("contains_tenant_rows") is not False:
                raise StateConflictError("跨租户汇总携带了可反推的单租户行数，发布边界校验失败",
                                         reason="reversible_summary")
            forbidden = {"failed", "passed", "sampled", "total"}.intersection(
                self._tenant_level_keys(cross))
            if forbidden:
                raise StateConflictError("汇总字段包含可反推单租户数量的口径，发布被拦截",
                                         reason="reversible_summary")
        if entry.get("conflict"):
            raise StateConflictError(
                f"结果表存在待裁决冲突：{entry['conflict'].get('message', '冲突处理规则未定')}",
                reason="unresolved_conflict")

    @staticmethod
    def _tenant_level_keys(payload: Any) -> set[str]:
        """递归收集汇总结构里疑似租户级别的字段键，用于防反推校验。"""
        keys: set[str] = set()
        if isinstance(payload, dict):
            for key, value in payload.items():
                if isinstance(value, dict) and any(
                        token in key.lower() for token in ("tenant", "owner")):
                    keys.update(value.keys())
                keys.update(AuditResultService._tenant_level_keys(value))
        elif isinstance(payload, list):
            for item in payload:
                keys.update(AuditResultService._tenant_level_keys(item))
        return keys

    def _persist_to_publish(self, entry: dict[str, Any], ctx: RequestContext) -> None:
        """数据发布跨模块持久化：发布时把分级结果表快照写入发布侧存档。"""
        snapshot = {
            "结果编号": entry["结果编号"],
            "批次编号": entry["批次编号"],
            "dataset": f"audit_result/{entry['结果编号']}",
            "owner_tenant": entry["租户归属"],
            "grade": entry["grade"],
            "pass_rate": entry["pass_rate"],
            "status": "已发布",
            "cross_summary": entry.get("cross_summary"),
            "detail_rule_count": len(entry.get("details", [])),
            "published_by": ctx.role,
            "published_at": datetime.now().isoformat(),
        }
        entry["published_snapshot"] = snapshot
        store.save_publish_audit_snapshot(snapshot)

    # ---- 视图投影 ----

    def _project_for_context(self, entry: dict[str, Any], ctx: RequestContext) -> dict[str, Any]:
        """按归属和权限投影结果表：明细仅本租户可见；共享/他租户内容保持只读。"""
        view = {key: entry.get(key) for key in ("id", *LIST_FIELDS, "status", "grade",
                                                 "pass_rate", "sample_ratio", "conflict",
                                                 "published_snapshot")}
        is_owner = entry.get("租户归属") == ctx.tenant_id
        view["readonly"] = not is_owner
        view["accessible"] = True
        if is_owner:
            # 本租户：返回完整明细
            view["details"] = [dict(item) for item in entry.get("details", [])]
            view["cross_summary"] = entry.get("cross_summary") if ctx.can(PERM_CROSS_SUMMARY) else None
        else:
            # 非归属（共享域）：只读，且剥离明细行，仅保留汇总口径
            view["details"] = []
            view["cross_summary"] = entry.get("cross_summary")
            view["boundary_note"] = "共享内容仅支持只读查看，明细按租户归属隔离"
        view["allowed_actions"] = self._allowed_actions(entry, ctx)
        return view

    def _allowed_actions(self, entry: dict[str, Any], ctx: RequestContext) -> list[str]:
        if entry.get("租户归属") != ctx.tenant_id:
            return []
        if entry["status"] == STATUS_ORDER[-1]:
            return []
        next_action = {v: k for k, v in ACTION_RULES.items()}.get(NEXT_STATUS[entry["status"]])
        if next_action and ctx.can(ACTION_PERMISSION[next_action]):
            return [next_action]
        return []
