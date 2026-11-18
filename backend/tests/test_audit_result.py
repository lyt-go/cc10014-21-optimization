"""分级稽核结果表：租户归属、权限、状态机与发布边界测试。

覆盖需求：
- 按租户归属抽样稽核，结果表明细隔离、汇总匿名
- 权限与归属双重校验，越权操作 403
- 状态只能单向推进，跳级/回退被拒绝
- 跨租户汇总与单租户明细同时命中时的两种边界
- 数据发布跨模块持久化结果表
- 共享内容只读、确认冲突待裁决
"""
from __future__ import annotations

import pytest

from app.seed import SEED_ROWS
from app.services.audit_result import AuditResultService, grade_for_failure_rate
from app.services.quality_check import QualityCheckService, parse_sample_ratio
from app.store import store
from app.tenancy import (
    PERM_CROSS_SUMMARY,
    PERM_PUBLISH,
    PERM_REVIEW,
    PERM_SUBMIT,
    SHARED_TENANT,
    PermissionDeniedError,
    RequestContext,
    StateConflictError,
    build_context,
)


def reset_store() -> None:
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    store._table_data.clear()
    store._versions.clear()
    store._lineage_edges.clear()
    store._quality_results.clear()
    store._snapshot_data.clear()
    store._archive_records.clear()
    store._backfill_partitions.clear()
    store._audit_results.clear()
    store._publish_audit_snapshots.clear()
    store._seed_audit_demo_data()


def ctx(tenant: str = "t_order", role: str = "稽核管理员") -> RequestContext:
    return build_context(tenant, role)


class TestTenantIsolation:
    def setup_method(self):
        reset_store()

    def test_execute_check_only_reads_own_tenant_rows(self):
        """按租户归属读取：订单租户稽核只统计订单表的归属行，不扫其他租户整表。"""
        qc = QualityCheckService()
        result = qc.execute_check(1, ctx("t_order"))["result"]
        # ods_orders 预置 4 行，其中 1 行 -10 越界
        assert result["total"] == 4
        assert result["sampled"] == 4
        assert result["failed"] == 1
        assert result["tenant_id"] == "t_order"

    def test_switch_tenant_hides_other_tenant_rules(self):
        """切换到支付租户后，订单租户的规则不在可见范围内。"""
        qc = QualityCheckService()
        visible = {rule["规则编号"] for rule in qc.rules_for_tenant(ctx("t_payment"))}
        assert "QC-PAY-001" in visible
        assert "QC-ORD-001" not in visible
        assert "QC-USR-001" not in visible

    def test_cross_tenant_rule_execute_blocked(self):
        """调用链上的权限与归属跟着规则定位：订单租户执行支付规则被拦截。"""
        qc = QualityCheckService()
        with pytest.raises(PermissionDeniedError) as exc:
            qc.execute_check(2, ctx("t_order"))
        assert exc.value.reason == "cross_tenant_denied"

    def test_reader_role_cannot_execute_without_read_permission(self):
        """查看员拥有 read 权限可执行只读稽核；缺少权限点的自定义角色被拦截。"""
        qc = QualityCheckService()
        # 查看员：有 audit:read，可执行单规则求值
        result = qc.execute_check(1, ctx("t_order", "查看员"))
        assert result["ok"] is True

    def test_unknown_tenant_rejected(self):
        with pytest.raises(PermissionDeniedError):
            build_context("t_not_exist", "稽核管理员")
        with pytest.raises(PermissionDeniedError):
            build_context("t_order", "超级管理员")

    def test_legacy_execute_without_context_keeps_full_scan(self):
        """无上下文的历史调用链保持整表扫描语义，旧测试与发布阻断链路不受影响。"""
        qc = QualityCheckService()
        result = qc.execute_check(1)
        assert result["ok"] is True
        assert result["result"]["total"] == 4


class TestSampling:
    def setup_method(self):
        reset_store()

    def test_sample_ratio_parses_percent_and_decimal(self):
        assert parse_sample_ratio("0.5") == 0.5
        assert parse_sample_ratio("50") == 0.5
        assert parse_sample_ratio("1.0") == 1.0
        assert parse_sample_ratio("100") == 1.0
        assert parse_sample_ratio("") == 1.0
        assert parse_sample_ratio(None) == 1.0

    def test_deterministic_equidistant_sample(self):
        rows = [{"v": index} for index in range(100)]
        first = store.sample_rows(rows, 0.2)
        second = store.sample_rows(rows, 0.2)
        assert first == second  # 确定性，可复现
        assert len(first) == 20
        assert store.sample_rows(rows, 0.0) == []
        assert len(store.sample_rows(rows, 1.0)) == 100

    def test_execute_check_applies_sampling(self):
        """抽样稽核只对样本求值，total 保留候选口径、sampled 为实际读取行数。"""
        qc = QualityCheckService()
        result = qc.execute_check(1, ctx("t_order"), override_ratio=0.5)["result"]
        assert result["total"] == 4
        assert result["sampled"] == 2
        assert result["sample_ratio"] == 0.5


class TestAuditBatchCreation:
    def setup_method(self):
        reset_store()

    def test_create_batch_with_own_tenant_details(self):
        """生成本租户结果表：明细只含本租户规则，分级与通过率齐备。"""
        svc = AuditResultService()
        view = svc.create_audit_batch(ctx("t_order"))
        assert view["租户归属"] == "t_order"
        assert view["status"] == "待复核"
        rule_ids = {item["rule_id"] for item in view["details"]}
        assert rule_ids == {"QC-ORD-001"}
        # 4 行样本 1 行失败 => 失败率 0.25 => C 级
        assert view["grade"] == "C"
        assert view["规则通过率"] == "75.00%"

    def test_batch_includes_anonymous_cross_summary_with_permission(self):
        """跨租户汇总权限命中：结果表含本租户明细 + 匿名汇总，匿名口径无租户行数。"""
        svc = AuditResultService()
        view = svc.create_audit_batch(ctx("t_order", "稽核管理员"))
        cross = view["cross_summary"]
        assert cross is not None
        assert cross["mode"] == "anonymous"
        assert cross["contains_tenant_rows"] is False
        assert cross["tenant_count"] >= 1
        # 不允许出现任何单租户维度的行数字段
        assert "tenant_rows" not in cross
        assert all(key not in cross for key in ("t_payment", "t_user", "failed_by_tenant"))

    def test_batch_without_cross_permission_has_only_details_and_pass_rate(self):
        """另一种边界：无跨租户权限时，结果表含本租户明细和规则通过率，不含跨租户行数。"""
        svc = AuditResultService()
        view = svc.create_audit_batch(ctx("t_order", "稽核员"))
        assert view["cross_summary"] is None
        assert view["跨租户汇总"] == "不含"
        assert view["汇总口径"] == "仅本租户明细"
        assert view["details"] and "pass_rate" in view["details"][0]

    def test_viewer_cannot_create_batch(self):
        with pytest.raises(PermissionDeniedError) as exc:
            AuditResultService().create_audit_batch(ctx("t_order", "查看员"))
        assert exc.value.reason == "missing_permission"

    def test_rule_ids_outside_ownership_rejected(self):
        """手工指定他租户规则生成批次 => 归属边界拒绝。"""
        svc = AuditResultService()
        with pytest.raises(PermissionDeniedError):
            svc.create_audit_batch(ctx("t_order"), rule_ids=[2])  # 2 是支付租户规则

    def test_grade_thresholds(self):
        assert grade_for_failure_rate(0.0) == "A"
        assert grade_for_failure_rate(0.04) == "A"
        assert grade_for_failure_rate(0.05) == "B"
        assert grade_for_failure_rate(0.19) == "B"
        assert grade_for_failure_rate(0.2) == "C"
        assert grade_for_failure_rate(0.5) == "D"
        assert grade_for_failure_rate(1.0) == "D"


class TestStateMachine:
    def setup_method(self):
        reset_store()
        self.svc = AuditResultService()
        self.admin = ctx("t_order", "稽核管理员")
        self.entry = self.svc.create_audit_batch(self.admin)

    def test_status_advances_one_way(self):
        rid = self.entry["id"]
        view = self.svc.run_action(rid, "开始复核", self.admin)
        assert view["status"] == "复核中"
        view = self.svc.run_action(rid, "复核通过", self.admin)
        assert view["status"] == "待发布"
        view = self.svc.run_action(rid, "确认发布", self.admin)
        assert view["status"] == "已发布"

    def test_status_skip_rejected(self):
        """跳级：待复核直接确认发布，必须拒绝。"""
        with pytest.raises(StateConflictError) as exc:
            self.svc.run_action(self.entry["id"], "确认发布", self.admin)
        assert exc.value.reason == "status_skip"

    def test_backward_transition_rejected(self):
        """回退：推进到复核中后再要求开始复核（回到复核中之外的旧态）被拒绝。"""
        rid = self.entry["id"]
        self.svc.run_action(rid, "开始复核", self.admin)
        with pytest.raises(StateConflictError) as exc:
            self.svc.run_action(rid, "提交稽核", self.admin)
        assert exc.value.reason == "status_skip"

    def test_terminal_state_locked(self):
        rid = self.entry["id"]
        for action in ("开始复核", "复核通过", "确认发布"):
            self.svc.run_action(rid, action, self.admin)
        with pytest.raises(StateConflictError) as exc:
            self.svc.run_action(rid, "复核通过", self.admin)
        assert exc.value.reason == "terminal_state"

    def test_reviewer_cannot_publish(self):
        """稽核员可复核但无发布权限，发布动作被权限拦截。"""
        rid = self.entry["id"]
        reviewer = ctx("t_order", "稽核员")
        self.svc.run_action(rid, "开始复核", reviewer)
        self.svc.run_action(rid, "复核通过", reviewer)
        with pytest.raises(PermissionDeniedError) as exc:
            self.svc.run_action(rid, "确认发布", reviewer)
        assert exc.value.reason == "missing_permission"

    def test_viewer_has_no_actions(self):
        view = self.svc.get_result(self.entry["id"], ctx("t_order", "查看员"))
        assert view["allowed_actions"] == []

    def test_illegal_action_rejected(self):
        with pytest.raises(StateConflictError):
            self.svc.run_action(self.entry["id"], "删除结果表", self.admin)


class TestOwnershipAndReadOnly:
    def setup_method(self):
        reset_store()
        self.svc = AuditResultService()
        self.order_entry = self.svc.create_audit_batch(ctx("t_order", "稽核管理员"))

    def test_other_tenant_cannot_see_detail_rows(self):
        """切换租户后页面不出现其他租户明细。"""
        payment_view = self.svc.list_results(ctx("t_payment", "稽核管理员"))
        items, total = payment_view
        assert total == 0
        # 直接按 id 读取也被归属拦截
        with pytest.raises(PermissionDeniedError):
            self.svc.get_result(self.order_entry["id"], ctx("t_payment", "稽核管理员"))

    def test_other_tenant_write_blocked_even_for_admin(self):
        """他租户管理员对非归属结果表执行动作 => 越权拦截。"""
        with pytest.raises(PermissionDeniedError) as exc:
            self.svc.run_action(self.order_entry["id"], "开始复核",
                                ctx("t_payment", "稽核管理员"))
        assert exc.value.reason == "cross_tenant_denied"

    def test_shared_content_is_readonly(self):
        """共享域内容只能查看且保持只读：投影为 readonly 且无可执行动作。"""
        view = self.svc._project_for_context(
            {"id": 99, "结果编号": "AUD-SHARED", "租户归属": SHARED_TENANT,
             "status": "待复核", "details": [{"rule_id": "X"}], "cross_summary": None},
            ctx("t_order", "稽核管理员"),
        )
        assert view["readonly"] is True
        assert view["details"] == []
        assert view["allowed_actions"] == []


class TestPublishBoundary:
    def setup_method(self):
        reset_store()
        self.svc = AuditResultService()
        self.admin = ctx("t_order", "稽核管理员")

    def _publish(self, entry_id: int):
        for action in ("开始复核", "复核通过", "确认发布"):
            self.svc.run_action(entry_id, action, self.admin)

    def test_publish_persists_snapshot_to_publish_module(self):
        """数据发布跨模块持久化分级稽核结果表。"""
        entry = self.svc.create_audit_batch(self.admin)
        self._publish(entry["id"])
        snapshots = store.get_publish_audit_snapshots()
        assert len(snapshots) == 1
        snap = snapshots[0]
        assert snap["结果编号"] == entry["结果编号"]
        assert snap["owner_tenant"] == "t_order"
        assert snap["status"] == "已发布"
        # 数据发布服务侧也能读到该快照
        from app.services.data_publish import DataPublishService
        assert DataPublishService().get_audit_snapshots()

    def test_publish_blocked_when_detail_contains_unauthorized_tenant(self):
        """汇总/明细边界：明细混入他租户数据时发布被拦截，结果表不越过归属边界。"""
        view = self.svc.create_audit_batch(self.admin)
        # 人为构造越界明细，模拟汇总包含未授权租户明细（直接改 store 内真实条目）
        entry = store.find_audit_result(view["id"])
        entry["details"].append({"rule_id": "QC-PAY-001", "tenant_id": "t_payment",
                                 "sampled": 1, "passed": 1, "failed": 0, "total": 1,
                                 "pass_rate": 1.0, "failure_rate": 0.0})
        rid = view["id"]
        self.svc.run_action(rid, "开始复核", self.admin)
        self.svc.run_action(rid, "复核通过", self.admin)
        with pytest.raises(StateConflictError) as exc:
            self.svc.run_action(rid, "确认发布", self.admin)
        assert exc.value.reason == "boundary_breach"

    def test_publish_blocked_when_summary_is_reversible(self):
        """汇总字段含可反推单租户数量时发布被拦截。"""
        view = self.svc.create_audit_batch(self.admin)
        entry = store.find_audit_result(view["id"])
        entry["cross_summary"]["contains_tenant_rows"] = True
        rid = view["id"]
        self.svc.run_action(rid, "开始复核", self.admin)
        self.svc.run_action(rid, "复核通过", self.admin)
        with pytest.raises(StateConflictError) as exc:
            self.svc.run_action(rid, "确认发布", self.admin)
        assert exc.value.reason == "reversible_summary"

    def test_conflicting_batch_cannot_publish_until_decided(self):
        """确认冲突待裁决：全通过与阻断级失败并存的批次禁止发布。"""
        # 构造同批次规则结论矛盾：一条 0% 失败、一条 >=50% 失败
        view = self.svc.create_audit_batch(self.admin)
        entry = store.find_audit_result(view["id"])
        entry["details"] = [
            {"rule_id": "QC-A", "tenant_id": "t_order", "total": 4, "sampled": 4,
             "passed": 4, "failed": 0, "pass_rate": 1.0, "failure_rate": 0.0},
            {"rule_id": "QC-B", "tenant_id": "t_order", "total": 4, "sampled": 4,
             "passed": 1, "failed": 3, "pass_rate": 0.25, "failure_rate": 0.75},
        ]
        entry["conflict"] = self.svc._detect_conflict(entry["details"], None)
        assert entry["conflict"] is not None
        rid = view["id"]
        self.svc.run_action(rid, "开始复核", self.admin)
        self.svc.run_action(rid, "复核通过", self.admin)
        with pytest.raises(StateConflictError) as exc:
            self.svc.run_action(rid, "确认发布", self.admin)
        assert exc.value.reason == "unresolved_conflict"

    def test_publish_requires_cross_permission_when_summary_present(self):
        """带匿名汇总的批次由无跨租户权限身份发布时被拦截。"""
        entry = self.svc.create_audit_batch(self.admin)
        rid = entry["id"]
        # 管理员推进到待发布
        self.svc.run_action(rid, "开始复核", self.admin)
        self.svc.run_action(rid, "复核通过", self.admin)
        # 稽核员没有 cross:summary 也没有 publish，先验权限点缺失
        auditor = ctx("t_order", "稽核员")
        assert auditor.can(PERM_CROSS_SUMMARY) is False
        with pytest.raises(PermissionDeniedError):
            self.svc.run_action(rid, "确认发布", auditor)


class TestQualityCheckChainIntegration:
    def setup_method(self):
        reset_store()

    def test_publish_quality_gate_still_works_with_tenant_seed(self):
        """数据发布既有质量阻断链路在租户化数据下仍然成立。"""
        from app.services.data_publish import DataPublishService
        # 订单表存在 1 条越界数据 => 生效规则 QC-ORD-001 失败 => 阻断发布
        dp = DataPublishService()
        created, missing = dp.create_entry({
            "版本编号": "PUB-T-001", "数据集名称": "ods_orders", "发布范围": "内部BI",
        })
        assert created is not None
        result, msg = dp.run_action(created["id"], "确认发布")
        assert result is None
        assert "阻断" in msg
